"""Tests for `close-case.py execute` (and its detached last step `_final`).

Run:  SCRATCH=<dir> python3 -B -m unittest discover -s tests -v     (from tmux/popups)

Windows, agents, GitHub and the destructive actions are faked and RECORDED (`self.calls`); git runs on a throwaway repo
and worktree. The real tmux/workmux/git flow is covered by step6_integration_execute.py.
"""
import json
import os
import shlex
import subprocess
import sys
import threading
import types
import unittest

sys.dont_write_bytecode = True
from test_close_case_check import Base, cc, git, pane, put, win  # noqa: E402  (the shared fixture: a throwaway worktree and fake windows)


def slurp(path):
    with open(path) as f:
        return f.read()


class ExecBase(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.saved_acts = (cc.act_kill_window, cc.act_cleanup, cc.act_launch_final)

        def kill(wid):
            self.calls.append(("kill", wid))
            self.windows.pop(wid, None)
            return True, ""
        cc.act_kill_window = kill
        cc.act_cleanup = lambda rid: (self.calls.append(("cleanup", rid)) or (True, "released"))
        cc.act_launch_final = lambda cmd: (self.calls.append(("final", cmd)) or (True, ""))

    def tearDown(self):
        cc.act_kill_window, cc.act_cleanup, cc.act_launch_final = self.saved_acts
        super().tearDown()

    def write_state(self):
        os.makedirs(os.path.join(self.runs, "t-1"), exist_ok=True)
        put(os.path.join(self.runs, "t-1", "state.json"), json.dumps(self.state))

    def run_exec(self, *tokens, dry=False):
        self.write_state()
        return cc.execute("t-1", list(tokens), dry)

    def kills(self):
        return [c[1] for c in self.calls if c[0] == "kill"]

    def finals(self):
        return [c[1] for c in self.calls if c[0] == "final"]

    def labels(self, res, result):
        return [s["label"] for s in res["steps"] if s["result"] == result]

    def refused(self, res):
        return {x["label"]: x["reason"] for x in res["refused"]}


class Plan(ExecBase):
    def test_everything_ready_closes_side_then_plan_then_starts_the_final_step(self):
        self.windows["@5"] = win("@5", "babysit-1", True, [pane("%5", "copilot", self.wt)])
        self.agents["%5"] = "done"
        res = self.run_exec("triage", "babysit", "plan", "worktree")
        self.assertEqual(self.kills(), ["@5", "@2", "@3"], "babysit-1, triage-1 (by label), then the plan window")
        self.assertEqual(self.labels(res, "closed"), ["babysit-1", "triage-1", "change-pdp-9-1"])
        self.assertEqual(res["refused"], [])
        self.assertEqual(res["final"]["result"], "started")
        self.assertEqual(len(self.finals()), 1)
        words = shlex.split(self.finals()[0])
        self.assertIn("_final", words)
        self.assertEqual(words[words.index("--mode") + 1], "remove")
        self.assertEqual(words[words.index("--window") + 1], "@1", "it waits on the driver window")
        self.assertEqual(words[words.index("--run") + 1], "t-1")
        self.assertNotIn("@1", self.kills(), "the driver window is never killed in the first phase")

    def test_labels_and_kinds_are_interchangeable(self):
        res = self.run_exec("triage-1", "change-pdp-9-1", "worktree")
        self.assertEqual(self.kills(), ["@2", "@3"])
        self.assertEqual(res["final"]["result"], "started")

    def test_an_unknown_name_is_an_error_and_nothing_happens(self):
        with self.assertRaises(cc.Fail) as cm:
            self.run_exec("triage", "typo-153", "worktree")
        self.assertIn("typo-153", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_dry_run_changes_nothing(self):
        res = self.run_exec("triage", "plan", "worktree", dry=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.labels(res, "would close"), ["triage-1", "change-pdp-9-1"])
        self.assertEqual(res["final"]["result"], "would start")

    def test_a_working_agent_is_refused_the_rest_closes_and_the_worktree_stays(self):
        self.agents["%2"] = "working"
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.kills(), ["@3"], "only the plan window closes")
        self.assertIn("triage-1", self.refused(res))
        self.assertIn("still working", self.refused(res)["triage-1"])
        self.assertIn("triage-1 would stay open inside the worktree", self.refused(res)["worktree"])
        self.assertIsNone(res["final"])
        self.assertEqual(self.finals(), [])

    def test_a_waiting_agent_is_refused(self):
        self.agents["%2"] = "waiting"
        res = self.run_exec("triage")
        self.assertEqual(self.kills(), [])
        self.assertIn("waiting", self.refused(res)["triage-1"])

    def test_the_window_running_the_command_is_never_closed_in_the_first_phase(self):
        os.environ["TMUX_PANE"] = "%2"
        res = self.run_exec("triage", "plan")
        self.assertEqual(self.kills(), ["@3"])
        self.assertIn("running the command", self.refused(res)["triage-1"])

    def test_without_worktree_approval_the_driver_and_worktree_are_left_alone(self):
        res = self.run_exec("triage", "plan")
        self.assertEqual(self.kills(), ["@2", "@3"])
        self.assertIsNone(res["final"])
        self.assertEqual(res["refused"], [])

    def test_driver_alone_closes_the_window_and_keeps_the_worktree(self):
        res = self.run_exec("triage", "plan", "driver")
        words = shlex.split(self.finals()[0])
        self.assertEqual(words[words.index("--mode") + 1], "close")
        self.assertEqual(res["final"]["result"], "started")

    def test_an_unmerged_branch_refuses_everything(self):
        self.merged = (False, ["PR #1 is open, not merged"], [])
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.calls, [])
        self.assertIn("not merged", self.refused(res)["(everything)"])

    def test_a_run_that_is_not_done_refuses_everything(self):
        self.state["status"] = "failed"
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.calls, [])
        self.assertIn("not done", self.refused(res)["(everything)"])

    def test_an_untracked_real_file_refuses_the_worktree_but_the_windows_still_close(self):
        put(os.path.join(self.wt, "notes.txt"), "x\n")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.kills(), ["@2", "@3"])
        self.assertIn("notes.txt", self.refused(res)["worktree"])
        self.assertIsNone(res["final"])

    def test_a_tracked_change_refuses_the_worktree(self):
        put(os.path.join(self.wt, "a.txt"), "changed\n")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertIn("worktree", self.refused(res))
        self.assertEqual(self.finals(), [])

    def test_an_untracked_symlink_does_not_refuse_the_worktree(self):  # the 153 case
        os.symlink(self.main, os.path.join(self.wt, "node_modules"))
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(res["final"]["result"], "started")

    def test_an_unknown_process_in_the_driver_window_refuses_the_worktree(self):
        self.windows["@1"]["panes"].append(pane("%8", "python3", self.wt))
        res = self.run_exec("triage", "plan", "worktree")
        self.assertIn("worktree", self.refused(res))
        self.assertIn("running something else", self.refused(res)["worktree"])

    def test_a_second_window_in_the_worktree_refuses_the_worktree(self):
        self.windows["@7"] = win("@7", "scratch-shell", False, [pane("%7", "zsh", self.wt)])
        res = self.run_exec("triage", "plan", "worktree")
        self.assertIn("scratch-shell", self.refused(res)["worktree"])
        self.assertEqual(self.finals(), [])

    def test_the_caller_in_a_driver_window_with_no_tracked_agent_is_never_removed_mid_reply(self):
        os.environ["TMUX_PANE"] = "%1"
        self.windows["@1"]["panes"] = [pane("%1", "zsh", self.wt)]
        del self.agents["%1"]  # workmux does not track it, so there is no `done` to wait for
        res = self.run_exec("triage", "plan", "worktree")
        self.assertIn("not tracked by workmux", self.refused(res)["worktree"])
        self.assertEqual(self.finals(), [])
        self.assertEqual(self.kills(), ["@2", "@3"], "the side windows still close")

    def test_the_same_window_run_from_elsewhere_may_go_without_a_wait(self):  # nobody is mid-reply in it
        self.windows["@1"]["panes"] = [pane("%1", "zsh", self.wt)]
        del self.agents["%1"]
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(res["final"]["result"], "started")
        self.assertIn("--no-wait", shlex.split(self.finals()[0]))

    def test_the_main_checkout_is_never_removed(self):
        self.state["worktreePath"] = self.main
        self.state["branch"] = "main"
        res = self.run_exec("worktree")
        self.assertIn("worktree", self.refused(res))
        self.assertEqual(self.finals(), [])


