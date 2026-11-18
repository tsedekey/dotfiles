#!/usr/bin/env python3
"""Spec step 3, on throwaway things only.

Question A: does removing a worktree that holds symlinks leave their targets alone?
            (`git worktree remove --force`, and `workmux remove -f`, which is what finish-window.py runs)
Question B: does the detached last step `workmux wait <handle> --status done --timeout N && workmux remove -f <handle>`
            (1) do nothing while the agent is `working`, (2) remove worktree + branch + window once it is `done`,
            (3) do nothing at all when the status never reaches `done` (`waiting`), i.e. on timeout?

Everything lives in a temp dir and a throwaway tmux session (zz-closetest-<pid>); nothing of Eddie's is touched.
Run:  SCRATCH=<dir> python3 -B tests/step3_verify_removal_and_wait.py        (~1 minute; exit 1 if any check fails)
"""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
RESULTS = []


def sh(cmd, cwd=None, env=None, timeout=60, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError(f"{shlex.join(cmd)} -> {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def snapshot(root):
    """path -> sha256 for every file and symlink target text under root (not following symlinks)."""
    snap = {}
    for dirpath, dirnames, files in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            snap[os.path.relpath(p, root)] = ("link:" + os.readlink(p)) if os.path.islink(p) else hashlib.sha256(open(p, "rb").read()).hexdigest()
    return snap


def make_repo(root):
    main = os.path.join(root, "main")
    os.makedirs(os.path.join(main, "shared", "deps", "sub"))
    for rel in ("shared/deps/a.js", "shared/deps/sub/b.js", "tracked.txt"):
        with open(os.path.join(main, rel), "w") as f:
            f.write(f"content of {rel}\n")
    sh(["git", "init", "-q", "-b", "main"], cwd=main)
    with open(os.path.join(main, ".workmux.yaml"), "w") as f:  # throwaway project config: keep everything inside the temp dir
        f.write(f"worktree_dir: {root}/worktrees\nbase_branch: main\n")
    with open(os.path.join(main, ".git", "info", "exclude"), "a") as f:
        f.write(".workmux.yaml\n")
    sh(["git", "add", "tracked.txt", "shared"], cwd=main)
    sh(["git", "commit", "-q", "-m", "init"], cwd=main)
    return main


def plant_symlinks(wt, target):
    """What dagrunner left in 153's worktree (node_modules -> main checkout), plus harder cases."""
    os.symlink(os.path.join(target, "shared"), os.path.join(wt, "node_modules"))                  # absolute dir link
    os.symlink(os.path.relpath(os.path.join(target, "shared"), wt), os.path.join(wt, "rel_link"))  # relative dir link
    os.symlink(os.path.join(target, "shared", "deps", "a.js"), os.path.join(wt, "file_link"))      # link to a file
    os.symlink("/nonexistent/target", os.path.join(wt, "dangling"))                                # dangling link
    os.makedirs(os.path.join(wt, "cache"))                                                         # real untracked dir holding a link
    with open(os.path.join(wt, "cache", "real.txt"), "w") as f:
        f.write("untracked\n")
    os.symlink(os.path.join(target, "shared"), os.path.join(wt, "cache", "inner_link"))


def question_a_git(root):
    print("\nA1. git worktree remove --force")
    main = make_repo(os.path.join(root, "a1"))
    wt = os.path.join(root, "a1", "wt")
    sh(["git", "worktree", "add", "-q", "-b", "feat/a1", wt], cwd=main)
    plant_symlinks(wt, main)
    before = snapshot(main)
    sh(["git", "worktree", "remove", "--force", wt], cwd=main)
    check("worktree directory is gone", not os.path.exists(wt))
    check("link targets and the main checkout are byte-identical", snapshot(main) == before,
          f"{len(before)} files compared")


def add_window(main, session, handle):
    sh(["workmux", "add", handle, "-C", "-b", "--parent-session", session], cwd=main, timeout=60)
    wins = sh(["tmux", "list-windows", "-t", session, "-F", "#{window_id}\t#{window_name}"]).stdout.splitlines()
    wid = next(l.split("\t")[0] for l in wins if l.split("\t")[1].endswith(handle))
    pane = sh(["tmux", "list-panes", "-t", wid, "-F", "#{pane_id}"]).stdout.splitlines()[0]
    return wid, pane


def set_status(pane, status):
    sh(["workmux", "set-window-status", status], env={**ENV, "TMUX_PANE": pane})


def agent_status_of(pane):
    out = sh(["workmux", "status", "--all", "--json"]).stdout
    return next((a["status"] for a in json.loads(out)["agents"] if a["pane_id"] == pane), None)


def window_exists(wid):
    return wid in sh(["tmux", "list-windows", "-a", "-F", "#{window_id}"]).stdout.split()


def closer(main, handle, timeout, log):
    """The detached last step, exactly as `execute` will start it: from the tmux server, not from the agent's pane."""
    chain = (f"cd {shlex.quote(main)} && workmux wait {handle} --status done --timeout {timeout} >> {shlex.quote(log)} 2>&1"
             f" && workmux remove -f {handle} >> {shlex.quote(log)} 2>&1; echo exit=$? >> {shlex.quote(log)}")
    sh(["tmux", "run-shell", "-b", chain])


def question_a_workmux_and_b(root, session):
    print("\nA2 + B1. workmux wait (agent working) -> nothing; then done -> workmux remove -f (worktree + branch + window)")
    main = make_repo(os.path.join(root, "b1"))
    wid, pane = add_window(main, session, "feat-b1")
    wt = os.path.join(root, "b1", "worktrees", "feat-b1")
    check("workmux add made the worktree and window", os.path.isdir(wt) and window_exists(wid), wt)
    plant_symlinks(wt, main)
    before = snapshot(main)
    set_status(pane, "working")
    st = agent_status_of(pane)
    check("workmux status sees the fake agent as working", st == "working", f"status={st}")
    log = os.path.join(root, "b1.log")
    closer(main, "feat-b1", 60, log)
    time.sleep(4)
    check("while working: window and worktree are untouched", window_exists(wid) and os.path.isdir(wt))
    t_done = time.time()
    set_status(pane, "done")
    gone_after = None
    for _ in range(60):
        if not window_exists(wid) and not os.path.exists(wt):
            gone_after = time.time() - t_done
            break
        time.sleep(0.5)
    check("after done: window and worktree are removed", gone_after is not None,
          f"{gone_after:.1f}s after the status flipped" if gone_after is not None else "still there after 30s")
    branches = sh(["git", "branch", "--list", "feat-b1"], cwd=main).stdout.strip()
    check("after done: the branch is removed too", branches == "", branches or "no branch left")
    check("link targets and the main checkout are byte-identical", snapshot(main) == before, f"{len(before)} files compared")
    time.sleep(1)
    print("      closer log: " + " | ".join(l.strip() for l in open(log).read().splitlines() if l.strip())[:300])


def question_b_timeout(root, session):
    print("\nB2. status never reaches done (agent `waiting`): the timeout must leave everything alone")
    main = make_repo(os.path.join(root, "b2"))
    wid, pane = add_window(main, session, "feat-b2")
    wt = os.path.join(root, "b2", "worktrees", "feat-b2")
    set_status(pane, "waiting")
    log = os.path.join(root, "b2.log")
    closer(main, "feat-b2", 3, log)
    time.sleep(9)
    text = open(log).read() if os.path.exists(log) else ""
    check("after the timeout: window and worktree still exist", window_exists(wid) and os.path.isdir(wt))
    check("the chain ended with a non-zero exit", "exit=0" not in text and "exit=" in text, text.strip().replace("\n", " | ")[:200])


def main():
    root = os.path.realpath(tempfile.mkdtemp(prefix="step3-", dir=os.environ.get("SCRATCH")))
    session = f"zz-closetest-{os.getpid()}"
    try:
        sh(["tmux", "new-session", "-d", "-s", session, "-n", "keeper", "-c", root])
        question_a_git(root)
        question_a_workmux_and_b(root, session)
        question_b_timeout(root, session)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        shutil.rmtree(root, ignore_errors=True)
        left = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True).stdout
        print(f"\ncleanup: temp dir removed, throwaway session {'GONE' if session not in left.split() else 'STILL THERE'}")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
