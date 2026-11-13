#!/usr/bin/env python3
"""tmux-resurrect companion: bring AI agents back after a restart, and keep the workmux sidebar sane.

tmux-resurrect (+ continuum) rebuilds sessions, windows, panes and directories, but an agent pane
(claude, copilot, opencode) only comes back as a bare shell. This script closes that gap. It is wired
into tmux.conf as three resurrect hooks:

  @resurrect-hook-post-save-all     ->  agents.py save
  @resurrect-hook-pre-restore-all   ->  agents.py pre-restore
  @resurrect-hook-post-restore-all  ->  agents.py restore

save         Looks at every pane, finds the agent running in it and the exact conversation it has open,
             and writes `agents_<timestamp>.json` next to the resurrect save file `last` points to.
pre-restore  On a fresh server, turns the workmux sidebar OFF. With it on, workmux adds a sidebar pane to every window
             resurrect creates, which doubles the sidebars and wrecks the saved pane sizes.
restore      (fresh server) kills the sidebar shells resurrect brought back, removes stale `term-<pid>` sessions, types
             the resume command for each saved agent into its restored pane, then turns the sidebar back ON.

What identifies each conversation (nothing here is guessed from a pane title):
  claude    <config dir>/sessions/<pid>.json, written by the running Claude: session id and profile dir.
            The transcript's last `permission-mode` and assistant `model` are replayed as flags, so a
            session that was in bypass mode comes back in bypass mode (and one that was not, does not).
  copilot   ~/.copilot/session-state/<id>/inuse.<pid>.lock, then the process log, then the pane title.
            The flags (--allow-all, --model ...) are copied from the running process.
  opencode  best effort: its newest session for the directory, only when one pane uses that directory.

The profile env (CLAUDE_CONFIG_DIR, NO_ANON_PING, COPILOT_HOME, ...) comes from the `agents:` block of
~/.config/workmux/config.yaml, so work and personal profiles never get mixed up.

Not restored on purpose: panes inside a workmux worktree (`worktree_dir`). `change run` uses the
claude-driver profile, whose `--setting-sources user` cannot be read back from a running process, so those
are left to `workmux resurrect`. They are counted in the log.

Safety: sidebar surgery and session cleanup run only on a FRESH server (pre-restore saw it younger than
five minutes and set @agents-restore-prep). A manual restore on a live server (prefix + C-r) or a hand-run
`agents.py restore` only resumes agents, only into idle shell panes, and never into a conversation that is
already open somewhere.

Manual use (from any shell, e.g. after a Ghostty crash where the layout survived):
  agents.py save --dry-run                  print what would be saved from the live server
  agents.py restore --dry-run               print what would be typed where, change nothing
  agents.py restore                         resume the agents of the snapshot `last` points to
  agents.py restore --file <agents_*.json>  use a specific snapshot (agents_latest_nonempty.json = the
                                            newest save that still had agents, in case a later autosave was empty)

Log: <resurrect dir>/agents.log
"""
import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
# tmux's global PATH may lack ~/.local/bin (claude) and Homebrew (workmux, python with PyYAML).
os.environ["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
try:
    import yaml  # noqa: F401  (needed to read the workmux profiles)
except ImportError:
    # A hook can get a PATH whose python3 is Apple's (no PyYAML). Without the profiles the resume
    # line would lose NO_ANON_PING and friends, so re-run under Homebrew's python once.
    if not os.environ.get("AGENTS_REEXEC"):
        for _py in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
            if os.path.exists(_py):
                os.environ["AGENTS_REEXEC"] = "1"
                os.execv(_py, [_py, *sys.argv])
WORKMUX_CONFIG = os.environ.get("AGENTS_WORKMUX_CONFIG", f"{HOME}/.config/workmux/config.yaml")
SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh"}
# Same list as `claude --help`. A mode read from a transcript that is not here is dropped, never guessed.
PERMISSION_MODES = {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
RESUME_FLAGS = {"-r", "--resume", "--continue", "-c", "--session-id", "-s", "--session"}
TRANSCRIPT_TAIL = 4 * 1024 * 1024  # bytes read from the end of a transcript
SCHEMA = 1


# --------------------------------------------------------------------------- plumbing

def run(argv, timeout=15):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def tmux(*args):
    """stdout (stripped) on success, None on failure. Uses $TMUX like any tmux client."""
    r = run(["tmux", *args])
    return r.stdout.strip("\n") if r.returncode == 0 else None


def tmux_option(name):
    return (tmux("show-option", "-gqv", name) or "").strip()


def resurrect_dir():
    d = os.environ.get("AGENTS_RESURRECT_DIR") or tmux_option("@resurrect-dir")
    if d:
        return os.path.expanduser(d.replace("$HOME", HOME))
    legacy = f"{HOME}/.tmux/resurrect"
    return legacy if os.path.isdir(legacy) else f"{HOME}/.local/share/tmux/resurrect"


_log_path = None


def log(msg):
    global _log_path
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, file=sys.stderr)
    try:
        _log_path = _log_path or os.path.join(resurrect_dir(), "agents.log")
        if os.path.exists(_log_path) and os.path.getsize(_log_path) > 200_000:
            os.replace(_log_path, _log_path + ".1")
        with open(_log_path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_workmux_agents():
    try:
        import yaml
        with open(WORKMUX_CONFIG) as f:
            doc = yaml.safe_load(f) or {}
        return doc.get("agents") or {}, doc.get("worktree_dir") or ""
    except Exception as e:  # missing PyYAML or config: restore still works with config-dir-only env
        log(f"warn: cannot read {WORKMUX_CONFIG}: {e}")
        return {}, ""


def panes():
    fmt = "\t".join(f"#{{{k}}}" for k in (
        "session_name", "window_index", "window_name", "pane_index", "pane_id",
        "pane_pid", "pane_current_path", "pane_current_command", "pane_title", "session_attached"))
    out = tmux("list-panes", "-a", "-F", fmt)
    rows = []
    for line in (out or "").splitlines():
        f = line.split("\t")
        if len(f) != 10:
            continue
        rows.append(dict(session=f[0], window=int(f[1]), window_name=f[2], index=int(f[3]), id=f[4],
                         pid=int(f[5]), cwd=f[6], cmd=f[7], title=f[8], attached=int(f[9] or 0)))
    return rows


def process_table():
    """{pid: (ppid, args)} and {ppid: [pids]} from one `ps` call."""
    procs, kids = {}, {}
    for line in run(["ps", "-axo", "pid=,ppid=,args="]).stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if m:
            pid, ppid = int(m[1]), int(m[2])
            procs[pid] = (ppid, m[3])
            kids.setdefault(ppid, []).append(pid)
    return procs, kids


def subtree(pid, kids):
    """pid and all its descendants, shallowest first."""
    out, queue = [], [pid]
    while queue:
        p = queue.pop(0)
        out.append(p)
        queue.extend(kids.get(p, []))
    return out


def argv_of(args):
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


# --------------------------------------------------------------------------- finding agents

def encode_project(path):
    """How Claude names a project folder: every non-alphanumeric character becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_cwd(transcript, candidates):
    """The directory a transcript's project folder stands for, or None if it cannot be told.
    `claude --resume <id>` looks the conversation up by directory, so the resume line must run there."""
    folder = os.path.basename(os.path.dirname(transcript))
    heads = []
    try:
        with open(transcript) as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                try:
                    heads.append(json.loads(line).get("cwd"))
                except (ValueError, AttributeError):
                    pass
    except OSError:
        pass
    for c in [*candidates, *heads]:
        if c and encode_project(c) == folder and os.path.isdir(c):
            return c
    return None


def claude_agent(pids, pane_cwd):
    for pid in pids:
        for path in glob.glob(f"{HOME}/.claude*/sessions/{pid}.json"):
            try:
                with open(path) as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            if s.get("kind") != "interactive" or s.get("pid") != pid or not s.get("sessionId"):
                continue
            config_dir = os.path.dirname(os.path.dirname(path))
            rec = dict(kind="claude", session_id=s["sessionId"], config_dir=config_dir)
            rec.update(transcript_state(config_dir, s["sessionId"]))
            transcript = rec.pop("_path", None)
            where = project_cwd(transcript, [pane_cwd, s.get("cwd")]) if transcript else None
            if where:
                rec["resume_cwd"] = where
            return rec
    return None


def transcript_state(config_dir, sid):
    """Last permission mode and model from the tail of the conversation transcript."""
    found = glob.glob(f"{config_dir}/projects/*/{sid}.jsonl")
    if not found:
        return {"transcript": False}
    out = {"transcript": True, "_path": found[0]}
    try:
        with open(found[0], "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TRANSCRIPT_TAIL))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return out
    lines = data.splitlines()
    if size > TRANSCRIPT_TAIL:
        lines = lines[1:]  # first line is cut mid-way
    for line in reversed(lines):
        if "permission_mode" in out and "model" in out:
            break
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if "permission_mode" not in out and o.get("type") == "permission-mode":
            mode = o.get("permissionMode")
            if mode in PERMISSION_MODES:
                out["permission_mode"] = mode
        msg = o.get("message")
        if "model" not in out and o.get("type") == "assistant" and isinstance(msg, dict):
            model = msg.get("model")
            if model and not model.startswith("<"):
                out["model"] = model
    return out


def copilot_home():
    return os.environ.get("COPILOT_HOME") or f"{HOME}/.copilot"


def copilot_agent(pids, procs, pane):
    argv = None
    for pid in pids:
        a = argv_of(procs[pid][1])
        for i in (0, 1):  # `copilot ...` or an interpreter running a `copilot` script
            if len(a) > i and os.path.basename(a[i]) == "copilot":
                argv = a[i:]
                break
        if argv:
            break
    if argv is None:
        return None
    base = copilot_home()
    sid = None
    for pid in pids:
        hits = glob.glob(f"{base}/session-state/*/inuse.{pid}.lock")
        if hits:
            sid = os.path.basename(os.path.dirname(hits[0]))
            break
    if not sid:
        for pid in pids:
            for logf in sorted(glob.glob(f"{base}/logs/process-*-{pid}.log"), reverse=True):
                try:
                    ids = re.findall(r"Registering foreground session: ([0-9a-f-]{36})", open(logf).read())
                except OSError:
                    continue
                if ids:
                    sid = ids[-1]
                    break
            if sid:
                break
    if not sid:  # last resort: the pane title is the session name; take it only if one session matches
        name = re.sub(r"\s+-\s+GitHub Copilot$", "", pane["title"]).strip()
        cands = []
        for ws in glob.glob(f"{base}/session-state/*/workspace.yaml"):
            try:
                txt = open(ws).read()
            except OSError:
                continue
            n = re.search(r"^name:\s*(.+)$", txt, re.M)
            c = re.search(r"^cwd:\s*(.+)$", txt, re.M)
            if n and c and n[1].strip("'\" ") == name and c[1].strip("'\" ") == pane["cwd"]:
                cands.append(os.path.dirname(ws))
        if len(cands) == 1:
            sid = os.path.basename(cands[0])
    rec = dict(kind="copilot", session_id=sid, argv=strip_resume(argv[1:]))
    if sid:  # the session remembers the directory it ran in; resume from there
        try:
            m = re.search(r"^cwd:\s*(.+)$", open(f"{base}/session-state/{sid}/workspace.yaml").read(), re.M)
            if m and os.path.isdir(m[1].strip("'\" ")):
                rec["resume_cwd"] = m[1].strip("'\" ")
        except OSError:
            pass
    return rec


def opencode_agent(pids, procs, pane):
    for pid in pids:
        a = argv_of(procs[pid][1])[:2]
        if any(os.path.basename(x).startswith("opencode") for x in a):
            env = run(["ps", "eww", "-p", str(pid)]).stdout
            perm = re.search(r"OPENCODE_PERMISSION=(\S+)", env)
            return dict(kind="opencode", session_id=None, permission=perm[1] if perm else None)
    return None


def strip_resume(args):
    """Drop any resume/continue/session flag (and its value) from an argv."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in RESUME_FLAGS:
            skip = not (a == "--continue" or a == "-c")
        elif any(a.startswith(f + "=") for f in RESUME_FLAGS if f.startswith("--")):
            pass
        else:
            out.append(a)
    return out


def resolve_opencode_sessions(recs):
    """opencode has no pid -> session link: use the newest session of the directory, if unambiguous."""
    mine = [r for r in recs if r["kind"] == "opencode"]
    if not mine:
        return
    r = run(["opencode", "session", "list", "--format", "json", "-n", "50"])
    try:
        sessions = json.loads(r.stdout)
    except ValueError:
        return
    for rec in mine:
        same = [x for x in mine if x["cwd"] == rec["cwd"]]
        cands = sorted((s for s in sessions if s.get("directory") == rec["cwd"]),
                       key=lambda s: s.get("updated", 0), reverse=True)
        if len(same) == 1 and cands:
            rec["session_id"] = cands[0]["id"]


def collect():
    """Agents and sidebar panes in the live tmux server."""
    procs, kids = process_table()
    agents, sidebars = [], []
    for p in panes():
        pids = subtree(p["pid"], kids)
        where = dict(session=p["session"], window=p["window"], window_name=p["window_name"],
                     index=p["index"], cwd=p["cwd"])
        if p["cmd"] == "workmux" or any("_sidebar-run" in procs[x][1] for x in pids if x in procs):
            sidebars.append(dict(where))
            continue
        rec = claude_agent(pids, p["cwd"]) or copilot_agent(pids, procs, p) or opencode_agent(pids, procs, p)
        if rec:
            agents.append({**where, **rec})
    resolve_opencode_sessions(agents)
    return agents, sidebars


# --------------------------------------------------------------------------- save

def last_target(d):
    link = os.path.join(d, "last")
    return os.path.basename(os.path.realpath(link)) if os.path.exists(link) else None


def sidecar_for(resurrect_file):
    m = re.match(r"tmux_resurrect_(.+)\.txt$", resurrect_file or "")
    return f"agents_{m[1]}.json" if m else None


def cmd_save(args):
    agents, sidebars = collect()
    doc = dict(version=SCHEMA, saved_at=int(time.time()), agents=agents, sidebars=sidebars)
    if args.dry_run:
        print(json.dumps(doc, indent=2))
        return 0
    d = resurrect_dir()
    target = last_target(d)
    name = sidecar_for(target)
    if not name:
        log("save: no resurrect `last` file yet, nothing to pair the agent map with")
        return 0
    doc["resurrect_file"] = target
    write_json(os.path.join(d, name), doc)
    if agents:
        write_json(os.path.join(d, "agents_latest_nonempty.json"), doc)  # survives an empty autosave
    for old in glob.glob(os.path.join(d, "agents_2*.json")):  # prune sidecars whose save file was removed
        if not os.path.exists(os.path.join(d, "tmux_resurrect_" + os.path.basename(old)[7:-5] + ".txt")):
            os.remove(old)
    resumable = sum(1 for a in agents if a.get("session_id"))
    log(f"save: {name}: {len(agents)} agent pane(s) ({resumable} resumable), {len(sidebars)} sidebar pane(s)")
    return 0


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- restore

FRESH_SECONDS = int(os.environ.get("AGENTS_FRESH_SECONDS", "300"))  # younger = the server a boot-time restore is rebuilding
PREP_OPTION = "@agents-restore-prep"  # set by pre-restore when it turned the sidebar off, read by restore


def server_age():
    t = tmux("display-message", "-p", "#{start_time}")
    return time.time() - int(t) if t and t.isdigit() else None


def settle(timeout=6.0):
    """With @resurrect-capture-pane-contents on, a restored pane first runs `cat <saved contents>; exec $SHELL`.
    Wait until none of them is still printing, so every target is an idle shell."""
    end = time.time() + timeout
    while time.time() < end:
        if not any(p["cmd"] == "cat" for p in panes()):
            return
        time.sleep(0.25)


def cmd_pre_restore(_args):
    age = server_age()
    if age is None or age > FRESH_SECONDS:
        tmux("set-option", "-gu", PREP_OPTION)
        log(f"pre-restore: server is {'?' if age is None else int(age)}s old: a manual restore, "
            "leaving the sidebar, sessions and panes alone (agents are still resumed)")
        return 0
    tmux("set-option", "-g", PREP_OPTION, "1")
    run(["workmux", "sidebar", "off"])
    log("pre-restore: fresh server, workmux sidebar off")
    return 0


def sidebars_from_resurrect_file(path):
    """Fallback for saves made before the sidecar existed: panes whose command was `workmux`."""
    out = []
    try:
        for line in open(path):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 11 and f[0] == "pane" and f[9] == "workmux":
                out.append(dict(session=f[1], window=int(f[2]), index=int(f[5]), cwd=f[7].lstrip(":")))
    except (OSError, ValueError):
        pass
    return out


def build_line(rec, workmux_agents, pane_cwd):
    """The shell line that resumes this conversation, with the right profile env."""
    env, argv = {}, None
    kind = rec["kind"]
    sid = rec["session_id"]

    def profile_env(match):
        merged = {}
        for a in workmux_agents.values():
            if match(a):
                merged.update({k: str(v) for k, v in (a.get("env") or {}).items()})
                break
        return merged

    if kind == "claude":
        cdir = os.path.realpath(rec["config_dir"])
        env = profile_env(lambda a: a.get("type") == "claude"
                          and os.path.realpath(str((a.get("env") or {}).get("CLAUDE_CONFIG_DIR", ""))) == cdir)
        env["CLAUDE_CONFIG_DIR"] = rec["config_dir"]
        argv = ["claude"]
        if rec.get("permission_mode"):
            argv += ["--permission-mode", rec["permission_mode"]]
        if rec.get("model"):
            argv += ["--model", rec["model"]]
        argv += ["--resume", sid]
    elif kind == "copilot":
        env = profile_env(lambda a: os.path.basename(str(a.get("command", ""))) == "copilot")
        argv = ["copilot", *rec.get("argv", []), "--resume", sid]
    elif kind == "opencode":
        want = rec.get("permission")
        pick = None
        for a in workmux_agents.values():
            if a.get("type") == "opencode":
                if want and str((a.get("env") or {}).get("OPENCODE_PERMISSION", "")) == want:
                    pick = a
                    break
                pick = pick or a
        if not pick:
            return None
        env = {k: str(v) for k, v in (pick.get("env") or {}).items()}
        argv = [str(pick["command"]), *map(str, pick.get("args") or []), "--session", sid]
    if argv is None:
        return None
    line = shlex.join((["env", *[f"{k}={v}" for k, v in env.items()]] if env else []) + argv)
    where = rec.get("resume_cwd")  # conversations are looked up by directory: resume from where it started
    if where and os.path.isdir(where) and os.path.realpath(where) != os.path.realpath(pane_cwd):
        line = f"cd -- {shlex.quote(where)} && {line}"
    return line


def find_target(rec, live, claimed):
    """The restored pane for a saved agent: same slot first, then a shell with the same cwd in the window."""
    same = [p for p in live if (p["session"], p["window"], p["index"]) == (rec["session"], rec["window"], rec["index"])]
    near = [p for p in live if p["session"] == rec["session"] and p["window"] == rec["window"]
            and p["cwd"] == rec["cwd"]]
    for p in same + near:
        if p["id"] not in claimed and p["cmd"].lstrip("-") in SHELLS:
            return p
    return None


def worktree_root(cfg_value):
    root = os.path.expanduser(cfg_value.split("{")[0]).rstrip("/") if cfg_value else ""
    return root or None


def cmd_restore(args):
    d = resurrect_dir()
    path = args.file or os.path.join(d, sidecar_for(last_target(d)) or "-")
    doc = None
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        log(f"restore: no agent map at {path}")
    agents = (doc or {}).get("agents", [])
    wm_agents, wt_dir = load_workmux_agents()
    wt_root = worktree_root(wt_dir)
    # Sidebar surgery and session cleanup only happen when pre-restore saw a fresh server and turned the
    # sidebar off. A manual restore on a live server (prefix + C-r) or a hand-run `agents.py restore` only
    # resumes agents, and never into a conversation that is already open somewhere.
    prep = tmux_option(PREP_OPTION) == "1"
    dry = args.dry_run
    if prep:
        settle()
    live = panes()
    running = {a["session_id"]: a for a in collect()[0] if a.get("session_id")}

    # 1. Resolve every target pane by id BEFORE killing anything: killing shifts pane indexes.
    plan, skipped, claimed = [], [], set()
    for rec in agents:
        if wt_root and (rec["cwd"] == wt_root or rec["cwd"].startswith(wt_root + "/")):
            skipped.append((rec, "worktree window: use `workmux resurrect`"))
        elif not rec.get("session_id"):
            skipped.append((rec, "no session id found when saved"))
        elif rec["kind"] == "claude" and not rec.get("transcript", True):
            skipped.append((rec, "empty conversation (nothing to resume)"))
        elif rec["session_id"] in running:
            r = running[rec["session_id"]]
            skipped.append((rec, f"already open in {r['session']}:{r['window']}.{r['index']}"))
        else:
            target = find_target(rec, live, claimed)
            line = build_line(rec, wm_agents, target["cwd"]) if target else None
            if not target:
                skipped.append((rec, "no idle shell pane at that spot (already running something?)"))
            elif not line:
                skipped.append((rec, "no profile to build a command from"))
            else:
                claimed.add(target["id"])
                plan.append((rec, target, line))

    doomed, stale_terms = [], []  # sidebar shells resurrect brought back, and abandoned term-<pid> sessions
    if prep:
        saved = (doc or {}).get("sidebars")
        if saved is None:
            saved = sidebars_from_resurrect_file(os.path.realpath(os.path.join(d, "last")))
        per_window = {}
        for p in live:
            per_window.setdefault((p["session"], p["window"]), []).append(p)
        for sb in saved:
            win = per_window.get((sb["session"], sb["window"]), [])
            hit = [p for p in win if p["index"] == sb["index"]]
            # Guard: only a plain shell in the saved directory is ever killed, and never a window's last pane.
            if hit and hit[0]["cmd"].lstrip("-") in SHELLS and hit[0]["cwd"] == sb["cwd"] and len(win) > 1 \
                    and hit[0]["id"] not in claimed:
                doomed.append(hit[0])
        keep = {rec["session"] for rec in agents} | {t["session"] for _, t, _ in plan}
        # no agent map (doc is None): cannot tell what a term-<pid> session held, so leave them all
        by_session = {}
        for p in live:
            by_session.setdefault(p["session"], []).append(p)
        for name, ps in ({} if doc is None else by_session).items():
            if re.fullmatch(r"term-\d+", name) and name not in keep and all(p["attached"] == 0 for p in ps) \
                    and all(p["cmd"].lstrip("-") in SHELLS | {"workmux"} for p in ps):
                stale_terms.append(name)

    if dry:
        print(f"snapshot: {path}")
        print(f"mode: {'fresh restore (sidebar surgery and session cleanup on)' if prep else 'agents only'}")
        for rec, target, line in plan:
            print(f"  type into {rec['session']}:{rec['window']}.{rec['index']} ({rec['window_name']}, pane {target['id']}):\n    {line}")
        for rec, why in skipped:
            print(f"  skip {rec['session']}:{rec['window']}.{rec['index']} {rec['kind']}: {why}")
        for p in doomed:
            print(f"  kill saved sidebar shell {p['session']}:{p['window']}.{p['index']} ({p['id']})")
        for name in stale_terms:
            print(f"  kill stale session {name}")
        return 0

    try:
        for p in doomed:
            tmux("kill-pane", "-t", p["id"])
        for name in stale_terms:
            tmux("kill-session", "-t", f"={name}")
        for rec, target, line in plan:
            tmux("send-keys", "-t", target["id"], "-l", line)
            tmux("send-keys", "-t", target["id"], "Enter")
    finally:
        if prep:
            run(["workmux", "sidebar", "on"])
            tmux("set-option", "-gu", PREP_OPTION)
    for rec, why in skipped:
        log(f"restore: skipped {rec['session']}:{rec['window_name']} ({rec['kind']}): {why}")
    log(f"restore: {'fresh' if prep else 'agents-only'}: resumed {len(plan)} agent(s), skipped {len(skipped)}, "
        f"removed {len(doomed)} sidebar shell(s) and {len(stale_terms)} stale term session(s)")
    tmux("display-message", f"agents resumed: {len(plan)}, skipped: {len(skipped)} (see agents.log)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save", help="write the agent map next to the latest resurrect save")
    s.add_argument("--dry-run", action="store_true", help="print the map instead of writing it")
    sub.add_parser("pre-restore", help="resurrect hook: on a fresh server, turn the workmux sidebar off")
    r = sub.add_parser("restore", help="resume saved agents into idle shell panes (hook, or by hand)")
    r.add_argument("--file", help="agents_*.json snapshot to use (default: the one paired with `last`)")
    r.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args()
    try:
        return {"save": cmd_save, "pre-restore": cmd_pre_restore, "restore": cmd_restore}[args.cmd](args)
    except Exception as e:  # a hook must never break resurrect itself
        log(f"{args.cmd}: error: {type(e).__name__}: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