class LastMomentGates(ExecBase):
    def test_an_agent_that_started_working_after_the_check_is_not_killed(self):
        n = {"c": 0}

        def flipping():
            n["c"] += 1
            return dict(self.agents) if n["c"] == 1 else {**self.agents, "%2": "working"}
        cc.fw.agent_status = flipping
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.kills(), ["@3"])
        self.assertIn("working now", [s for s in res["steps"] if s["label"] == "triage-1"][0]["reason"])
        self.assertEqual(res["final"]["result"], "not started", "a window still sits in the worktree")
        self.assertEqual(self.finals(), [])

    def test_a_window_renamed_after_the_check_is_not_touched(self):
        n = {"c": 0}

        def inv():
            n["c"] += 1
            if n["c"] == 2:
                self.windows["@2"]["bare"] = "something-else"
            return self.windows
        cc.inventory = inv
        res = self.run_exec("triage", "plan")
        self.assertNotIn("@2", self.kills())
        self.assertIn("changed", [s for s in res["steps"] if s["window_id"] == "@2"][0]["reason"])

    def test_a_window_that_is_already_gone_is_not_a_failure(self):
        n = {"c": 0}

        def inv():
            n["c"] += 1
            if n["c"] == 2:
                self.windows.pop("@2", None)
            return self.windows
        cc.inventory = inv
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.labels(res, "already gone"), ["triage-1"])
        self.assertEqual(res["final"]["result"], "started")

    def test_a_failed_kill_stops_the_final_step(self):
        cc.act_kill_window = lambda wid: (False, "boom")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(self.labels(res, "FAILED"), ["triage-1", "change-pdp-9-1"])
        self.assertEqual(res["final"]["result"], "not started")
        self.assertEqual(self.finals(), [])


