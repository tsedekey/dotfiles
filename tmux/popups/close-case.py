#!/usr/bin/env python3
"""close-case: check, then close, a whole DagRunner change case.

  close-case.py check   [RUN-ID | CASE-KEY | WORKTREE-DIR] [--json]
  close-case.py execute [RUN-ID | CASE-KEY | WORKTREE-DIR] --approve NAME[,NAME...] [--dry-run] [--timeout N] [--json]

Spec: ~/.local/share/general/session-finisher-enhancements/SPEC.md.  With no target it uses the git checkout of
the current directory, which is where the driver session runs.

`check` is READ-ONLY. It finds the case's family from the run's state.json (the driver window and its worktree, the
plan window `change-<case key>`, the triage and babysit windows = launcher `here` windows whose panes sit in the
worktree, the dagrun ui pane) and prints one sheet: READY, NEEDS YOU, BLOCKED, INFO. No tmux, git, gh, dagrun or file
write, and no bytecode is written next to it.

`execute` closes ONLY what is named in --approve (window labels, the kinds triage/babysit/plan/driver, and
`worktree`), from a FRESH check, and refuses anything that fails its gates. The gates fail closed. Order: side windows
(triage, babysit), the plan window, then, detached from the tmux server, the driver window and the worktree last:
`workmux wait <handle> --status done`, the gates again, then `workmux remove -f`. `worktree` deletes the worktree,
its branch and the driver window; `driver` alone closes the driver window and keeps the worktree. Nothing is closed
unless the run is `done` and the branch is merged. Run it from the driver window; a window running the command is
never closed in the first phase.

It reuses finish-window.py (agent_status, merge_state, classify_dirt, sibling_windows) so the popup and this script
cannot disagree about "merged" or "dirty". An untracked SYMLINK is not dirt (removing the worktree removes only the
link); any other untracked file blocks deletion; so does a tracked change.
"""
import argparse
import importlib.util
import json
import os
import re
import shlex
import sys
import time

sys.dont_write_bytecode = True

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
DAGRUNNER = os.environ.get("CLOSE_CASE_DAGRUNNER_HOME") or os.path.join(HOME, ".local/share/camunda/dagrunner")  # the override is for tests
RUNS = os.path.join(DAGRUNNER, "runs")
GIT_LOCK = os.path.join(HOME, ".claude/skills/_shared/git-lock.sh")

_spec = importlib.util.spec_from_file_location("finish_window", os.path.join(HERE, "finish-window.py"))
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)
Fail = fw.Fail
classify_dirt = fw.classify_dirt  # shared with the popup, so both agree on what is dirt (untracked symlinks are not)

