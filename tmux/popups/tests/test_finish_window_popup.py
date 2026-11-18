"""Tests for the popup helpers added to ~/dotfiles/tmux/popups/finish-window.py (named dirt, running_line, case_lines).

Run:  SCRATCH=<dir> python3 -B -m unittest discover -s tests -v     (from tmux/popups)
The rule they protect: the popup NEVER crashes on a helper; it falls back to the plainer line.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")  # the override is for mutation checks
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(POPUPS, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = load("fw_under_test", "finish-window.py")
cc = load("cc_under_test", "close-case.py")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=ENV)


def put(path, text="x\n"):
    with open(path, "w") as f:
        f.write(text)


class Repo(unittest.TestCase):
    """A throwaway git repo stands in for the worktree; the merge lookup (GitHub) is faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        self.repo = os.path.realpath(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        put(os.path.join(self.repo, "a.txt"))
        git(self.repo, "add", "."), git(self.repo, "commit", "-q", "-m", "i")
        self.wt = {"path": self.repo, "branch": "feat/x", "handle": "wt", "project_path": self.repo}
        self.vague = ["1 uncommitted or untracked file(s)", "PR #1 is open, not merged"]
        self.saved_merge = fw.merge_state
        fw.merge_state = lambda wt: (True, [], ["PR #1 merged"])

    def tearDown(self):
        fw.merge_state = self.saved_merge
        self.tmp.cleanup()


class NamedDirt(Repo):
    def test_tracked_and_untracked_are_named_separately(self):
        put(os.path.join(self.repo, "a.txt"), "changed\n")
        put(os.path.join(self.repo, "new.txt"))
        out = fw.named_dirt(self.wt, self.vague)
        self.assertIn("uncommitted change: M a.txt", out)
        self.assertIn("untracked file: new.txt", out)
        self.assertNotIn("1 uncommitted or untracked file(s)", out)

    def test_more_than_three_are_summarised(self):
        for i in range(5):
            put(os.path.join(self.repo, f"f{i}.txt"))
        out = fw.named_dirt(self.wt, self.vague)
        self.assertEqual(sum(1 for o in out if o.startswith("untracked file:")), 3)
        self.assertIn("... and 2 more", out)

    def test_a_symlink_alone_has_nothing_to_name(self):
        os.symlink("/tmp", os.path.join(self.repo, "node_modules"))
        self.assertEqual(fw.named_dirt(self.wt, self.vague), self.vague)

    def test_clean_tree_keeps_the_original_reasons(self):
        self.assertEqual(fw.named_dirt(self.wt, self.vague), self.vague)

    def test_git_failure_keeps_the_original_reasons(self):
        self.assertEqual(fw.named_dirt({"path": os.path.join(self.repo, "nope")}, self.vague), self.vague)


class WorktreeState(Repo):
    def test_clean_and_merged_is_deletable(self):
        ok, reasons, facts = fw.worktree_state(self.wt)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_an_untracked_symlink_is_not_dirt_and_is_mentioned(self):  # the 153 case: DagRunner's node_modules link
        os.symlink("/tmp", os.path.join(self.repo, "node_modules"))
        ok, reasons, facts = fw.worktree_state(self.wt)
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        self.assertIn("untracked symlink: node_modules (only the link goes with the worktree)", facts)

    def test_more_than_three_symlinks_are_summarised(self):
        for i in range(5):
            os.symlink("/tmp", os.path.join(self.repo, f"l{i}"))
        ok, _, facts = fw.worktree_state(self.wt)
        self.assertTrue(ok)
        self.assertIn("... and 2 more untracked symlinks", facts)

    def test_an_untracked_real_file_blocks(self):
        put(os.path.join(self.repo, "new.txt"))
        ok, reasons, _ = fw.worktree_state(self.wt)
        self.assertFalse(ok)
        self.assertIn("1 uncommitted or untracked file(s)", reasons)

    def test_a_link_next_to_a_real_file_still_blocks(self):
        os.symlink("/tmp", os.path.join(self.repo, "node_modules"))
        put(os.path.join(self.repo, "new.txt"))
        self.assertFalse(fw.worktree_state(self.wt)[0])

    def test_a_tracked_change_blocks(self):
        put(os.path.join(self.repo, "a.txt"), "changed\n")
        self.assertFalse(fw.worktree_state(self.wt)[0])

    def test_unmerged_blocks_even_when_clean(self):
        fw.merge_state = lambda wt: (False, ["PR #1 is open, not merged"], [])
        ok, reasons, _ = fw.worktree_state(self.wt)
        self.assertFalse(ok)
        self.assertIn("PR #1 is open, not merged", reasons)

    def test_unreadable_git_fails_closed(self):  # the old code said "deletable" here when git failed with no output
        ok, reasons, _ = fw.worktree_state({**self.wt, "path": os.path.join(self.repo, "nope")})
        self.assertFalse(ok)
        self.assertIn("cannot read git status (git failed)", reasons)


class SiblingWindows(unittest.TestCase):
    def listing(self, root, rc=0):
        text = "\n".join([f"@1\tdriver\t{root}", f"@2\ttriage-1\t{root}/sub/dir", f"@3\t\ue0a0babysit-1\t{root}",
                          "@4\tunrelated\t/elsewhere", f"@5\tlookalike\t{root}-other"])
        return lambda cmd, cwd=None, timeout=20: (rc, text, "")

    def setUp(self):
        self.saved = fw.run

    def tearDown(self):
        fw.run = self.saved

    def test_other_windows_inside_the_worktree_are_named(self):
        root = os.path.realpath(tempfile.gettempdir())
        fw.run = self.listing(root)
        self.assertEqual(fw.sibling_windows({"path": root}, "@1"), ["triage-1", "babysit-1"])

    def test_the_window_itself_a_lookalike_path_and_an_unrelated_window_do_not_count(self):
        root = os.path.realpath(tempfile.gettempdir())
        fw.run = self.listing(root)
        names = fw.sibling_windows({"path": root}, "@1")
        self.assertNotIn("driver", names)
        self.assertNotIn("lookalike", names)
        self.assertNotIn("unrelated", names)

    def test_tmux_failure_is_none_so_the_caller_can_fail_closed(self):
        fw.run = self.listing("/x", rc=1)
        self.assertIsNone(fw.sibling_windows({"path": "/x"}, "@1"))


class PlanGate(unittest.TestCase):
    """plan(): a worktree is deleted only when merged, clean AND no other window sits in it."""

    def setUp(self):
        self.saved = (fw.find_worktree, fw.worktree_state, fw.case_helpers, fw.sibling_windows)
        fw.find_worktree = lambda win: {"handle": "wt", "branch": "b", "path": "/nonexistent-w", "project_path": "/p"}
        fw.worktree_state = lambda wt: (True, [], ["PR #1 merged"])
        fw.case_helpers = lambda: None
        self.win = {"id": "@1", "session": "change", "name": "drv", "windows": 3, "here": False,
                    "panes": [{"id": "%1", "cmd": "zsh", "path": "/nonexistent-w", "pid": 1}]}

    def tearDown(self):
        fw.find_worktree, fw.worktree_state, fw.case_helpers, fw.sibling_windows = self.saved

    def test_merged_clean_and_alone_is_deleted(self):
        fw.sibling_windows = lambda wt, wid: []
        title, lines, (argv, cwd) = fw.plan(self.win, {})
        self.assertTrue(title.startswith("Delete"))
        self.assertEqual(argv[:3], ["workmux", "remove", "-f"])

    def test_another_window_in_the_worktree_means_park_not_delete(self):
        fw.sibling_windows = lambda wt, wid: ["triage-1", "babysit-1"]
        title, lines, (argv, cwd) = fw.plan(self.win, {})
        self.assertTrue(title.startswith("Close"))
        self.assertEqual(argv[:2], ["workmux", "close"])
        self.assertIn("not deleted: 2 other windows still sit in it: triage-1, babysit-1", lines)

    def test_tmux_unable_to_say_means_park_not_delete(self):
        fw.sibling_windows = lambda wt, wid: None
        title, lines, (argv, cwd) = fw.plan(self.win, {})
        self.assertEqual(argv[:2], ["workmux", "close"])
        self.assertIn("not deleted: cannot tell which other windows sit in this worktree", lines)

    def test_a_waiting_agent_is_warned_about_and_changes_nothing_else(self):
        fw.sibling_windows = lambda wt, wid: []
        title, lines, (argv, cwd) = fw.plan(self.win, {"%1": "waiting"})
        self.assertIn("! an agent here is waiting for your answer; closing drops the question", lines)
        self.assertTrue(title.startswith("Delete"), "the warning informs; it does not change the action")

    def test_a_working_agent_keeps_its_own_warning(self):
        fw.sibling_windows = lambda wt, wid: []
        lines = fw.plan(self.win, {"%1": "working"})[1]
        self.assertIn("! an agent here is still working; closing stops it", lines)
        self.assertNotIn("! an agent here is waiting for your answer; closing drops the question", lines)

    def test_an_idle_agent_gets_no_warning(self):
        fw.sibling_windows = lambda wt, wid: []
        lines = fw.plan(self.win, {"%1": "done"})[1]
        self.assertFalse([l for l in lines if l.startswith("!")])

    def test_an_unmerged_or_dirty_worktree_is_parked_even_when_alone(self):
        fw.worktree_state = lambda wt: (False, ["1 uncommitted or untracked file(s)"], [])
        fw.sibling_windows = lambda wt, wid: []
        self.assertEqual(fw.plan(self.win, {})[2][0][:2], ["workmux", "close"])


class RunningLine(unittest.TestCase):
    pane = {"id": "%1", "cmd": "node", "path": "/x", "pid": 1}

    def test_dagrun_ui_is_named(self):
        fake = types.SimpleNamespace(describe_process=lambda procs, p: ("dagrun-ui", "the dagrun ui server (up 5 d)"))
        self.assertEqual(fw.running_line(fake, {}, self.pane), "! the dagrun ui server (up 5 d) is running here")

    def test_unknown_process_shows_its_command(self):
        fake = types.SimpleNamespace(describe_process=lambda procs, p: ("other", "vite --port 5173"))
        self.assertEqual(fw.running_line(fake, {}, self.pane), "! still running here: vite --port 5173")

    def test_no_helpers_falls_back_to_the_command_name(self):
        self.assertEqual(fw.running_line(None, {}, self.pane), "! still running here: node")

    def test_a_helper_that_raises_falls_back_instead_of_crashing(self):
        broken = types.SimpleNamespace(describe_process=lambda procs, p: p["no such key"])
        self.assertEqual(fw.running_line(broken, {}, self.pane), "! still running here: node")

    def test_a_pane_without_a_pid_does_not_crash_the_real_helper(self):  # the bug found on the first live dry-run
        pane = {"id": "%1", "cmd": "node", "path": "/x"}
        self.assertTrue(fw.running_line(cc, {}, pane).startswith("! still running here"))


class CaseLines(unittest.TestCase):
    def helper(self, driver_id, here=(), plan=None, extra=()):
        win = lambda i, n: {"id": i, "bare": n}
        fam = {"key": "pdp-9/1", "here": [win(i, n) for i, n in here], "plan": win(*plan) if plan else None,
               "in_worktree": [win(driver_id, "driver")] + [win(i, n) for i, n in extra],
               "driver": win(driver_id, "driver")}
        return types.SimpleNamespace(resolve_run=lambda p: {"runId": "r-1", "status": "done"},
                                     inventory=lambda: {}, find_family=lambda s, w, st: fam)

    def test_the_driver_window_names_its_family(self):
        cc_ = self.helper("@1", here=[("@2", "triage-1"), ("@3", "babysit-1")], plan=("@4", "change-pdp-9-1"))
        out = fw.case_lines(cc_, {"id": "@1"}, {"path": "/w"}, {})
        self.assertEqual(out[0], "this window drives case pdp-9/1 (run r-1, done)")
        self.assertIn("3 more windows belong to it: triage-1, babysit-1, change-pdp-9-1", out[1])

    def test_a_single_other_window_is_singular(self):
        out = fw.case_lines(self.helper("@1", here=[("@2", "triage-1")]), {"id": "@1"}, {"path": "/w"}, {})
        self.assertIn("1 more window belong to it", out[1])

    def test_a_driver_alone_gets_only_the_first_line(self):
        out = fw.case_lines(self.helper("@1"), {"id": "@1"}, {"path": "/w"}, {})
        self.assertEqual(len(out), 1)

    def test_a_window_that_is_not_the_driver_says_nothing(self):
        self.assertEqual(fw.case_lines(self.helper("@1"), {"id": "@9"}, {"path": "/w"}, {}), [])

    def test_no_run_for_the_worktree_says_nothing(self):
        def boom(p):
            raise RuntimeError("no run")
        self.assertEqual(fw.case_lines(types.SimpleNamespace(resolve_run=boom), {"id": "@1"}, {"path": "/w"}, {}), [])

    def test_no_helpers_says_nothing(self):
        self.assertEqual(fw.case_lines(None, {"id": "@1"}, {"path": "/w"}, {}), [])


if __name__ == "__main__":
    unittest.main()