class DagrunCleanup(ExecBase):
    def lock(self, run_id):
        put(os.path.join(self.home, "active.lock"), json.dumps({"runId": run_id, "pid": 1, "startedAt": "x"}))

    def test_own_lock_of_an_adopted_done_run_is_released_before_any_window_closes(self):
        self.state["worktreeOrigin"] = "adopted"
        self.lock("t-1")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual([c[0] for c in self.calls][:3], ["cleanup", "kill", "kill"])
        self.assertEqual(res["cleanup"]["result"], "released")

    def test_another_runs_lock_is_never_touched(self):  # `dagrun cleanup` would delete it whoever owns it
        self.state["worktreeOrigin"] = "adopted"
        self.lock("some-other-run")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertNotIn("cleanup", [c[0] for c in self.calls])
        self.assertIsNone(res["cleanup"])
        self.assertEqual(res["final"]["result"], "started")

    def test_a_run_that_is_not_adopted_is_never_cleaned_up_because_cleanup_would_remove_the_worktree(self):
        self.lock("t-1")  # no worktreeOrigin in the state
        res = self.run_exec("triage", "plan", "worktree")
        self.assertNotIn("cleanup", [c[0] for c in self.calls])
        self.assertIn("dagrunner lock", self.refused(res))
        self.assertEqual(res["final"]["result"], "started", "the lock says nothing about the folder")

    def test_no_lock_no_cleanup(self):
        self.state["worktreeOrigin"] = "adopted"
        self.run_exec("triage", "plan", "worktree")
        self.assertNotIn("cleanup", [c[0] for c in self.calls])

    def test_a_failed_cleanup_is_reported_and_the_close_goes_on(self):
        self.state["worktreeOrigin"] = "adopted"
        self.lock("t-1")
        cc.act_cleanup = lambda rid: (False, "dagrun: not found")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(res["cleanup"]["result"], "FAILED")
        self.assertEqual(res["final"]["result"], "started")


class LogAndCommand(ExecBase):
    def test_a_log_is_written_in_the_case_folder(self):
        case = os.path.join(self.tmp.name, "change-companion", "pdp-9", "1")
        os.makedirs(case)
        self.state["sourcePlanPath"] = os.path.join(case, "1-plan.md")
        res = self.run_exec("triage", "plan", "worktree")
        self.assertEqual(res["log"], os.path.join(case, "close-case.log"))
        text = slurp(res["log"])
        self.assertIn("execute: approved=triage,plan,worktree", text)
        self.assertIn("window triage-1 (@2): closed", text)
        self.assertIn("final step started", text)

    def test_the_final_command_quotes_every_path_and_carries_the_test_override(self):
        f = {"mode": "remove", "handle": "wt", "project": "/p q/o'x", "worktree": "/w t", "window_id": "@1", "wait": False, "timeout": 30}
        os.environ["CLOSE_CASE_DAGRUNNER_HOME"] = "/dag home"
        try:
            words = shlex.split(cc.final_command(f, "t-1", "/log dir/c.log"))
        finally:
            del os.environ["CLOSE_CASE_DAGRUNNER_HOME"]
        self.assertEqual(words[:4], ["cd", "/p q/o'x", "&&", "env"])
        self.assertIn("CLOSE_CASE_DAGRUNNER_HOME=" + cc.DAGRUNNER, words)
        self.assertEqual(words[words.index("--worktree") + 1], "/w t")
        self.assertIn("--no-wait", words)
        self.assertEqual(words[-3:], [">>", "/log dir/c.log", "2>&1"])


