#!/usr/bin/env python3
"""tmux popup: finish the current window.   Bound in tmux.conf as: prefix + F

Picks the right close for what the window holds, shows it, and asks before doing it:

  plain window      tmux kill-window. (An agent that has exited needs nothing else; a running one is
                    ended by the hangup, its transcript is already on disk and resumable.)
  worktree window   merged and clean  -> workmux remove -f <handle>   (worktree + branch + window)
                    anything else     -> workmux close <handle>       (window only; worktree and
                                         branch stay, reopen with `workmux open <handle>`)
                    "merged" = the PR is MERGED and HEAD is that PR's head (or already in origin's
                    base branch). Uncommitted/untracked files, an open or missing PR, or newer local
                    commits all mean "keep". Unmerged work is never deleted from here.

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
    session, name, nwin = tmux("display-message", "-p", "-t", wid,
                               "#{session_name}\t#{window_name}\t#{session_windows}").split("\t")
    panes = []
    for line in tmux("list-panes", "-t", wid, "-F", "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}").splitlines():
        pid, cmd, path = line.split("\t")
        panes.append({"id": pid, "cmd": cmd, "path": path})
    return {"id": wid, "session": session, "name": name, "windows": int(nwin), "panes": panes}


def agent_status():
    rc, out, _ = run(["workmux", "status", "--all", "--json"])
    try:
        return {a["pane_id"]: a["status"] for a in json.loads(out)["agents"]} if rc == 0 else {}
    except (ValueError, KeyError):
        return {}


def find_worktree(win):
    """The non-main worktree this window works in, or None (plain window)."""
    for p in win["panes"]:
        if p["cmd"] == "workmux":
            continue
        rc, out, _ = run(["workmux", "list", "--json"], cwd=p["path"])
        if rc:
            continue
        rows = json.loads(out)
        top = run(["git", "rev-parse", "--show-toplevel"], cwd=p["path"])[1]
        for r in rows:
            if r["path"] == top:
                return None if r["is_main"] else r
        bare = re.sub(r"^[^A-Za-z0-9]+", "", win["name"])  # workmux prefixes an icon
        for r in rows:
            if not r["is_main"] and r["handle"] == bare:
                return r
    return None


def git(wt, *args):
    return run(["git", *args], cwd=wt["path"])


def worktree_state(wt):
    """-> (merged_and_clean, [reasons it is not], [facts to show])"""
    facts, reasons = [], []
    dirty = [l for l in git(wt, "status", "--porcelain")[1].splitlines() if l]
    if dirty:
        reasons.append(f"{len(dirty)} uncommitted or untracked file(s)")
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
    return (merged and not dirty), reasons, facts


def getkey():
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


def plan(win, status):
    """-> (title, lines to show, command argv + cwd, confirm label)"""
    agents = [(p, status.get(p["id"])) for p in win["panes"] if p["id"] in status]
    busy = [p for p in win["panes"] if p["cmd"] not in SHELLS and p["id"] not in status]
    lines = []
    for p, st in agents:
        lines.append(f"agent   pane {p['id']} ({p['cmd']})  status: {st}" + ("   <-- still working" if st == "working" else ""))
    for p in busy:
        lines.append(f"running pane {p['id']}: {p['cmd']}")
    if win["windows"] <= 1:
        raise Fail(f"'{win['name']}' is the only window in session '{win['session']}'; closing it would end the session.")
    wt = find_worktree(win)
    if wt is None:
        lines.append("plain window: nothing on disk besides the agent's transcript")
        return (f"Finish {win['session']}:{win['name']}", lines,
                (["tmux", "kill-window", "-t", win["id"]], None), "close the window (tmux kill-window)")
    ok, reasons, facts = worktree_state(wt)
    lines.append(f"worktree '{wt['handle']}'  branch {wt['branch']}")
    lines += [f"  {f}" for f in facts]
    cwd = wt["project_path"]
    if ok:
        lines.append("merged and clean: safe to delete")
        return (f"Finish {win['session']}:{win['name']}", lines,
                (["workmux", "remove", "-f", wt["handle"]], cwd),
                f"DELETE worktree + branch + window (workmux remove -f {wt['handle']})")
    lines.append("NOT finished, so nothing is deleted:")
    lines += [f"  - {r}" for r in reasons]
    return (f"Park {win['session']}:{win['name']}", lines,
            (["workmux", "close", wt["handle"]], cwd),
            f"close the window only; worktree and branch stay (workmux close {wt['handle']})")


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
        win = window_info(wid)
        title, lines, (argv, cwd), label = plan(win, agent_status())
        print(f"\n  {title}\n  " + "-" * len(title))
        for l in lines:
            print(f"  {l}")
        print(f"\n  Action: {label}")
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
    except Fail as e:
        print(f"\033[31m  finish-window: {e}\033[0m", file=sys.stderr)
        if interactive:
            try:
                getkey()
            except OSError:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
