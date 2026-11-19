#!/usr/bin/env python3
"""tmux popup: finish the current window.   Bound in tmux.conf as: prefix + F

Picks the right close for what the window holds, shows it, and asks before doing it:

  plain window      tmux kill-window. (An agent that has exited needs nothing else; a running one is
                    ended by the hangup, its transcript is already on disk and resumable.)
  `here` window     (triage, babysit: the launcher tags it @launcher_here) tmux kill-window; the worktree it
                    sits in belongs to another window and is never touched.
  worktree window   merged and clean  -> workmux remove -f <handle>   (worktree + branch + window)
                    anything else     -> workmux close <handle>       (window only; worktree and
                                         branch stay, reopen with `workmux open <handle>`)
                    "merged" = the PR is MERGED and HEAD is that PR's head (or already in origin's
                    base branch). Uncommitted/untracked files, an open or missing PR, or newer local
                    commits all mean "keep". Unmerged work is never deleted from here.
                    A `stable/*` branch (a release's worktree) is never deleted either, merged or not: the window
                    closes and the worktree and branch stay. `release/*` and other branches follow the rules above.

  Refuses to close the last window of a session (that would end the session).

Usage: finish-window.py [@window-id] [--dry-run] [--yes]
  no id  -> the window the popup was opened from ($TMUX_PANE).  --yes skips the question and acts synchronously (tests).
Detection uses the panes' cwd (inside a non-main worktree of `workmux list --json`) or, failing that,
the window name (workmux names worktree windows after their handle, with an icon prefix).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
os.environ["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
SHELLS = {"zsh", "bash", "fish", "sh", "workmux"}  # workmux = the sidebar pane
PROTECTED_BRANCH = re.compile(r"stable/.+")  # long-lived: a release works ON it, so `workmux remove` (which deletes the branch) is never right


class Fail(Exception):
    """A loud, human-readable stop (shown in the popup, exit 1)."""


def run(cmd, cwd=None, timeout=20):
    # stdin from /dev/null: a child (workmux has a first-run prompt) must never read the popup's
    # keystrokes or block waiting for an answer nobody can see.
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def tmux(*args):
    rc, out, err = run(["tmux", *args])
    if rc:
        raise Fail(f"tmux {' '.join(args)}: {err or 'failed'}")
    return out


def window_info(wid):
    session, name, nwin, here = tmux("display-message", "-p", "-t", wid,
                                     "#{session_name}\t#{window_name}\t#{session_windows}\t#{?@launcher_here,1,0}").split("\t")
    panes = []
    for line in tmux("list-panes", "-t", wid, "-F", "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}").splitlines():
        pid, cmd, path, ppid = line.split("\t")
        panes.append({"id": pid, "cmd": cmd, "path": path, "pid": int(ppid)})
    return {"id": wid, "session": session, "name": name, "windows": int(nwin), "panes": panes, "here": here == "1"}


def agent_status():
    rc, out, _ = run(["workmux", "status", "--all", "--json"])
    try:
        return {a["pane_id"]: a["status"] for a in json.loads(out)["agents"]} if rc == 0 else {}
    except (ValueError, KeyError):
        return {}


def find_worktree(win):
    """The linked (non-main) worktree this window works in, or None (plain window).

    Plain git, a few ms per pane: `workmux list --json` runs git status in every worktree and takes
    seconds in a big repo, which left the popup blank. In a linked worktree the git dir differs from
    the common dir. Every pane is checked: an agent pane that wandered into the main checkout must
    not hide the worktree next to it. Fallback: the window is named after a linked worktree."""
    bare = re.sub(r"^[^A-Za-z0-9]+", "", win["name"])  # workmux prefixes an icon
    main_roots = set()
    for p in win["panes"]:
        if p["cmd"] == "workmux":
            continue
        rc, out, _ = run(["git", "rev-parse", "--show-toplevel", "--absolute-git-dir"], cwd=p["path"])
        rc2, common, _ = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=p["path"])
        if rc or rc2:
            continue
        top, gitdir = out.splitlines()
        project = os.path.dirname(common)
        if os.path.realpath(gitdir) != os.path.realpath(common):
            branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=top)[1]
            return {"handle": os.path.basename(top), "branch": "(detached)" if branch == "HEAD" else branch,
                    "path": top, "project_path": project}
        main_roots.add(project)
    for project in main_roots:  # no pane is in a linked worktree; maybe the window is named after one
        wts = run(["git", "worktree", "list", "--porcelain"], cwd=project)[1].split("\n\n")[1:]
        for block in wts:
            fields = dict(l.split(" ", 1) for l in block.splitlines() if " " in l)
            path = fields.get("worktree", "")
            if path and os.path.basename(path) == bare and os.path.isdir(path):
                br = fields.get("branch", "(detached)").replace("refs/heads/", "")
                return {"handle": bare, "branch": br, "path": path, "project_path": project}
    return None


def git(wt, *args):
    return run(["git", *args], cwd=wt["path"])


def raw(cmd, cwd=None, timeout=20):
    """Like run() but the output is NOT stripped (git status -z needs its leading spaces)."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return r.returncode, r.stdout, r.stderr.strip()