class FinalFixture(Base):
    """The detached last step against a real throwaway worktree; workmux and tmux are faked, git is real."""

    def setUp(self):
        super().setUp()
        self.calls, self.wait_rc, self.remove_mode = [], 0, "sync"
        self.saved_consts = (cc.REMOVE_WAIT, cc.REMOVE_POLL)
        cc.REMOVE_WAIT, cc.REMOVE_POLL = 5, 0.05
        self.windows = {"@1": self.windows["@1"], "@9": self.windows["@9"]}  # the side and plan windows are already closed
        self.saved_run = cc.fw.run
        real = self.saved_run

        def fake(cmd, cwd=None, timeout=20):
            if cmd[0] == "workmux" and cmd[1] == "wait":
                self.calls.append(("wait", cmd[2:]))
                return self.wait_rc, "", "Timeout waiting for: wt" if self.wait_rc else ""
            if cmd[0] == "workmux" and cmd[1] == "remove":
                self.calls.append(("remove", cmd[2:]))
                gone = lambda: real(["git", "worktree", "remove", "--force", self.wt], cwd=self.main)
                if self.remove_mode == "sync":
                    gone()
                elif self.remove_mode == "background":  # what workmux really does: schedule, return at once, delete a moment later
                    threading.Timer(0.3, gone).start()
                return 0, "✓ Scheduled removal of worktree 'wt' and branch 'b'", ""
            if cmd[0] == "workmux" and cmd[1] == "close":
                self.calls.append(("close", cmd[2:]))
                return 0, "", ""
            if cmd[0] == "tmux" and cmd[1] == "display-message":
                self.calls.append(("notify", cmd[-1]))
                return 0, "", ""
            return real(cmd, cwd=cwd, timeout=timeout)
        cc.fw.run = fake
        self.saved_sibling = cc.fw.sibling_windows  # the real one asks tmux; its own tests are in test_finish_window_popup.py

        def sibling(wt, wid):
            root = os.path.realpath(wt["path"])
            return [w["bare"] for k, w in self.windows.items() if k != wid and any(cc.under(p["path"], root) for p in w["panes"])]
        cc.fw.sibling_windows = sibling
        os.makedirs(os.path.join(self.runs, "t-1"))
        put(os.path.join(self.runs, "t-1", "state.json"), json.dumps(self.state))
        self.log = os.path.join(self.tmp.name, "final.log")
        self.args = types.SimpleNamespace(run="t-1", handle="wt", project=self.main, worktree=self.wt, window="@1",
                                          mode="remove", timeout=5, no_wait=False, log=self.log)

    def tearDown(self):
        cc.fw.run = self.saved_run
        cc.fw.sibling_windows = self.saved_sibling
        cc.REMOVE_WAIT, cc.REMOVE_POLL = self.saved_consts
        super().tearDown()

    def kinds(self):
        return [c[0] for c in self.calls]

    def notes(self):
        return [c[1] for c in self.calls if c[0] == "notify"]


class FinalGates(FinalFixture):
    def gates(self, **over):
        for k, v in over.items():
            setattr(self.args, k, v)
        return cc.final_gates(self.state, self.args)

    def test_everything_in_order_is_a_go(self):
        self.assertEqual(self.gates(), [])

    def test_run_not_done(self):
        self.state["status"] = "failed"
        self.assertTrue(any("not done" in p for p in self.gates()))

    def test_arguments_that_disagree_with_the_run_state(self):
        self.assertTrue(any("disagree" in p for p in self.gates(handle="other")))

    def test_the_driver_agent_working_again(self):
        self.agents["%1"] = "working"
        self.assertTrue(any("driver agent is working" in p for p in self.gates()))

    def test_not_merged(self):
        self.merged = (False, ["PR #1 is open, not merged"], [])
        self.assertTrue(any("not merged" in p for p in self.gates()))

    def test_an_untracked_real_file(self):
        put(os.path.join(self.wt, "notes.txt"), "x\n")
        self.assertTrue(any("notes.txt" in p for p in self.gates()))

    def test_an_untracked_symlink_is_fine(self):
        os.symlink(self.main, os.path.join(self.wt, "node_modules"))
        self.assertEqual(self.gates(), [])

    def test_another_window_still_in_the_worktree(self):
        self.windows["@2"] = win("@2", "triage-1", True, [pane("%2", "copilot", self.wt)])
        self.assertTrue(any("triage-1" in p for p in self.gates()))

    def test_a_held_git_write_lock(self):
        gitdir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=self.wt, capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(gitdir, "pr-writers.lock"))
        put(os.path.join(gitdir, "pr-writers.lock", "owner"), "ci-babysit")
        put(os.path.join(gitdir, "pr-writers.lock", "at"), str(int(__import__("time").time())))
        self.assertTrue(any("git write lock" in p for p in self.gates()))

    def test_the_worktree_is_already_gone(self):
        git(self.main, "worktree", "remove", "--force", self.wt)
        self.assertTrue(any("already gone" in p for p in self.gates()))

    def test_the_main_checkout_is_never_removed(self):
        self.state["worktreePath"] = self.main
        self.assertTrue(any("not a linked worktree" in p for p in self.gates(worktree=self.main, handle="main")))

    def test_close_mode_only_cares_about_the_run_and_the_agent(self):
        put(os.path.join(self.wt, "notes.txt"), "x\n")  # would block a removal, is irrelevant to closing a window
        self.merged = (False, ["open"], [])
        self.assertEqual(self.gates(mode="close"), [])
        self.agents["%1"] = "working"
        self.assertTrue(self.gates(mode="close"))