READY, NEEDS, BLOCKED, INFO = "ready", "needs-you", "blocked", "info"
GLYPH = {READY: "✓", NEEDS: "!", BLOCKED: "✗", INFO: "·"}
TITLE = {READY: "READY", NEEDS: "NEEDS YOU", BLOCKED: "BLOCKED", INFO: "INFO"}
RESOLVED = {"posted", "skipped", "superseded"}  # triage-state.json lifecycles that need nothing more
FAILED_CHECK = {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"}
# Items that do not say anything about whether the FOLDER is safe to delete: open triage comments live on GitHub and in the
# run folder, and `dagrun cleanup <run>` (releasing active.lock) is a step `execute` takes before the removal.
NOT_ABOUT_THE_FOLDER = {"triage state", "babysit state", "dagrunner lock", "checkpoint"}
SIDE_KINDS = ("triage", "babysit", "side")  # windows that sit inside the worktree and close before the driver
REMOVE_WAIT = 180  # seconds the last step waits for workmux's scheduled removal to finish (module constants so tests can shorten them)
REMOVE_POLL = 1.0


# ----- small helpers -------------------------------------------------------------------------------------------

def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def up(etime):
    """ps etime (`5-01:15:56`) -> `5 d`."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(x) for x in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return f"{days} d" if days else (f"{parts[0]} h" if parts[0] else f"{parts[1]} min")


def under(path, root):
    rp = os.path.realpath(path)
    return rp == root or rp.startswith(root + os.sep)


class Sheet:
    def __init__(self):
        self.items = []

    def add(self, level, who, text):
        self.items.append({"level": level, "who": who, "text": text})


# ----- finding the case ----------------------------------------------------------------------------------------

def resolve_run(arg):
    runs = []
    for rid in (os.listdir(RUNS) if os.path.isdir(RUNS) else []):
        st = read_json(os.path.join(RUNS, rid, "state.json"))
        if st and st.get("worktreePath"):
            runs.append(st)
    if arg:
        exact = [r for r in runs if r.get("runId") == arg]
        if exact:
            return exact[0]
    if arg and not os.path.isdir(arg):  # a case key such as 65755 or pdp-3517/153
        key = arg.strip("/")
        hits = [r for r in runs if f"/change-companion/{key}/" in (r.get("sourcePlanPath") or "")]
        what = f"case key '{key}'"
    else:
        d = arg or os.getcwd()
        rc, top, _ = fw.run(["git", "rev-parse", "--show-toplevel"], cwd=d)
        if rc:
            raise Fail(f"{d} is not inside a git checkout; name a run id, a case key or a worktree.")
        top = os.path.realpath(top)
        hits = [r for r in runs if os.path.realpath(r["worktreePath"]) == top]
        what = f"worktree {top}"
    if not hits:
        raise Fail(f"no DagRunner run found for {what}.")
    return max(hits, key=lambda r: r.get("updatedAt") or "")


def case_info(state):
    plan = state.get("sourcePlanPath") or ""
    m = re.search(r"/change-companion/(.+)/[^/]+$", plan)
    if not m:
        return None, None, None
    case_dir = os.path.dirname(plan)
    cp = next((os.path.join(case_dir, n) for n in ("checkpoint.md", "CHECKPOINT.md")
               if os.path.isfile(os.path.join(case_dir, n))), None)
    return m.group(1), case_dir, cp


def checkpoint_facts(path):
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return {}, None
    sections, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1)
            sections[cur] = []
        elif cur is not None and line.strip():
            sections[cur].append(line)
    m = (re.search(r"^- Issue:\s*https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", text, re.M)
         or re.search(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", text))
    return sections, ({"repo": m.group(1), "number": m.group(2)} if m else None)


# ----- tmux and processes --------------------------------------------------------------------------------------

def inventory():
    rc, out, err = fw.run(["tmux", "list-windows", "-a", "-F",
                           "#{window_id}\t#{session_name}\t#{window_name}\t#{?@launcher_here,1,0}"])
    if rc:
        raise Fail(f"tmux list-windows: {err or 'tmux is not running'}")
    wins = {}
    for line in out.splitlines():
        wid, sess, name, here = line.split("\t")
        wins[wid] = {"id": wid, "session": sess, "name": name, "bare": re.sub(r"^[^A-Za-z0-9]+", "", name),
                     "here": here == "1", "panes": []}
    rc, out, _ = fw.run(["tmux", "list-panes", "-a", "-F",
                         "#{window_id}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"])
    for line in out.splitlines() if rc == 0 else []:
        wid, pid, ppid, cmd, path = line.split("\t")
        if wid in wins:
            wins[wid]["panes"].append({"id": pid, "pid": int(ppid), "cmd": cmd, "path": path})
    return wins


def process_table():
    rc, out, _ = fw.run(["ps", "-axo", "pid=,ppid=,etime=,command="])
    procs = {}
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = {"ppid": int(parts[1]), "etime": parts[2], "cmd": parts[3]}
    return procs


def descendants(procs, root):
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    out, stack = [], list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return sorted(out)


def describe_process(procs, pane):
    """-> (kind, text) for a pane that runs something other than a shell or a tracked agent."""
    kids = [procs[d] for d in descendants(procs, pane["pid"]) if d in procs]
    ui = next((k for k in kids if re.search(r"\bdagrun\b.*\bui\b", k["cmd"])), None)
    if ui:
        return "dagrun-ui", f"the dagrun ui server (up {up(ui['etime'])})"
    return "other", (kids[0]["cmd"] if kids else pane["cmd"])[:80]


def find_family(state, wins, status):
    """The windows that belong to a case: side windows (triage, babysit = launcher `here` windows whose panes sit in
    the worktree), the windows in the worktree (the driver first, then any other) and the plan window
    `change-<case key>` (never matched by cwd: it sits in the shared main checkout). Also used by finish-window.py."""
    wt_real = os.path.realpath(state["worktreePath"])

    def sits_in_worktree(w):
        return any(under(p["path"], wt_real) for p in w["panes"] if p["cmd"] != "workmux")

    here = sorted((w for w in wins.values() if w["here"] and sits_in_worktree(w)), key=lambda w: w["bare"])
    in_wt = sorted((w for w in wins.values() if not w["here"] and sits_in_worktree(w)),
                   key=lambda w: (not any(p["id"] in status for p in w["panes"]), int(w["id"][1:])))
    key = case_info(state)[0]
    plan_name = f"change-{key.replace('/', '-')}" if key else None
    plan = next((w for w in wins.values() if plan_name and w["bare"] == plan_name), None)
    return {"key": key, "here": here, "in_worktree": in_wt, "driver": in_wt[0] if in_wt else None,
            "plan_name": plan_name, "plan": plan}


# ----- GitHub and git facts ------------------------------------------------------------------------------------

def pr_details(wt_path, branch):
    rc, out, _ = fw.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "5", "--json",
                         "number,state"], cwd=wt_path, timeout=45)
    if rc:
        return None
    try:
        prs = json.loads(out or "[]")
    except ValueError:
        return None
    pick = next((p for p in prs if p["state"] == "MERGED"), prs[0] if prs else None)
    if not pick:
        return None
    # `gh pr list` caps statusCheckRollup at 100 entries (153 showed 40 ok + 60 skipped); `gh pr view` returns them all.
    rc, out, _ = fw.run(["gh", "pr", "view", str(pick["number"]), "--json",
                         "number,state,url,mergeCommit,reviewDecision,statusCheckRollup"], cwd=wt_path, timeout=45)
    try:
        pr = json.loads(out) if rc == 0 else None
    except ValueError:
        pr = None
    if not pr:
        return None
    c = {"ok": 0, "failed": 0, "skipped": 0, "pending": 0, "no_result": 0}
    for r in pr.get("statusCheckRollup") or []:
        res = (r.get("conclusion") or r.get("state") or "").upper()
        if res in ("SUCCESS", "NEUTRAL"):
            c["ok"] += 1
        elif res == "SKIPPED":
            c["skipped"] += 1
        elif res in FAILED_CHECK:
            c["failed"] += 1
        elif not res and (r.get("status") or "").upper() == "COMPLETED":
            c["no_result"] += 1
        elif not res and not r.get("status"):
            c["no_result"] += 1
        else:
            c["pending"] += 1
    sha = ((pr.get("mergeCommit") or {}).get("oid") or "")[:7]
    return {"number": pr["number"], "state": pr["state"], "url": pr["url"], "sha": sha,
            "review": pr.get("reviewDecision") or "none", "checks": c}


def issue_state(issue):
    rc, out, _ = fw.run(["gh", "issue", "view", issue["number"], "--repo", issue["repo"], "--json", "state,title"],
                        timeout=30)
    try:
        d = json.loads(out) if rc == 0 else None
    except ValueError:
        d = None
    return d


# ----- the check -----------------------------------------------------------------------------------------------

def check(state):
    sheet = Sheet()
    rid, wt_path, branch = state["runId"], state["worktreePath"], state.get("branch") or ""
    wt_real = os.path.realpath(wt_path)
    key, case_dir, cp_path = case_info(state)
    sections, issue = checkpoint_facts(cp_path) if cp_path else ({}, None)
    self_pane = os.environ.get("TMUX_PANE")
    out = {"case": key, "case_dir": case_dir, "checkpoint": cp_path,
           "run": {"id": rid, "workflow": state.get("workflow"), "status": state.get("status")},
           "worktree": {"path": wt_path, "branch": branch, "exists": os.path.isdir(wt_path)},
           "members": [], "pr": None, "issue": None}

    # ---- the run
    if state.get("status") == "done":
        sheet.add(READY, f"run {rid}", "done")
    else:
        sheet.add(BLOCKED, f"run {rid}", f"status is '{state.get('status')}', not done")
    lock_path = os.path.join(DAGRUNNER, "active.lock")
    lock = read_json(lock_path)
    if not os.path.exists(lock_path):
        sheet.add(READY, "dagrunner lock", "active.lock not held (dagrun cleanup has nothing left to release)")
    elif lock and lock.get("runId") == rid:
        sheet.add(NEEDS, "dagrunner lock", f"this run still holds active.lock (pid {lock.get('pid')}): run `dagrun cleanup {rid}`")
    elif lock:
        sheet.add(INFO, "dagrunner lock", f"active.lock belongs to another run ({lock.get('runId')}), not this case")
    else:
        sheet.add(NEEDS, "dagrunner lock", "active.lock exists but cannot be read")

    # ---- the worktree, the PR, the dirt
    delete_ok = False
    if not out["worktree"]["exists"]:
        sheet.add(READY, "worktree", "already gone, nothing to remove")
    else:
        rc, cur, _ = fw.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt_path)
        rc2, common, _ = fw.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=wt_path)
        if rc or rc2:
            sheet.add(BLOCKED, "worktree", "cannot read git here (fails closed)")
        else:
            if cur != branch:
                sheet.add(NEEDS, "worktree", f"is on branch '{cur}', the run recorded '{branch}'")
            wt = {"handle": os.path.basename(wt_path), "branch": branch, "path": wt_path,
                  "project_path": os.path.dirname(common)}
            rc3, gitdir, _ = fw.run(["git", "rev-parse", "--absolute-git-dir"], cwd=wt_path)
            linked = (not rc3) and os.path.realpath(gitdir) != os.path.realpath(common)
            if not linked:  # the main checkout is never removed
                sheet.add(BLOCKED, "worktree", "this is the main checkout, not a linked worktree: never removed")
            merged, why_not, facts = fw.merge_state(wt)
            out["worktree"].update({"handle": wt["handle"], "project_path": wt["project_path"],
                                    "merged": bool(merged), "linked": bool(linked)})
            if merged:
                sheet.add(READY, "branch", "merged: " + "; ".join(facts))
            else:
                sheet.add(BLOCKED, "branch", "NOT merged (" + "; ".join(why_not) + "): park only, never delete")
            dirt = classify_dirt(wt_path)
            tracked, others, links = dirt if dirt else ([], [], [])
            if dirt is None:
                sheet.add(BLOCKED, "worktree", "cannot read git status (fails closed)")
            if tracked:
                sheet.add(BLOCKED, "worktree", "uncommitted changes to tracked files, never deleted: " + ", ".join(tracked[:8]))
            if others:
                sheet.add(NEEDS, "worktree", "untracked files that are not symlinks, each needs a yes: " + ", ".join(others[:8]))
            for p, target in links:
                sheet.add(READY, "worktree", f"untracked symlink {p} -> {target.replace(HOME, '~')}: only the link goes with the worktree")
            if dirt and not (tracked or others or links):
                sheet.add(READY, "worktree", "clean")
            git_lock = fw.run(["zsh", GIT_LOCK, "status"], cwd=wt_path)[1] or "unknown"
            sheet.add(READY if git_lock == "free" else BLOCKED, "git write lock", git_lock)
            # Two layers on purpose: these conditions, then the scan of every blocked/needs-you item at the end of check().
            # They overlap; each alone is enough to say no, so a mistake in one cannot make the folder deletable.
            delete_ok = bool(linked and merged and dirt is not None and not tracked and not others and state.get("status") == "done")
            pr = pr_details(wt_path, branch) if branch else None
            out["pr"] = pr
    # delete_ok is finished at the end, once the windows have been checked

    # ---- triage and babysit state on disk
    tri = read_json(os.path.join(RUNS, rid, "pr-triage", "triage-state.json"))
    if tri is None:
        sheet.add(INFO, "triage state", "no triage-state.json for this run")
    else:
        counts = {}
        for c in (tri.get("comments") or {}).values():
            counts[c.get("lifecycle") or "unknown"] = counts.get(c.get("lifecycle") or "unknown", 0) + 1
        open_ = {k: v for k, v in counts.items() if k not in RESOLVED}
        text = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no comments"
        if open_:
            sheet.add(NEEDS, "triage state", f"comments still open ({', '.join(f'{v} {k}' for k, v in open_.items())}); all: {text}")
        else:
            sheet.add(READY, "triage state", f"every comment resolved ({text})")
    # ci-babysit's run id is --run-id, else DAGRUN_RUN_ID, else the branch name with `/` turned into `-`. Started from the
    # launcher's side window it has neither, so its state is in runs/<branch>/ci-babysit/, not under the dagrunner run id.
    babysit_dir = next((d for d in (os.path.join(RUNS, x, "ci-babysit") for x in (rid, branch.replace("/", "-")) if x) if os.path.isdir(d)), None)
    if babysit_dir:
        ticks = sorted(f for f in os.listdir(babysit_dir) if f.startswith("tick-"))
        where = os.path.relpath(babysit_dir, RUNS)
        sheet.add(INFO, "babysit state", f"{where}: {len(ticks)} tick(s), last {ticks[-1]}" if ticks else f"{where}: no tick written yet")
    else:
        sheet.add(INFO, "babysit state", "no ci-babysit folder for this run or branch: only its window status is checked")

    # ---- the windows
    wins, procs, status = inventory(), process_table(), fw.agent_status()
    fam = find_family(state, wins, status)
    here_wins, other_wt_wins, driver = fam["here"], fam["in_worktree"], fam["driver"]
    plan_name, plan_win = fam["plan_name"], fam["plan"]

    def check_window(w, kind):
        who = f"{w['bare']}"
        agents = [p for p in w["panes"] if p["id"] in status]
        member = {"kind": kind, "window_id": w["id"], "session": w["session"], "name": w["name"], "label": w["bare"],
                  "self": any(p["id"] == self_pane for p in w["panes"]), "agents": []}
        for p in agents:
            member["agents"].append({"pane": p["id"], "status": status[p["id"]]})
            if p["id"] == self_pane:
                sheet.add(READY, who, "this session (the one running the check); closes last")
            elif status[p["id"]] == "working":
                sheet.add(BLOCKED, who, "agent is still working: wait or stop it")
            elif status[p["id"]] == "waiting":  # workmux: "agent needs user input"; closing would drop the question
                sheet.add(NEEDS, who, "agent is waiting for an answer from Eddie: answer it, or let it go")
            else:
                sheet.add(READY, who, f"agent idle ({status[p['id']]})")
        if not agents:
            sheet.add(INFO, who, "no tracked agent in this window")
        for p in w["panes"]:
            if p["id"] in status or p["cmd"] in fw.SHELLS:
                continue
            kind_, text = describe_process(procs, p)
            if kind_ == "dagrun-ui":
                sheet.add(INFO, who, f"pane {p['id']} runs {text}: stops with the window")
            else:
                sheet.add(NEEDS, who, f"pane {p['id']} is running something else: {text}")
        out["members"].append(member)

    for w in sorted(here_wins, key=lambda w: w["bare"]):
        check_window(w, "triage" if w["bare"].startswith("triage") else "babysit" if w["bare"].startswith("babysit") else "side")
    if plan_win:
        check_window(plan_win, "plan")
    elif plan_name:
        sheet.add(INFO, plan_name, "plan window is not open")
    if driver:
        check_window(driver, "driver")
        for w in other_wt_wins[1:]:
            sheet.add(NEEDS, w["bare"], "another window also sits in the worktree: it would lose its folder")
    else:
        sheet.add(INFO, "driver window", "no window found in the worktree")
    if not any(m["kind"] in ("triage", "babysit", "side") for m in out["members"]):
        sheet.add(INFO, "triage/babysit", "no side windows open")

    # ---- the issue and the checkpoint (for the driver's follow-up conversation)
    if issue:
        d = issue_state(issue)
        out["issue"] = {**issue, "state": d["state"] if d else "unknown", "title": d["title"] if d else ""}
    if cp_path:
        notable = [(h, len(sections[h])) for h in sections if re.search(r"follow-up|learning|lesson|left for|closed", h, re.I)]
        sheet.add(INFO, "checkpoint", "sections for the driver to read: " + (", ".join(f"{h} ({n} line{'' if n == 1 else 's'})" for h, n in notable) or "none"))
    else:
        sheet.add(INFO, "checkpoint", "no case checkpoint found (older case layout or plan outside change-companion)")

    # The worktree may be deleted only if nothing that could still be using it is blocked or waiting on Eddie: a
    # working agent, a held git write lock, a second window or an unknown process in the worktree (see NOT_ABOUT_THE_FOLDER).
    out["worktree"]["delete_ok"] = bool(delete_ok and not any(
        i["level"] in (BLOCKED, NEEDS) and i["who"] not in NOT_ABOUT_THE_FOLDER for i in sheet.items))
    out["items"] = sheet.items
    levels = {i["level"] for i in sheet.items}
    out["overall"] = ("blocked" if BLOCKED in levels else "needs-you" if NEEDS in levels else "ready")
    order = [m["label"] for m in out["members"] if m["kind"] in ("triage", "babysit", "side")]
    order += [m["label"] for m in out["members"] if m["kind"] == "plan"]
    order += [m["label"] + " (+ its panes)" for m in out["members"] if m["kind"] == "driver"]
    out["close_order"] = order + (["then the worktree"] if out["worktree"]["exists"] else [])
    return out


def render(r):
    lines = []
    head = f"Case {r['case'] or '(no change-companion key)'}  ·  run {r['run']['id']} ({r['run']['workflow']}, {r['run']['status']})  ·  branch {r['worktree']['branch']}"
    lines += [head, "-" * len(head)]
    pr = r["pr"]
    if pr:
        c = pr["checks"]
        lines.append(f"PR #{pr['number']} {pr['state']}{' ' + pr['sha'] if pr['sha'] else ''}  ·  review {pr['review']}  ·  checks {c['ok']} ok, "
                     f"{c['failed']} failed, {c['skipped']} skipped, {c['pending']} pending" + (f", {c['no_result']} without a result" if c["no_result"] else ""))
    else:
        lines.append("PR: none found for this branch (or gh failed)")
    if r["issue"]:
        i = r["issue"]
        lines.append(f"Issue {i['repo']}#{i['number']} {i['state']}  {i['title']}".rstrip())
    for level in (READY, NEEDS, BLOCKED, INFO):
        group = [i for i in r["items"] if i["level"] == level]
        if not group:
            continue
        lines += ["", TITLE[level]]
        width = min(max(len(i["who"]) for i in r["items"]), 42)
        lines += [f"  {GLYPH[level]} {i['who']:<{width}}  {i['text']}" for i in group]
    lines += ["", f"Overall: {r['overall']}.  Worktree can be deleted: {'yes' if r['worktree']['delete_ok'] else 'NO'}.",
              "Close order: " + (", ".join(r["close_order"]) or "nothing open"),
              "(read-only check: nothing was changed)"]
    return "\n".join(lines)


# ----- execute -------------------------------------------------------------------------------------------------
#
# execute closes what Eddie approved, from a FRESH check, in this order: the side windows (triage, babysit), the plan
# window, then, detached from the tmux server, the driver window and the worktree. Every destructive step is gated
# right before it runs and the gates fail closed. All side effects go through the act_* functions, so the tests can
# replace them.

class Log:
    def __init__(self, path):
        self.path = path

    def __call__(self, msg):
        if self.path:
            try:
                with open(self.path, "a") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except OSError:
                pass


def act_kill_window(wid):
    rc, _, err = fw.run(["tmux", "kill-window", "-t", wid])
    return rc == 0, err


def act_cleanup(rid):
    rc, out, err = fw.run(["dagrun", "cleanup", rid], timeout=60)
    return rc == 0, out or err


def act_launch_final(cmd):
    rc, _, err = fw.run(["tmux", "run-shell", "-b", cmd])
    return rc == 0, err


def notify(msg):
    fw.run(["tmux", "display-message", "-d", "10000", msg])


def plan_execute(r, state, tokens, timeout=180):
    """Decide what execute may do from a FRESH check `r`. Pure: no side effects.
    -> {"approved", "steps": [window kills], "refused": [{label, reason}], "cleanup": {run} | None, "final": {...} | None}"""
    rid, members, wt = state["runId"], r["members"], r["worktree"]
    by_label = {m["label"]: m for m in members}
    selected, want_worktree, unknown = [], False, []
    for t in tokens:
        if t == "worktree":
            want_worktree = True
        elif t in by_label:
            selected.append(by_label[t])
        elif any(m["kind"] == t for m in members):
            selected += [m for m in members if m["kind"] == t]
        else:
            unknown.append(t)
    if unknown:  # a typo or an invented name never turns into a guess
        raise Fail(f"not part of this case: {', '.join(unknown)}. Names: {', '.join(list(by_label) + ['worktree'])}; "
                   "the kinds triage, babysit, plan and driver also work.")
    seen, uniq = set(), []
    for m in selected:
        if m["window_id"] not in seen:
            seen.add(m["window_id"])
            uniq.append(m)
    selected = uniq
    plan = {"approved": list(tokens), "steps": [], "refused": [], "cleanup": None, "final": None}

    # Preconditions for ANY closing: the case is finished. Otherwise prefix + F parks single windows.
    pre = []
    if r["run"]["status"] != "done":
        pre.append(f"run {rid} is '{r['run']['status']}', not done")
    if wt["exists"] and wt.get("merged") is not True:
        pre.append("the branch is not merged (or git/gh could not say)")
    if pre:
        plan["refused"].append({"label": "(everything)", "reason": "; ".join(pre) + ": the case is not finished, nothing was closed"})
        return plan

    blockers = {}
    for i in r["items"]:
        if i["level"] in (BLOCKED, NEEDS):
            blockers.setdefault(i["who"], []).append(i["text"])

    side = sorted((m for m in selected if m["kind"] in SIDE_KINDS), key=lambda m: m["label"])
    for m in side + [m for m in selected if m["kind"] == "plan"]:
        why = None
        if m["self"]:
            why = "this window is the one running the command; only the driver window can close, and last"
        elif blockers.get(m["label"]):
            why = "; ".join(blockers[m["label"]])
        if why:
            plan["refused"].append({"label": m["label"], "reason": why})
        else:
            plan["steps"].append({"label": m["label"], "window_id": m["window_id"], "session": m["session"], "kind": m["kind"]})

    driver = next((m for m in members if m["kind"] == "driver"), None)
    if want_worktree or any(m["kind"] == "driver" for m in selected):
        problems = []
        if driver and blockers.get(driver["label"]):
            problems.append(f"{driver['label']}: " + "; ".join(blockers[driver["label"]]))
        if driver and driver["self"] and not driver["agents"]:  # without a tracked agent there is nothing to wait on
            problems.append("this session's agent is not tracked by workmux, so nothing can tell when its turn ends and the window would go mid-reply")
        if not wt["exists"]:
            problems.append("the worktree is already gone")
        elif not wt.get("project_path"):
            problems.append("cannot find the project checkout the worktree belongs to")
        if want_worktree and wt["exists"]:
            if not wt["delete_ok"]:
                why = [f"{i['who']}: {i['text']}" for i in r["items"] if i["level"] in (BLOCKED, NEEDS) and i["who"] not in NOT_ABOUT_THE_FOLDER]
                problems.append("it cannot be deleted now (" + ("; ".join(why) or "the check says no") + ")")
            closing = {s["window_id"] for s in plan["steps"]}
            stay = [m["label"] for m in members if m["kind"] in SIDE_KINDS and m["window_id"] not in closing]
            if stay:
                problems.append(f"{', '.join(stay)} would stay open inside the worktree")
        if problems:
            plan["refused"].append({"label": "worktree" if want_worktree else driver["label"], "reason": " | ".join(problems)})
        else:
            plan["final"] = {"mode": "remove" if want_worktree else "close", "handle": wt["handle"], "project": wt["project_path"],
                             "worktree": wt["path"], "window_id": driver["window_id"] if driver else None,
                             "wait": bool(driver and driver["agents"]), "timeout": timeout}

    # `dagrun cleanup` removes active.lock WHOEVER holds it (and, for a non-adopted run, the worktree itself):
    # only for an adopted, done run whose own lock it is.
    lock_path = os.path.join(DAGRUNNER, "active.lock")
    if plan["final"] and plan["final"]["mode"] == "remove" and os.path.exists(lock_path):
        lock = read_json(lock_path)
        if lock and lock.get("runId") == rid:
            if state.get("worktreeOrigin") == "adopted" and state.get("status") == "done":
                plan["cleanup"] = {"run": rid}
            else:
                plan["refused"].append({"label": "dagrunner lock", "reason": f"run {rid} still holds active.lock, but `dagrun cleanup` is only "
                                        "safe for an adopted, done run (for another kind it removes the worktree itself): release it by hand"})
    return plan


def window_gate(step):
    """Last look right before a window is killed. -> None (go), 'gone', or a reason to refuse."""
    w = inventory().get(step["window_id"])
    if not w:
        return "gone"
    if w["bare"] != step["label"]:
        return f"the window changed (it is now named '{w['bare']}'): not touched"
    me = os.environ.get("TMUX_PANE")
    if any(p["id"] == me for p in w["panes"]):
        return "this window is the one running the command"
    status = fw.agent_status()
    for p in w["panes"]:
        if status.get(p["id"]) in ("working", "waiting"):
            return f"the agent is {status[p['id']]} now"
    return None


def final_command(f, rid, log_path):
    argv = [sys.executable, os.path.abspath(__file__), "_final", "--run", rid, "--handle", f["handle"], "--project", f["project"],
            "--worktree", f["worktree"], "--mode", f["mode"], "--timeout", str(f["timeout"])]
    if f["window_id"]:
        argv += ["--window", f["window_id"]]
    if not f["wait"]:
        argv.append("--no-wait")
    if log_path:
        argv += ["--log", log_path]
    env = ["env", f"CLOSE_CASE_DAGRUNNER_HOME={DAGRUNNER}"] if os.environ.get("CLOSE_CASE_DAGRUNNER_HOME") else []
    return f"cd {shlex.quote(f['project'])} && " + " ".join(shlex.quote(a) for a in env + argv) + f" >> {shlex.quote(log_path or os.devnull)} 2>&1"


def execute(target, tokens, dry_run=False, timeout=180):
    state = resolve_run(target)
    rid = state["runId"]
    r = check(state)
    plan = plan_execute(r, state, tokens, timeout)
    case_dir = r["case_dir"]
    log_path = os.path.join(case_dir, "close-case.log") if case_dir and os.path.isdir(case_dir) else None
    log = Log(log_path)
    res = {"case": r["case"], "run": rid, "approved": plan["approved"], "dry_run": dry_run, "steps": [],
           "refused": plan["refused"], "cleanup": None, "final": None, "log": log_path}
    if not dry_run:
        log(f"execute: approved={','.join(tokens)}; refused={[x['label'] for x in plan['refused']]}")

    if plan["cleanup"]:
        if dry_run:
            res["cleanup"] = {"result": "would run", "command": f"dagrun cleanup {rid}"}
        else:
            ok, msg = act_cleanup(rid)
            res["cleanup"] = {"result": "released" if ok else "FAILED", "detail": (msg or "").strip()[:200]}
            log(f"dagrun cleanup {rid}: {res['cleanup']['result']} {res['cleanup']['detail']}")

    failed = False
    for s in plan["steps"]:
        entry = {"label": s["label"], "window_id": s["window_id"]}
        if dry_run:
            entry["result"] = "would close"
        else:
            gate = window_gate(s)
            if gate == "gone":
                entry["result"] = "already gone"
            elif gate:
                entry.update(result="refused", reason=gate)
                failed = True
            else:
                ok, err = act_kill_window(s["window_id"])
                if ok and s["window_id"] not in inventory():
                    entry["result"] = "closed"
                else:
                    entry.update(result="FAILED", reason=err or "the window is still there")
                    failed = True
            log(f"window {s['label']} ({s['window_id']}): {entry['result']} {entry.get('reason', '')}")
        res["steps"].append(entry)

    f = plan["final"]
    if f:
        what = ("removes the worktree, its branch and the driver window" if f["mode"] == "remove" else "closes the driver window (the worktree stays)")
        if failed and f["mode"] == "remove":
            res["final"] = {"result": "not started", "reason": "a window that sits in the worktree is still open"}
        elif dry_run:
            res["final"] = {"result": "would start", "what": what, "waits_for_driver": f["wait"], "timeout": f["timeout"]}
        else:
            ok, err = act_launch_final(final_command(f, rid, log_path))
            res["final"] = {"result": "started" if ok else "FAILED", "what": what, "waits_for_driver": f["wait"],
                            "timeout": f["timeout"], **({} if ok else {"reason": err})}
            log(f"final step {res['final']['result']}: {what}")
    return res


def render_execute(res):
    lines = [f"Close case {res['case'] or res['run']}  ·  approved: {', '.join(res['approved'])}" + ("   (DRY RUN: nothing was changed)" if res["dry_run"] else ""), ""]
    if res["cleanup"]:
        lines.append(f"  {'✓' if res['cleanup']['result'] in ('released', 'would run') else '✗'} dagrun cleanup {res['run']}: {res['cleanup']['result']} {res['cleanup'].get('detail', '')}".rstrip())
    for s in res["steps"]:
        ok = s["result"] in ("closed", "would close", "already gone")
        lines.append(f"  {'✓' if ok else '✗'} {s['label']}: {s['result']}" + (f" ({s['reason']})" if s.get("reason") else ""))
    for x in res["refused"]:
        lines.append(f"  ✗ {x['label']}: REFUSED, {x['reason']}")
    f = res["final"]
    if f:
        mark = "✓" if f["result"] in ("started", "would start") else "✗"
        lines.append(f"  {mark} driver window + worktree: {f['result']}" + (f" ({f['reason']})" if f.get("reason") else ""))
        if f["result"] in ("started", "would start"):
            lines.append(f"      The last step {f['what']}. It waits for the driver agent to finish its turn"
                         f"{' (up to ' + str(f['timeout']) + ' s)' if f.get('waits_for_driver') else ''}, checks everything again, and removes nothing if a check fails.")
            lines.append("      Do not start new work in this window.")
    if res["log"]:
        lines += ["", f"  log: {res['log']}"]
    return "\n".join(lines)


def final_gates(state, a):
    """The checks right before the driver window and worktree go. -> a list of problems (empty = go)."""
    problems = []
    if state.get("status") != "done":
        problems.append(f"run is '{state.get('status')}', not done")
    wt_path = a.worktree
    if os.path.realpath(state.get("worktreePath") or "") != os.path.realpath(wt_path) or os.path.basename(wt_path) != a.handle:
        problems.append("the run's worktree and the arguments disagree")
    status = fw.agent_status()
    drv = inventory().get(a.window) if a.window else None
    for p in (drv["panes"] if drv else []):
        if status.get(p["id"]) in ("working", "waiting"):
            problems.append(f"the driver agent is {status[p['id']]} again")
    if a.mode == "remove":
        if not os.path.isdir(wt_path):
            problems.append("the worktree is already gone")
            return problems
        rc, gitdir, _ = fw.run(["git", "rev-parse", "--absolute-git-dir"], cwd=wt_path)
        rc2, common, _ = fw.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=wt_path)
        if rc or rc2 or os.path.realpath(gitdir) == os.path.realpath(common):
            problems.append("not a linked worktree (or git cannot say): never removed")
        wt = {"handle": a.handle, "branch": state.get("branch") or "", "path": wt_path, "project_path": a.project}
        merged, why, _ = fw.merge_state(wt)
        if not merged:
            problems.append("not merged: " + "; ".join(why))
        dirt = classify_dirt(wt_path)
        if dirt is None:
            problems.append("cannot read git status")
        elif dirt[0] or dirt[1]:
            problems.append("uncommitted or untracked files: " + ", ".join((dirt[0] + dirt[1])[:5]))
        sib = fw.sibling_windows(wt, a.window)
        if sib is None:
            problems.append("cannot tell which other windows sit in the worktree")
        elif sib:
            problems.append("other windows still sit in the worktree: " + ", ".join(sib))
        lock = fw.run(["zsh", GIT_LOCK, "status"], cwd=wt_path)[1] or "unknown"
        if lock != "free":
            problems.append(f"git write lock: {lock}")
    return problems


def run_final(a):
    """The detached last step: wait for the driver's turn to end, check everything again, then remove or close."""
    log = Log(a.log)

    def stop(msg):
        log(f"NOT DONE: {msg}")
        notify(f"close-case: {a.handle} NOT removed: {msg}")
        return 1

    log(f"final step: mode={a.mode} handle={a.handle} window={a.window or '-'} wait={'no' if a.no_wait else str(a.timeout) + 's'}")
    state = read_json(os.path.join(RUNS, a.run or "", "state.json"))
    if not state:
        return stop(f"cannot read the state of run {a.run}")
    if a.window and not a.no_wait:
        rc, out, err = fw.run(["workmux", "wait", a.handle, "--status", "done", "--timeout", str(a.timeout)],
                              cwd=a.project, timeout=a.timeout + 30)
        if rc:
            return stop(f"the driver agent did not finish within {a.timeout}s ({(err or out).strip()[:100]})")
    problems = final_gates(state, a)
    if problems:
        return stop("; ".join(problems))
    cmd = ["workmux", "remove", "-f", a.handle] if a.mode == "remove" else ["workmux", "close", a.handle]
    rc, out, err = fw.run(cmd, cwd=a.project, timeout=120)
    log(f"{' '.join(cmd)} -> {rc} {(out or err).strip()[:200]}")
    if rc:
        return stop(f"{' '.join(cmd)} failed: {(err or out).strip()[:100]}")
    if a.mode == "remove":
        # `workmux remove` only SCHEDULES the destructive part ("Scheduled removal": it waits for the window to close first)
        # and returns at once, so the folder can outlive the command. The first real close (153, 2026-11-18) logged a false
        # "NOT DONE" because it looked immediately. Wait for the folder to go before judging.
        t0 = time.time()
        while os.path.exists(a.worktree) and time.time() - t0 < REMOVE_WAIT:
            time.sleep(REMOVE_POLL)
        if os.path.exists(a.worktree):
            log(f"NOT DONE: workmux scheduled the removal but the worktree is still there {REMOVE_WAIT:.0f}s later: check it by hand")
            notify(f"close-case: {a.handle}: workmux scheduled the removal but the folder is still there after {REMOVE_WAIT:.0f}s; check it by hand")
            return 1
        log(f"worktree gone {time.time() - t0:.0f}s after workmux returned")
    log("done")
    notify(f"close-case: {a.handle} {'removed' if a.mode == 'remove' else 'window closed'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Check, then close, a whole DagRunner change case (see the module docstring).")
    ap.add_argument("command", choices=["check", "execute", "_final"])
    ap.add_argument("target", nargs="?", help="run id, case key (65755, pdp-3517/153) or worktree dir; default: the current checkout")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--approve", action="append", default=[], metavar="NAME[,NAME...]",
                    help="execute: what to close: window labels, the kinds triage/babysit/plan/driver, and `worktree`")
    ap.add_argument("--dry-run", action="store_true", help="execute: show what would happen, change nothing")
    ap.add_argument("--timeout", type=int, default=180, help="execute: seconds the last step waits for the driver agent to finish")
    for opt in ("--run", "--handle", "--project", "--worktree", "--window", "--mode", "--log"):  # internal, for the detached last step
        ap.add_argument(opt, help=argparse.SUPPRESS)
    ap.add_argument("--no-wait", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    try:
        if args.command == "check":
            result = check(resolve_run(args.target))
            print(json.dumps(result, indent=2) if args.json else render(result))
        elif args.command == "execute":
            tokens = [t.strip() for a in args.approve for t in a.split(",") if t.strip()]
            if not tokens:
                raise Fail("name what to close: --approve triage,babysit,plan,worktree (labels work too; `check` shows them).")
            res = execute(args.target, tokens, args.dry_run, args.timeout)
            print(json.dumps(res, indent=2) if args.json else render_execute(res))
            bad = res["refused"] or any(s["result"] in ("refused", "FAILED") for s in res["steps"]) or \
                (res["final"] and res["final"]["result"] in ("FAILED", "not started")) or (res["cleanup"] and res["cleanup"]["result"] == "FAILED")
            sys.exit(1 if bad else 0)
        else:
            sys.exit(run_final(args))
    except Fail as e:
        print(f"close-case: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