def classify_dirt(path):
    """-> (tracked changes, untracked non-symlinks, untracked symlinks as (path, target)), or None if git fails.
    Also used by close-case.py, so the popup and the case check agree on what is dirt.

    An untracked symlink is NOT dirt: removing a worktree removes only the link, never what it points to (checked
    2026-11-18 with `git worktree remove --force` and `workmux remove -f`). DagRunner seeds `node_modules` symlinks
    into its worktree at launch; the repo's `node_modules/` ignore rule has a trailing slash and so does not match a
    symlink, which is why one of them shows up as untracked."""
    rc, out, _ = raw(["git", "status", "--porcelain", "-z"], cwd=path)
    if rc:
        return None
    parts = [e for e in out.split("\0") if e]
    tracked, others, links, i = [], [], [], 0
    while i < len(parts):
        xy, rel = parts[i][:2], parts[i][3:]
        if xy == "??":
            full = os.path.join(path, rel.rstrip("/"))
            if os.path.islink(full):
                links.append((rel, os.readlink(full)))
            else:
                others.append(rel)
        elif xy != "!!":
            tracked.append(f"{xy.strip() or '?'} {rel}")
        if xy[:1] in ("R", "C"):
            i += 1  # a rename or copy carries the old path as the next entry
        i += 1
    return tracked, others, links


def sibling_windows(wt, wid):
    """Names of the OTHER tmux windows with a pane inside this worktree (None if tmux cannot say): removing the
    worktree would pull the folder out from under them."""
    rc, out, _ = run(["tmux", "list-panes", "-a", "-F", "#{window_id}\t#{window_name}\t#{pane_current_path}"])
    if rc:
        return None
    root = os.path.realpath(wt["path"])
    names = {}
    for line in out.splitlines():
        w, name, path = line.split("\t")
        rp = os.path.realpath(path)
        if w != wid and (rp == root or rp.startswith(root + os.sep)):
            names[w] = re.sub(r"^[^A-Za-z0-9]+", "", name)
    return list(names.values())


def merge_state(wt):
    """-> (merged, [reasons it is not], [facts to show]). Also used by close-case.py.

    "merged" = the PR is MERGED and HEAD is that PR's head, or HEAD is already in origin's base branch."""
    facts, reasons = [], []
    head = git(wt, "rev-parse", "HEAD")[1]
    branch = wt["branch"]
    base = git(wt, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1] or "origin/main"
    ahead = git(wt, "rev-list", "--count", f"{base}..HEAD")[1]
    in_base = ahead == "0"
    pr = None
    if branch and not branch.startswith("("):
        rc, out, _ = run(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "5",
                          "--json", "number,state,headRefOid"], cwd=wt["path"], timeout=25)
        if rc == 0:
            prs = json.loads(out or "[]")
            pr = next((p for p in prs if p["state"] == "MERGED"), prs[0] if prs else None)
        else:
            facts.append("PR state unknown (gh failed)")
    merged_pr = bool(pr and pr["state"] == "MERGED" and (pr["headRefOid"] == head or in_base))
    if pr:
        facts.append(f"PR #{pr['number']} {pr['state'].lower()}")
    if in_base:
        facts.append(f"HEAD is already in {base}")
    merged = merged_pr or in_base
    if not merged:
        if pr and pr["state"] == "MERGED":
            reasons.append(f"local commits are newer than merged PR #{pr['number']}")
        elif pr:
            reasons.append(f"PR #{pr['number']} is {pr['state'].lower()}, not merged")
        else:
            reasons.append(f"no merged PR, and {ahead or '?'} commit(s) are not in {base}")
    return merged, reasons, facts