class RunFinal(FinalFixture):
    def test_waits_then_removes_and_says_so(self):
        self.assertEqual(cc.run_final(self.args), 0)
        self.assertEqual(self.kinds(), ["wait", "remove", "notify"])
        self.assertEqual(self.calls[0][1][:3], ["wt", "--status", "done"])
        self.assertEqual(self.calls[1][1], ["-f", "wt"])
        self.assertFalse(os.path.exists(self.wt))
        self.assertIn("removed", self.notes()[0])
        self.assertIn("done", slurp(self.log))

    def test_a_removal_that_workmux_only_schedules_is_still_a_success(self):  # the real close of 153 logged a false NOT DONE here
        self.remove_mode = "background"
        self.assertEqual(cc.run_final(self.args), 0)
        self.assertFalse(os.path.exists(self.wt))
        text = slurp(self.log)
        self.assertNotIn("NOT DONE", text)
        self.assertIn("worktree gone", text)
        self.assertIn("removed", self.notes()[0])

    def test_a_removal_that_never_finishes_is_reported_as_unfinished_not_as_removed(self):
        self.remove_mode = "never"
        cc.REMOVE_WAIT = 0.4
        self.assertEqual(cc.run_final(self.args), 1)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertIn("scheduled the removal but", slurp(self.log))
        self.assertIn("check it by hand", self.notes()[0])
        self.assertNotIn("removed", self.notes()[0].replace("the removal", ""))

    def test_a_wait_that_times_out_removes_nothing(self):
        self.wait_rc = 1
        self.assertEqual(cc.run_final(self.args), 1)
        self.assertNotIn("remove", self.kinds())
        self.assertTrue(os.path.isdir(self.wt))
        self.assertIn("NOT removed", self.notes()[0])

    def test_a_gate_that_fails_after_the_wait_removes_nothing(self):
        put(os.path.join(self.wt, "notes.txt"), "x\n")
        self.assertEqual(cc.run_final(self.args), 1)
        self.assertEqual(self.kinds(), ["wait", "notify"])
        self.assertTrue(os.path.isdir(self.wt))
        self.assertIn("notes.txt", self.notes()[0])
        self.assertIn("NOT DONE", slurp(self.log))

    def test_no_wait_skips_the_wait(self):
        self.args.no_wait = True
        self.assertEqual(cc.run_final(self.args), 0)
        self.assertNotIn("wait", self.kinds())

    def test_close_mode_closes_the_window_and_keeps_the_worktree(self):
        self.args.mode = "close"
        self.assertEqual(cc.run_final(self.args), 0)
        self.assertIn("close", self.kinds())
        self.assertNotIn("remove", self.kinds())
        self.assertTrue(os.path.isdir(self.wt))

    def test_an_unreadable_run_state_stops(self):
        self.args.run = "no-such-run"
        self.assertEqual(cc.run_final(self.args), 1)
        self.assertNotIn("remove", self.kinds())


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", cc.__file__, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def test_execute_without_approve_is_a_usage_error(self):
        r = self.run_cli("execute", "no-such-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("name what to close", r.stderr)

    def test_execute_for_an_unknown_case_is_an_error_and_changes_nothing(self):
        r = self.run_cli("execute", "no-such-run-xyz", "--approve", "worktree", "--dry-run")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no DagRunner run found", r.stderr)


if __name__ == "__main__":
    unittest.main()
