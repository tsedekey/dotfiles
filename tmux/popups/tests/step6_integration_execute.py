#!/usr/bin/env python3
"""`close-case.py execute` for real, on throwaway things only: real tmux windows (in a throwaway session), real git
worktrees (with a local `origin`), real `workmux add/wait/remove`, the real detached last step. Only the agents are
fake: their status is set by hand with `workmux set-window-status`, the way the real hooks do.

Each scenario builds its own small case (a driver window in a worktree, triage and babysit windows in it, a plan
window in the main checkout, a run state.json) and checks what must happen AND what must not.

Run:  SCRATCH=<dir> python3 -B tests/step6_integration_execute.py        (~3 minutes; exit 1 if any check fails; arguments select scenarios, e.g. `8`)
Nothing of Eddie's is touched: temp dir, session zz-exec-<pid>, and a private dagrunner home via
CLOSE_CASE_DAGRUNNER_HOME.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
CLOSE_CASE = os.path.expanduser("~/dotfiles/tmux/popups/close-case.py")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
RESULTS = []


def sh(cmd, cwd=None, env=None, timeout=60, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def slurp(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def snapshot(root):
    snap = {}
    for dirpath, dirnames, files in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                with open(p, "rb") as fh:
                    snap[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return snap


def windows():
    return sh(["tmux", "list-windows", "-a", "-F", "#{window_id}"]).stdout.split()


def wait_until(pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return time.time() - t0
        time.sleep(0.4)
    return None


def set_status(pane, status):
    sh(["workmux", "set-window-status", status], env={**ENV, "TMUX_PANE": pane}, timeout=15)


class Case:
    """One throwaway case: <root>/c<n>/{main,origin.git,worktrees/feat-<n>}, a session with its windows, a run state."""

    def __init__(self, root, session, n, bulk=0):
        self.n, self.session = n, session
        self.dir = os.path.join(root, f"c{n}")
        self.main = os.path.join(self.dir, "main")
        self.wt = os.path.join(self.dir, "worktrees", f"feat-{n}")
        self.handle = f"feat-{n}"
        self.dag = os.path.join(root, "dagrunner")
        self.case_dir = os.path.join(root, "change-companion", "pdp-9", str(n))
        self.rid = f"it-{n}"
        os.makedirs(os.path.join(self.main, "shared"))
        os.makedirs(self.case_dir, exist_ok=True)
        for rel in ("shared/dep.js", "tracked.txt"):
            put(os.path.join(self.main, rel), f"content of {rel}\n")
        sh(["git", "init", "-q", "-b", "main"], cwd=self.main)
        put(os.path.join(self.main, ".workmux.yaml"), f"worktree_dir: {self.dir}/worktrees\nbase_branch: main\n")
        put(os.path.join(self.main, ".git", "info", "exclude"), ".workmux.yaml\n")
        # A lockfile at the project root switches on workmux's background cleanup, as in the Camunda monorepo: `workmux remove`
        # then returns at once ("Scheduled removal") and the folder disappears later. Without it the removal is synchronous,
        # which hid a bug in the first version of the final step (found on the real close of 153, 2026-11-18).
        put(os.path.join(self.main, "package-lock.json"), "{}\n")
        for i in range(bulk // 100):  # a big tracked tree makes the background removal take real time, as in the monorepo
            d = os.path.join(self.main, "bulk", f"d{i:03d}")
            os.makedirs(d)
            for j in range(100):
                put(os.path.join(d, f"f{j:02d}.txt"), "x\n")
        sh(["git", "add", "tracked.txt", "shared", "package-lock.json"] + (["bulk"] if bulk else []), cwd=self.main)
        sh(["git", "commit", "-q", "-m", "init"], cwd=self.main)
        sh(["git", "init", "-q", "--bare", os.path.join(self.dir, "origin.git")])
        sh(["git", "remote", "add", "origin", os.path.join(self.dir, "origin.git")], cwd=self.main)
        sh(["git", "push", "-q", "-u", "origin", "main"], cwd=self.main)
        sh(["git", "remote", "set-head", "origin", "main"], cwd=self.main)
        # the driver window and the worktree, made the way the launcher makes them
        sh(["workmux", "add", self.handle, "-C", "-b", "--parent-session", session], cwd=self.main)
        self.driver = self.win_id(self.handle)
        self.driver_pane = self.first_pane(self.driver)
        os.symlink(os.path.join(self.main, "shared"), os.path.join(self.wt, "node_modules"))  # what DagRunner seeds
        # triage and babysit: launcher `here` windows inside the worktree; the plan window in the main checkout
        self.triage = self.new_window(f"triage-{n}", self.wt, here=True)
        self.babysit = self.new_window(f"babysit-{n}", self.wt, here=True)
        self.plan = self.new_window(f"change-pdp-9-{n}", self.main, here=False)
        for w in (self.triage, self.babysit, self.plan):
            set_status(self.first_pane(w), "done")
        set_status(self.driver_pane, "working")  # the driver is mid-turn while it runs the command
        os.makedirs(os.path.join(self.dag, "runs", self.rid), exist_ok=True)
        put(os.path.join(self.dag, "runs", self.rid, "state.json"), json.dumps({
            "runId": self.rid, "workflow": "feature", "status": "done", "worktreePath": self.wt, "branch": self.handle,
            "worktreeOrigin": "adopted", "sourcePlanPath": os.path.join(self.case_dir, f"{n}-plan.md"), "updatedAt": "2026-11-18"}))
        self.before = snapshot(self.main)

    def win_id(self, name):
        lines = sh(["tmux", "list-windows", "-t", self.session, "-F", "#{window_id}\t#{window_name}"]).stdout.splitlines()
        return next(l.split("\t")[0] for l in lines if l.split("\t")[1].endswith(name))

    def first_pane(self, wid):
        return sh(["tmux", "list-panes", "-t", wid, "-F", "#{pane_id}"]).stdout.split()[0]

    def new_window(self, name, cwd, here):
        wid = sh(["tmux", "new-window", "-d", "-P", "-F", "#{window_id}", "-t", self.session, "-n", name, "-c", cwd]).stdout.strip()
        if here:
            sh(["tmux", "set-option", "-w", "-t", wid, "@launcher_here", "1"])
        return wid

    def execute(self, *args, timeout=180):
        env = {**ENV, "TMUX_PANE": self.driver_pane, "CLOSE_CASE_DAGRUNNER_HOME": self.dag}
        r = sh([sys.executable, "-B", CLOSE_CASE, "execute", self.rid, *args], cwd="/tmp", env=env, timeout=timeout, check=False)
        return r.returncode, r.stdout + r.stderr

    def log(self):
        return slurp(os.path.join(self.case_dir, "close-case.log"))

    def branch_exists(self):
        return bool(sh(["git", "branch", "--list", self.handle], cwd=self.main).stdout.strip())

    def main_intact(self):
        return snapshot(self.main) == self.before


def scenario_dry_run(root, session):
    print("\n1. --dry-run on a real case changes nothing")
    c = Case(root, session, 1)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--dry-run")
    check("exit 0 and it says DRY RUN", rc == 0 and "DRY RUN" in out, out.splitlines()[0] if out else "")
    check("every window and the worktree are still there", all(w in windows() for w in (c.driver, c.triage, c.babysit, c.plan)) and os.path.isdir(c.wt))
    check("no log was written (nothing was done)", c.log() == "")


def scenario_happy(root, session):
    print("\n2. everything approved: side windows and the plan window go at once; the driver window and the worktree go only after the driver's turn ends")
    c = Case(root, session, 2)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--timeout", "90")
    check("exit 0", rc == 0, out.replace("\n", " | ")[:200] if rc else "")
    gone = wait_until(lambda: all(w not in windows() for w in (c.triage, c.babysit, c.plan)), 5)
    check("triage, babysit and plan windows are closed", gone is not None)
    time.sleep(4)
    check("while the driver is still working: driver window and worktree are untouched", c.driver in windows() and os.path.isdir(c.wt))
    t_done = time.time()
    set_status(c.driver_pane, "done")
    removed = wait_until(lambda: not os.path.exists(c.wt) and c.driver not in windows(), 30)
    check("after the turn ends: worktree and driver window are removed", removed is not None, f"{time.time() - t_done:.1f}s after done" if removed is not None else "still there after 30s")
    check("the branch is removed too", not c.branch_exists())
    check("the main checkout and the symlink target are byte-identical", c.main_intact())
    text = c.log()
    check("the log tells the story", "closed" in text and "final step started" in text and "done" in text.splitlines()[-1], text.splitlines()[-1] if text else "no log")


def scenario_partial(root, session):
    print("\n3. a triage agent is still working: it stays, the rest closes, the worktree and driver stay")
    c = Case(root, session, 3)
    set_status(c.first_pane(c.triage), "working")
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--timeout", "10")
    check("exit 1 (partial) and it says REFUSED", rc == 1 and "REFUSED" in out, f"rc={rc}")
    check("babysit and plan windows are closed", wait_until(lambda: c.babysit not in windows() and c.plan not in windows(), 5) is not None)
    check("the working triage window is left open", c.triage in windows())
    set_status(c.driver_pane, "done")
    time.sleep(6)
    check("worktree and driver window stay", os.path.isdir(c.wt) and c.driver in windows())
    check("nothing about the main checkout changed", c.main_intact())


def scenario_unmerged(root, session):
    print("\n4. an unmerged branch: nothing at all is closed")
    c = Case(root, session, 4)
    put(os.path.join(c.wt, "new.txt"), "work\n")
    sh(["git", "add", "new.txt"], cwd=c.wt)
    sh(["git", "commit", "-q", "-m", "unpushed work"], cwd=c.wt)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree")
    check("exit 1 and it says not merged", rc == 1 and "not merged" in out, f"rc={rc}")
    check("no window was closed and the worktree is there", all(w in windows() for w in (c.driver, c.triage, c.babysit, c.plan)) and os.path.isdir(c.wt))
    check("the unpushed commit is still on its branch", "unpushed work" in sh(["git", "log", "--oneline", c.handle], cwd=c.main).stdout)


def scenario_gate_after_approval(root, session):
    print("\n5. a real file appears AFTER the approval: the last step's own gates refuse, nothing is deleted")
    c = Case(root, session, 5)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--timeout", "60")
    check("exit 0 (it was fine when approved)", rc == 0)
    wait_until(lambda: all(w not in windows() for w in (c.triage, c.babysit, c.plan)), 5)
    put(os.path.join(c.wt, "precious-notes.txt"), "do not lose me\n")  # created between the approval and the end of the turn
    set_status(c.driver_pane, "done")
    ok = wait_until(lambda: "NOT DONE" in c.log(), 20)
    check("the last step refuses and says why", ok is not None and "precious-notes.txt" in c.log(), c.log().splitlines()[-1][:160] if c.log() else "no log")
    check("the worktree, the new file and the driver window are untouched", os.path.exists(os.path.join(c.wt, "precious-notes.txt")) and c.driver in windows())
    check("the branch is still there", c.branch_exists())


def scenario_timeout(root, session):
    print("\n6. the driver's turn never ends within the timeout: nothing is deleted")
    c = Case(root, session, 6)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--timeout", "4")
    check("exit 0", rc == 0)
    ok = wait_until(lambda: "NOT DONE" in c.log(), 25)
    check("the last step gives up and says why", ok is not None and "did not finish" in c.log(), c.log().splitlines()[-1][:160] if c.log() else "no log")
    check("worktree and driver window are untouched", os.path.isdir(c.wt) and c.driver in windows())


def scenario_typo(root, session):
    print("\n7. a typo in the approval is an error, not a guess")
    c = Case(root, session, 7)
    rc, out = c.execute("--approve", "triage,babysitt,worktree")
    check("exit 2 and it names the bad word", rc == 2 and "babysitt" in out, f"rc={rc}")
    check("nothing was closed", all(w in windows() for w in (c.driver, c.triage, c.babysit, c.plan)) and os.path.isdir(c.wt))


def scenario_big_worktree(root, session):
    print("\n8. a big tracked tree (10,000 files) still ends in `done`. NOTE: this does not reproduce the race of the real close of 153")
    # `workmux remove` only schedules the destructive cleanup and returns; on the real monorepo checkout (with Claude and the
    # dagrun ui server in the window) the folder outlived the command and the first version logged a false NOT DONE. A toy tree
    # removes too fast to lose that race, so the race itself is covered by test_a_removal_that_workmux_only_schedules_is_still_a_success.
    c = Case(root, session, 8, bulk=10000)
    rc, out = c.execute("--approve", "triage,babysit,plan,worktree", "--timeout", "90")
    check("exit 0", rc == 0, out.replace("\n", " | ")[:200] if rc else "")
    wait_until(lambda: all(w not in windows() for w in (c.triage, c.babysit, c.plan)), 5)
    time.sleep(2)
    set_status(c.driver_pane, "done")
    removed = wait_until(lambda: not os.path.exists(c.wt) and c.driver not in windows(), 120)
    check("worktree and driver window are eventually removed", removed is not None, f"{removed:.0f}s after done" if removed is not None else "still there after 120s")
    time.sleep(3)
    text = c.log()
    last = text.strip().splitlines()[-1] if text.strip() else "no log"
    check("the log says done and never NOT DONE", "NOT DONE" not in text and last.endswith("done") or "gone" in last, last[:200])
    check("the branch is gone and the main checkout is intact", not c.branch_exists() and c.main_intact())


def main():
    root = os.path.realpath(tempfile.mkdtemp(prefix="step6-", dir=os.environ.get("SCRATCH")))
    session = f"zz-exec-{os.getpid()}"
    try:
        sh(["tmux", "new-session", "-d", "-s", session, "-n", "keeper", "-c", root])
        only = set(sys.argv[1:])  # e.g. `step6_integration_execute.py 2` runs just scenario 2
        for number, scenario in enumerate((scenario_dry_run, scenario_happy, scenario_partial, scenario_unmerged,
                                           scenario_gate_after_approval, scenario_timeout, scenario_typo,
                                           scenario_big_worktree), 1):
            if not only or str(number) in only:
                scenario(root, session)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        time.sleep(1)
        shutil.rmtree(root, ignore_errors=True)
        left = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True).stdout.split()
        procs = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
        stray = [l for l in procs.splitlines() if "close-case.py _final" in l and "step6-" in l]
        print(f"\ncleanup: temp dir removed, session {'GONE' if session not in left else 'STILL THERE'}, stray final-step processes: {len(stray)}")
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
