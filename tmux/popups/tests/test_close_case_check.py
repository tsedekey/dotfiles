"""Tests for ~/dotfiles/tmux/popups/close-case.py `check` (read-only).

Run:  python3 -B -m unittest discover -s tests -v     (from tmux/popups)

Nothing here touches tmux, GitHub or your real worktrees: windows, agents and the PR are faked; git runs on a
throwaway repo. Set SCRATCH to choose where the temp repos go.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
CLOSE_CASE = os.environ.get("CLOSE_CASE_PATH") or os.path.expanduser("~/dotfiles/tmux/popups/close-case.py")  # the override is for mutation checks
spec = importlib.util.spec_from_file_location("close_case", CLOSE_CASE)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def put(path, text):
    with open(path, "w") as f:
        f.write(text)


def pane(pid, cmd, path):
    return {"id": pid, "pid": 1, "cmd": cmd, "path": path}


def win(wid, name, here, panes):
    return {"id": wid, "session": "change", "name": name, "bare": name, "here": here, "panes": panes}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        root = os.path.realpath(self.tmp.name)
        self.main, self.wt = os.path.join(root, "main"), os.path.join(root, "wt")
        os.makedirs(self.main)
        git(self.main, "init", "-q", "-b", "main")
        for f in ("a.txt", "b.txt"):
            put(os.path.join(self.main, f), "x\n")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "init")
        git(self.main, "worktree", "add", "-q", "-b", "feat/x", self.wt)
        self.runs, self.home = os.path.join(root, "runs"), os.path.join(root, "dagrunner")
        os.makedirs(self.runs)
        os.makedirs(self.home)
        self.saved = (cc.RUNS, cc.DAGRUNNER, cc.inventory, cc.process_table, cc.fw.agent_status, cc.fw.merge_state,
                      cc.pr_details, cc.issue_state, os.environ.get("TMUX_PANE"))
        cc.RUNS, cc.DAGRUNNER = self.runs, self.home
        self.windows = {
            "@1": win("@1", "driver-win", False, [pane("%1", "claude", self.wt)]),
            "@2": win("@2", "triage-1", True, [pane("%2", "copilot", self.wt)]),
            "@3": win("@3", "change-pdp-9-1", False, [pane("%3", "copilot", self.main)]),
            "@9": win("@9", "change-other", False, [pane("%9", "copilot", self.main)]),  # an unrelated case: must be ignored
        }
        self.agents = {"%1": "done", "%2": "done", "%3": "done", "%9": "working"}
        self.merged = (True, [], ["PR #1 merged"])
        cc.inventory = lambda: self.windows
        cc.process_table = lambda: {}
        cc.fw.agent_status = lambda: self.agents
        cc.fw.merge_state = lambda wt: self.merged
        cc.pr_details = lambda p, b: None
        cc.issue_state = lambda i: None
        os.environ.pop("TMUX_PANE", None)
        self.state = {"runId": "t-1", "workflow": "feature", "status": "done", "worktreePath": self.wt, "branch": "feat/x",
                      "sourcePlanPath": os.path.expanduser("~/.local/share/camunda/change-companion/pdp-9/1/1-plan.md")}

    def tearDown(self):
        (cc.RUNS, cc.DAGRUNNER, cc.inventory, cc.process_table, cc.fw.agent_status, cc.fw.merge_state,
         cc.pr_details, cc.issue_state, tp) = self.saved
        if tp is not None:
            os.environ["TMUX_PANE"] = tp
        self.tmp.cleanup()

    def sheet(self):
        return cc.check(self.state)

    def items(self, r, level):
        return [f"{i['who']}: {i['text']}" for i in r["items"] if i["level"] == level]

    def has(self, r, level, needle):
        return any(needle in t for t in self.items(r, level))


class Happy(Base):
    def test_clean_merged_case_is_ready_and_deletable(self):
        r = self.sheet()
        self.assertEqual(r["overall"], "ready")
        self.assertTrue(r["worktree"]["delete_ok"])
        self.assertEqual([m["label"] for m in r["members"]], ["triage-1", "change-pdp-9-1", "driver-win"])
        self.assertEqual(r["close_order"][:3], ["triage-1", "change-pdp-9-1", "driver-win (+ its panes)"])

    def test_untracked_symlink_is_not_dirt(self):
        os.symlink(self.main, os.path.join(self.wt, "node_modules"))
        r = self.sheet()
        self.assertTrue(self.has(r, "ready", "untracked symlink node_modules"))
        self.assertTrue(r["worktree"]["delete_ok"])

    def test_unrelated_working_agent_in_another_case_is_ignored(self):
        self.assertFalse(self.has(self.sheet(), "blocked", "change-other"))


class Blockers(Base):
    def test_working_agent_blocks_its_window_only(self):
        self.agents["%2"] = "working"
        r = self.sheet()
        self.assertEqual(r["overall"], "blocked")
        self.assertTrue(self.has(r, "blocked", "triage-1: agent is still working"))
        self.assertTrue(self.has(r, "ready", "driver-win: agent idle"))
        self.assertFalse(r["worktree"]["delete_ok"], "a live agent sits in the worktree")

    def test_agent_waiting_for_an_answer_needs_you_and_blocks_deletion(self):
        self.agents["%2"] = "waiting"
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "triage-1: agent is waiting for an answer"))
        self.assertFalse(r["worktree"]["delete_ok"], "closing would drop a pending question")

    def test_this_session_working_is_expected_not_a_blocker(self):
        self.agents["%1"] = "working"
        os.environ["TMUX_PANE"] = "%1"
        r = self.sheet()
        self.assertFalse(self.items(r, "blocked"))
        self.assertTrue(self.has(r, "ready", "this session"))

    def test_unmerged_branch_blocks_and_never_deletes(self):
        self.merged = (False, ["PR #1 is open, not merged"], [])
        r = self.sheet()
        self.assertTrue(self.has(r, "blocked", "NOT merged"))
        self.assertFalse(r["worktree"]["delete_ok"])

    def test_tracked_change_blocks_deletion(self):
        put(os.path.join(self.wt, "a.txt"), "changed\n")
        r = self.sheet()
        self.assertTrue(self.has(r, "blocked", "uncommitted changes to tracked files"))
        self.assertFalse(r["worktree"]["delete_ok"])

    def test_staged_rename_is_one_change_not_two(self):
        git(self.wt, "mv", "b.txt", "c.txt")
        tracked, _, _ = cc.classify_dirt(self.wt)
        self.assertEqual(len(tracked), 1, tracked)

    def test_untracked_real_file_needs_you_and_blocks_deletion(self):
        put(os.path.join(self.wt, "new file.txt"), "x")
        os.makedirs(os.path.join(self.wt, "d"))
        put(os.path.join(self.wt, "d", "x"), "x")
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "new file.txt"))
        self.assertTrue(self.has(r, "needs-you", "d/"))
        self.assertFalse(r["worktree"]["delete_ok"])

    def test_run_not_done_blocks(self):
        self.state["status"] = "failed"
        r = self.sheet()
        self.assertTrue(self.has(r, "blocked", "status is 'failed'"))
        self.assertFalse(r["worktree"]["delete_ok"])

    def test_held_git_write_lock_blocks(self):
        gitdir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=self.wt, capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(gitdir, "pr-writers.lock"))
        put(os.path.join(gitdir, "pr-writers.lock", "owner"), "ci-babysit")
        put(os.path.join(gitdir, "pr-writers.lock", "at"), str(int(__import__("time").time())))
        r = self.sheet()
        self.assertTrue(self.has(r, "blocked", "git write lock: held by ci-babysit"))
        self.assertFalse(r["worktree"]["delete_ok"], "a writer may be mid-push")

    def test_open_triage_comment_needs_you(self):
        d = os.path.join(self.runs, "t-1", "pr-triage")
        os.makedirs(d)
        put(os.path.join(d, "triage-state.json"), json.dumps({"comments": {"a": {"lifecycle": "posted"}, "b": {"lifecycle": "drafted"}, "c": {"lifecycle": "skipped"}}}))
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "1 drafted"))
        self.assertTrue(r["worktree"]["delete_ok"], "open comments live on GitHub and in the run folder, not in the worktree")

    def test_babysit_state_is_found_under_the_branch_named_folder(self):  # ci-babysit's run id is the branch name from a side window
        d = os.path.join(self.runs, "feat-x", "ci-babysit")
        os.makedirs(d)
        put(os.path.join(d, "tick-2026-11-16T07:41:15Z.md"), "x\n")
        put(os.path.join(d, "tick-2026-11-16T09:36:08Z.md"), "x\n")
        r = self.sheet()
        self.assertTrue(self.has(r, "info", "feat-x/ci-babysit: 2 tick(s), last tick-2026-11-16T09:36:08Z.md"))

    def test_no_babysit_folder_is_only_info(self):
        r = self.sheet()
        self.assertTrue(self.has(r, "info", "no ci-babysit folder for this run or branch"))
        self.assertEqual(r["overall"], "ready")

    def test_active_lock_of_this_run_needs_cleanup(self):
        put(os.path.join(self.home, "active.lock"), json.dumps({"runId": "t-1", "pid": 4242, "startedAt": "x"}))
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "dagrun cleanup t-1"))
        self.assertTrue(r["worktree"]["delete_ok"], "execute runs `dagrun cleanup` first; the lock does not make the folder unsafe")

    def test_active_lock_of_another_run_is_only_info(self):
        put(os.path.join(self.home, "active.lock"), json.dumps({"runId": "other", "pid": 1, "startedAt": "x"}))
        r = self.sheet()
        self.assertTrue(self.has(r, "info", "another run"))
        self.assertFalse(self.items(r, "needs-you"))

    def test_second_window_in_the_worktree_needs_you(self):
        self.windows["@7"] = win("@7", "scratch-shell", False, [pane("%7", "zsh", self.wt)])
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "scratch-shell: another window also sits in the worktree"))
        self.assertFalse(r["worktree"]["delete_ok"], "that window would lose its folder")

    def test_unknown_process_in_a_pane_needs_you(self):
        self.windows["@1"]["panes"].append(pane("%8", "python3", self.wt))
        r = self.sheet()
        self.assertTrue(self.has(r, "needs-you", "running something else"))
        self.assertFalse(r["worktree"]["delete_ok"], "an unknown process may be using the folder")

    def test_worktree_already_gone(self):
        self.state["worktreePath"] = os.path.join(self.tmp.name, "nope")
        r = self.sheet()
        self.assertTrue(self.has(r, "ready", "already gone"))
        self.assertFalse(r["worktree"]["delete_ok"])


class Facts(unittest.TestCase):
    def test_up(self):
        self.assertEqual(cc.up("05-01:15:56"), "5 d")
        self.assertEqual(cc.up("02:10:00"), "2 h")
        self.assertEqual(cc.up("03:20"), "3 min")

    def test_checkpoint_issue_comes_from_the_issue_line_not_the_first_url(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, dir=os.environ.get("SCRATCH")) as f:
            f.write("# c\nsee https://github.com/a/b/issues/9\n## Identity\n- Issue: https://github.com/camunda/x/issues/153\n"
                    "## Docs follow-ups\n- one\n- two\n\n## Closed\n- done\n")
        try:
            sections, issue = cc.checkpoint_facts(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(issue, {"repo": "camunda/x", "number": "153"})
        self.assertEqual(len(sections["Docs follow-ups"]), 2)

    def test_check_counts(self):
        rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"conclusion": "FAILURE"},
                  {"conclusion": "", "status": "IN_PROGRESS"}, {"state": "SUCCESS"}, {}]
        calls = []

        def fake(cmd, cwd=None, timeout=20):
            calls.append(cmd[:3])
            if cmd[2] == "list":
                return 0, json.dumps([{"number": 5, "state": "MERGED"}]), ""
            return 0, json.dumps({"number": 5, "state": "MERGED", "url": "u", "mergeCommit": {"oid": "abcdef123"},
                                  "reviewDecision": "APPROVED", "statusCheckRollup": rollup}), ""
        saved = cc.fw.run
        cc.fw.run = fake
        try:
            pr = cc.pr_details("/x", "b")
        finally:
            cc.fw.run = saved
        self.assertEqual(pr["checks"], {"ok": 2, "failed": 1, "skipped": 1, "pending": 1, "no_result": 1})
        self.assertEqual(pr["sha"], "abcdef1")
        self.assertIn(["gh", "pr", "view"], calls)  # the full rollup comes from `pr view`, not the capped `pr list`


if __name__ == "__main__":
    unittest.main()