def worktree_state(wt):
    """-> (merged_and_clean, [reasons it is not], [facts to show]). Untracked symlinks do not count as dirt."""
    reasons, link_facts = [], []
    dirt = classify_dirt(wt["path"])
    if dirt is None:  # fail closed: if git cannot say the tree is clean, it is not
        reasons.append("cannot read git status (git failed)")
        dirty = True
    else:
        tracked, others, links = dirt
        dirty = bool(tracked or others)
        if dirty:
            reasons.append(f"{len(tracked) + len(others)} uncommitted or untracked file(s)")
        link_facts = [f"untracked symlink: {p} (only the link goes with the worktree)" for p, _ in links[:3]]
        if len(links) > 3:
            link_facts.append(f"... and {len(links) - 3} more untracked symlinks")
    merged, merge_reasons, facts = merge_state(wt)
    return (merged and not dirty), reasons + merge_reasons, facts + link_facts


def getkey():
    """One raw keypress. tty.setraw flushes pending input (TCSAFLUSH) on purpose: a key typed while
    the plan was still being worked out must never answer a question the user has not seen."""
    import termios
    import tty
    fd = os.open("/dev/tty", os.O_RDONLY)
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return os.read(fd, 1).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def case_helpers():
    """close-case.py's discovery helpers, or None. The popup must keep working without them (it then prints the
    plainer lines), so any failure here is swallowed."""
    try:
        import importlib.util
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("close_case", os.path.join(os.path.dirname(os.path.abspath(__file__)), "close-case.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def running_line(cc, procs, pane):
    """What a pane that is neither a shell nor a tracked agent is running."""
    plain = f"! still running here: {pane['cmd']}"
    if not cc:
        return plain
    try:
        kind, text = cc.describe_process(procs, pane)
    except Exception:  # the popup must never crash on a helper
        return plain
    return f"! {text} is running here" if kind == "dagrun-ui" else f"! still running here: {text}"


def case_lines(cc, win, wt, status):
    """If this window drives a DagRunner case, say so and name the windows that belong to it; [] otherwise."""
    if not cc:
        return []
    try:
        state = cc.resolve_run(wt["path"])
        fam = cc.find_family(state, cc.inventory(), status)
        if not fam["driver"] or fam["driver"]["id"] != win["id"]:
            return []
        others = ([w["bare"] for w in fam["here"]] + ([fam["plan"]["bare"]] if fam["plan"] else [])
                  + [w["bare"] for w in fam["in_worktree"][1:]])
        rid, st = state["runId"], state.get("status")
        lines = [f"this window drives case {fam['key']} (run {rid}, {st})" if fam["key"] else f"this window drives run {rid} ({st})"]
        if others:
            lines.append(f"{len(others)} more window{'' if len(others) == 1 else 's'} belong to it: {', '.join(others)}")
            lines.append("closing only this one leaves them open (close the whole case with close-case.py execute; try it with --dry-run)")
        return lines
    except Exception:  # the popup must never crash on a helper
        return []


def named_dirt(wt, reasons):
    """Replace the vague "N uncommitted or untracked file(s)" with the names (three per kind at most). Falls back to
    the original reasons when git gives nothing to name."""
    dirt = classify_dirt(wt["path"])
    if not dirt:
        return reasons
    tracked, others, _links = dirt
    named = []
    for items, fmt in ((tracked, "uncommitted change: {}"), (others, "untracked file: {}")):
        named += [fmt.format(i) for i in items[:3]]
        if len(items) > 3:
            named.append(f"... and {len(items) - 3} more")
    if not named:
        return reasons
    return named + [r for r in reasons if "uncommitted or untracked file" not in r]


def plan(win, status):
    """-> (title, lines to show, command argv + cwd)

    The popup shows only what can change the answer: something still running in the window, and for a
    worktree what keeps it from being deleted. An idle agent, pane ids and the tmux command are not shown
    (--dry-run still prints the command)."""
    lines = []
    cc = case_helpers()
    try:
        procs = cc.process_table() if cc else {}
    except Exception:
        procs = {}
    for p in win["panes"]:
        if status.get(p["id"]) == "working":
            lines.append("! an agent here is still working; closing stops it")
        elif status.get(p["id"]) == "waiting":  # workmux: "agent needs user input". Copilot never reports this state.
            lines.append("! an agent here is waiting for your answer; closing drops the question")
        elif p["id"] not in status and p["cmd"] not in SHELLS:
            lines.append(running_line(cc, procs, p))
    if win["windows"] <= 1:
        raise Fail(f"'{win['name']}' is the only window in session '{win['session']}'; closing it would end the session.")
    name = f"{win['session']}:{win['name']}"
    if win["here"]:  # a launcher `here` window (triage, babysit): it sits in a worktree that another window owns
        lines.append("the worktree it sits in stays")
        return (f"Close {name}", lines, (["tmux", "kill-window", "-t", win["id"]], None))
    wt = find_worktree(win)
    if wt is None:
        return (f"Close {name}", lines, (["tmux", "kill-window", "-t", win["id"]], None))
    ok, reasons, facts = worktree_state(wt)
    lines += case_lines(cc, win, wt, status)
    lines += facts
    reasons = named_dirt(wt, reasons)
    siblings = sibling_windows(wt, win["id"])  # never delete a folder another window still sits in
    if siblings is None:
        ok = False
        reasons.insert(0, "cannot tell which other windows sit in this worktree")
    elif siblings:
        ok = False
        reasons.insert(0, f"{len(siblings)} other window{'' if len(siblings) == 1 else 's'} still sit in it: {', '.join(siblings)}")
    cwd = wt["project_path"]
    if PROTECTED_BRANCH.fullmatch(wt["branch"] or ""):
        ok = False
        reasons.insert(0, f"{wt['branch']} is a stable branch: it is never deleted from here")
    if ok:
        lines.append("nothing uncommitted")
        return (f"Delete {name}  (worktree, branch and window)", lines,
                (["workmux", "remove", "-f", wt["handle"]], cwd))
    lines += [f"not deleted: {r}" for r in reasons]
    lines.append(f"reopen later: workmux open {wt['handle']}")
    return (f"Close {name}  (worktree and branch stay)", lines,
            (["workmux", "close", wt["handle"]], cwd))


def main():
    ap = argparse.ArgumentParser(description="Finish the current tmux window (see module docstring).")
    ap.add_argument("window", nargs="?", help="window id like @12; default: the window under the popup")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="act without asking, synchronously (tests)")
    args = ap.parse_args()
    interactive = sys.stdout.isatty() and not args.yes
    try:
        if args.window and re.fullmatch(r"@\d+", args.window):
            wid = args.window
        else:  # tmux does not expand formats in a popup command; the popup knows its origin pane
            pane = os.environ.get("TMUX_PANE")
            wid = tmux("display-message", "-p", *(["-t", pane] if pane else []), "#{window_id}")
        print("\n  checking the window, git and the PR ...", flush=True)
        win = window_info(wid)
        title, lines, (argv, cwd) = plan(win, agent_status())
        print(f"\n  {title}\n  " + "-" * len(title))
        for l in lines:
            print(f"  {l}")
        if args.dry_run:
            print(f"  (dry run: {shlex.join(argv)}" + (f" in {cwd}" if cwd else "") + ")")
            return
        if not args.yes:
            print("\n  y = do it    anything else = cancel ", end="", flush=True)
            if getkey().lower() != "y":
                print("\n  cancelled")
                return
        if shutil.which(argv[0]) is None:
            raise Fail(f"{argv[0]} not found on PATH")
        if args.yes:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if r.returncode:
                raise Fail(f"{shlex.join(argv)}: {(r.stderr or r.stdout).strip()}")
            print(f"  done: {shlex.join(argv)}")
            return
        # From the popup: let it close first, then act from the tmux server (killing the window the
        # popup sits over would otherwise race the popup). A failure shows in the status line.
        cmd = (f"sleep 0.4; cd {shlex.quote(cwd)} && " if cwd else "sleep 0.4; ") + shlex.join(argv)
        cmd += f" || tmux display-message -d 8000 {shlex.quote('finish-window FAILED: ' + shlex.join(argv))}"
        subprocess.Popen(["tmux", "run-shell", "-b", cmd])
    except Exception as e:  # Fail, or an unexpected crash: keep the message on screen either way
        import traceback
        if not isinstance(e, Fail):
            traceback.print_exc()
        print(f"\033[31m  finish-window: {e}\033[0m", file=sys.stderr)
        if interactive:
            try:
                getkey()
            except OSError:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
