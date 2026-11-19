"""Tests for the `release` workflow of ~/dotfiles/tmux/popups/launcher.py: the per-release key, the branch derived from it,
the pre-flight that makes the local branch exist and be current, and the relaunch rules.

Run:  SCRATCH=<dir> python3 -B -m unittest tests.test_launcher_release -v     (from tmux/popups)
Git is real (a throwaway bare "origin" and a clone); tmux and workmux are faked, because the rules under test are the
launcher's own. The one thing a fake cannot show, that workmux creates a MISSING branch from main, was checked by hand
against workmux 0.1.279 on 2026-11-18: that is why prepare_branch exists and why these tests assert on the commit.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")  # the override is for mutation checks
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(POPUPS, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load("launcher_under_test", "launcher.py")


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def commit(repo, name, text="x\n"):
    with open(os.path.join(repo, name), "w") as f:
        f.write(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


POM = "<project><version>{}</version></project>\n"


class Key(unittest.TestCase):
    def test_every_spelling_of_a_release_becomes_one(self):
        for text in ("8.6.30", "v8.6.30", "V8.6.30", "8.6.30-optimize", " 8.6.30 ", "8.06.030"):
            self.assertEqual(L.normalize_key("optimize-release", text), "8.6.30", text)

    def test_only_a_full_patch_version_is_a_release(self):
        for text in ("8.6", "8", "foo", "8.6.30.1", "8.6.x", "INC-8745", "CVE-2026-47848", "8.6.30-SNAPSHOT", "stable/optimize-8.6", "2026-11-18"):
            with self.assertRaises(L.Fail, msg=text) as cm:
                L.normalize_key("optimize-release", text)
            self.assertIn("8.6.30", str(cm.exception))  # the message gives an example

    def test_empty_stays_empty_and_the_line_is_major_minor(self):
        self.assertEqual(L.normalize_key("optimize-release", ""), "")
        self.assertEqual(L.release_line("8.6.30"), "8.6")
        self.assertEqual(L.release_line("8.10.2"), "8.10")


class ConfigBase(unittest.TestCase):
    """A throwaway workflows.yaml and agents file for load() and make_plan(); the skill is a stub folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        self.dir = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.dir, "claude", "skills", "release-companion"))
        with open(os.path.join(self.dir, "claude", "skills", "release-companion", "SKILL.md"), "w") as f:
            f.write("---\nname: release-companion\n---\n")
        os.makedirs(os.path.join(self.dir, "repo"))
        os.makedirs(os.path.join(self.dir, "files"))
        self.agents = os.path.join(self.dir, "agents.yaml")
        with open(self.agents, "w") as f:
            f.write(textwrap.dedent(f"""\
                agents:
                  claude: {{type: claude, command: claude, args: [], env: {{CLAUDE_CONFIG_DIR: {self.dir}/claude}}}}
                """))
        self.saved = (L.WORKFLOWS, L.AGENTS_CONFIG, L.workmux_prompt_is_ignored)
        L.WORKFLOWS, L.AGENTS_CONFIG = os.path.join(self.dir, "workflows.yaml"), self.agents
        L.workmux_prompt_is_ignored = lambda repo: True  # the stub repo is no git repo; the real one ignores .workmux/ (info/exclude)

    def tearDown(self):
        L.WORKFLOWS, L.AGENTS_CONFIG, L.workmux_prompt_is_ignored = self.saved
        self.tmp.cleanup()

    def workflows(self, **over):
        entry = {"agent": "claude", "skill": "release-companion", "worktree": True, "repo": f"{self.dir}/repo", "files": f"{self.dir}/files",
                 "key": "optimize-release", "case_label": "release version", "window": "release-{case}", "ask": ["case"],
                 "branch": "stable/optimize-{line}"}
        entry.update(over)
        import yaml
        with open(L.WORKFLOWS, "w") as f:
            yaml.safe_dump({"workflows": {"release": {k: v for k, v in entry.items() if v is not None}}}, f)
        return L.load()[1]

    def plan(self, case="8.6.30", **o):
        agents, wfs = L.load()
        return L.make_plan("release", wfs, agents, dict({"case": case}, **o))


class Config(ConfigBase):
    def test_the_branch_is_derived_from_the_version_and_the_window_is_named_for_it(self):
        self.workflows()
        for version, branch in (("8.6.30", "stable/optimize-8.6"), ("8.7.32", "stable/optimize-8.7"), ("v8.7.32", "stable/optimize-8.7")):
            p = self.plan(version)
            self.assertEqual(p["branch"], branch)
            self.assertEqual(p["window"], f"release-{p['case']}")
            self.assertEqual(p["prompt"], f"/release-companion {p['case']}")
            self.assertEqual(p["case_dir"], f"{self.dir}/files/{p['case']}")
            argv = L.worktree_argv(p, True)
            self.assertEqual(argv[:3], [L.WORKMUX, "add", branch])
            self.assertEqual(p["open_handle"], "", "no worktree yet: it is created")
            self.assertEqual(argv[argv.index("--target-name") + 1], f"release-{p['case']}")
            self.assertEqual(argv[argv.index("-p") + 1], f"/release-companion {p['case']}")

    def test_an_existing_worktree_is_reopened_with_the_prompt_not_added_again(self):
        # `workmux add -o` reopens the window but starts the agent WITHOUT the prompt (checked live, workmux 0.1.279); `open -p` has it
        self.workflows()
        repo = f"{self.dir}/repo"
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "a.txt")
        git(repo, "branch", "stable/optimize-8.6")
        git(repo, "worktree", "add", "-q", f"{self.dir}/wt/stable-optimize-8-6", "stable/optimize-8.6")
        p = self.plan("8.6.30")
        self.assertEqual(p["open_handle"], "stable-optimize-8-6")
        argv = L.worktree_argv(p, True)
        self.assertEqual(argv[:3], [L.WORKMUX, "open", "stable-optimize-8-6"])
        self.assertEqual(argv[argv.index("--target-name") + 1], "release-8.6.30")
        self.assertEqual(argv[argv.index("-p") + 1], "/release-companion 8.6.30")
        self.assertNotIn("add", argv)
        self.assertEqual(L.worktree_argv(p, False)[-1], "-b")
        self.assertIn("reopen the worktree stable-optimize-8-6", L.summary(p, {}, True))
        self.assertEqual(self.plan("8.7.32")["open_handle"], "", "the other line has no worktree yet")

    def test_the_main_checkout_is_never_taken_for_a_worktree_to_reopen(self):
        self.workflows()
        repo = f"{self.dir}/repo"
        git(repo, "init", "-q", "-b", "stable/optimize-8.6")
        commit(repo, "a.txt")
        self.assertEqual(self.plan("8.6.30")["open_handle"], "")

    def test_a_typed_branch_wins_over_the_derived_one(self):
        self.workflows()
        self.assertEqual(self.plan("8.6.30", branch="stable/optimize-8.5")["branch"], "stable/optimize-8.5")

    def test_two_releases_are_two_plans_that_do_not_share_a_window_or_a_folder(self):
        self.workflows()
        a, b = self.plan("8.6.30"), self.plan("8.7.32")
        self.assertNotEqual((a["window"], a["case_dir"], a["branch"]), (b["window"], b["case_dir"], b["branch"]))
        self.assertEqual({a["session"], b["session"]}, {"release"})

    def test_a_workflow_without_a_branch_template_still_needs_its_branch(self):
        self.workflows(branch=None, key="github-issue:camunda/camunda", window=None)
        agents, wfs = L.load()
        with self.assertRaises(L.Fail) as cm:
            L.make_plan("release", wfs, agents, {"case": "12345"})
        self.assertIn("needs a branch", str(cm.exception))

    def test_the_branch_field_is_checked_when_the_config_loads(self):
        for over, what in (({"worktree": False}, "needs `worktree: true`"), ({"key": "cve"}, "needs `key: optimize-release`"),
                           ({"branch": "stable/optimize-8"}, "must contain {line}"), ({"branch": "stable/{line}-{x}"}, "no other braces"),
                           ({"branch": "stable/optimize-{line"}, "must contain {line}")):
            with self.assertRaises(L.Fail, msg=str(over)) as cm:
                self.workflows(**over)
            self.assertIn(what, str(cm.exception))

    def test_the_real_release_entry_loads(self):
        real = os.path.expanduser("~/.config/workmux/config.yaml")
        if not os.path.exists(real):
            self.skipTest("no workmux agents config on this machine")
        L.WORKFLOWS, L.AGENTS_CONFIG = os.environ.get("WORKFLOWS_FILE") or os.path.expanduser("~/dotfiles/workmux/workflows.yaml"), real
        agents, wfs = L.load()
        w = wfs["release"]
        self.assertEqual((w["worktree"], w["branch_template"], w["key"], w["window"]), (True, "stable/optimize-{line}", "optimize-release", "release-{case}"))
        self.assertFalse(w["extra_dirs"], "worktree mode discards extra_dirs, so the entry should not carry any")
        self.assertEqual(w["case"], "", "a release is typed, never defaulted to a date")
        p = L.make_plan("release", wfs, agents, {"case": "8.7.32"})
        self.assertEqual((p["branch"], p["window"]), ("stable/optimize-8.7", "release-8.7.32"))


class Popup(ConfigBase):
    """run() from the popup, with the keyboard faked: pick() chooses the workflow, ask() answers by label."""

    def drive(self, tab, answers):
        import argparse
        self.workflows()
        self.asked, self.launched = [], []
        saved = (L.pick, L.ask, L.launch, L.confirm_launch)
        self.addCleanup(lambda: (setattr(L, "pick", saved[0]), setattr(L, "ask", saved[1]), setattr(L, "launch", saved[2]), setattr(L, "confirm_launch", saved[3])))

        def fake_ask(label, default="", complete=None):
            self.asked.append(label)
            return next((v for k, v in answers.items() if label.startswith(k)), default)
        L.pick = lambda wfs: ("release", tab)
        L.ask = fake_ask
        L.launch = lambda plan, agents, dry, switch, show=True: self.launched.append(plan)
        L.confirm_launch = lambda: True
        args = argparse.Namespace(workflow=[], agent=None, skill=None, model=None, effort=None, repo=None, files=None, case=None, branch=None,
                                  prompt=None, worktree=None, here=None, name=None, dry_run=False, no_switch=False)
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet:
            L.run(args)
        return self.launched[0]

    def test_enter_asks_only_for_the_version_never_for_a_branch(self):
        plan = self.drive(False, {"release version": "v8.7.32"})
        self.assertEqual(self.asked, ["release version"])
        self.assertEqual((plan["case"], plan["branch"], plan["window"], plan["prompt"]),
                         ("8.7.32", "stable/optimize-8.7", "release-8.7.32", "/release-companion 8.7.32"))

    def test_a_mistyped_version_is_asked_again_in_its_canonical_form(self):
        answers = iter(["8.7", "foo", "8.7.32-optimize"])
        saved_ask = None

        def stepping(label, default="", complete=None):
            self.asked.append(label)
            return next(answers)
        self.workflows()
        self.asked = []
        agents, wfs = L.load()
        saved_ask, L.ask = L.ask, stepping
        self.addCleanup(lambda: setattr(L, "ask", saved_ask))
        with contextlib.redirect_stdout(io.StringIO()):
            got = L.ask_case(wfs["release"], "", "")
        self.assertEqual(got, "8.7.32-optimize")
        self.assertEqual(self.asked, ["release version"] * 3)
        self.assertEqual(L.normalize_key("optimize-release", got), "8.7.32")

    def test_tab_offers_the_derived_branch_and_empty_keeps_it(self):
        plan = self.drive(True, {"release version": "8.6.30"})
        branch_asks = [a for a in self.asked if a.startswith("branch")]
        self.assertEqual(branch_asks, ["branch (empty = derived: stable/optimize-{line})"])
        self.assertEqual((plan["branch"], plan["window"]), ("stable/optimize-8.6", "release-8.6.30"))

    def test_tab_lets_you_name_another_branch(self):
        plan = self.drive(True, {"release version": "8.6.30", "branch": "stable/optimize-8.5"})
        self.assertEqual(plan["branch"], "stable/optimize-8.5")

    def test_tab_without_a_worktree_has_no_branch_to_ask_and_starts_in_the_repo(self):
        plan = self.drive(True, {"release version": "8.6.30", "new worktree": "n"})
        self.assertFalse([a for a in self.asked if a.startswith("branch")])
        self.assertEqual((plan["worktree"], plan["branch"]), (False, ""))


class Origin(unittest.TestCase):
    """A bare origin with main and stable/optimize-8.6 (pom 8.6.30-SNAPSHOT) and a clone on main, like the real checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        d = os.path.realpath(self.tmp.name)
        self.origin, self.repo, self.other = f"{d}/origin.git", f"{d}/repo", f"{d}/other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True, env=ENV)
        subprocess.run(["git", "clone", "-q", self.origin, self.repo], check=True, capture_output=True, env=ENV)
        git(self.repo, "checkout", "-q", "-B", "main")
        self.main_sha = commit(self.repo, "main.txt")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "checkout", "-q", "-b", "stable/optimize-8.6")
        os.makedirs(f"{self.repo}/optimize")
        self.stable_sha = commit(self.repo, "optimize/pom.xml", POM.format("8.6.30-SNAPSHOT"))
        git(self.repo, "push", "-q", "origin", "stable/optimize-8.6")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "branch", "-q", "-D", "stable/optimize-8.6")
        git(self.repo, "fetch", "-q", "origin")
        self.plan = {"repo": self.repo, "branch": "stable/optimize-8.6", "case": "8.6.30", "window": "release-8.6.30", "session": "release"}

    def tearDown(self):
        self.tmp.cleanup()

    def local(self, branch="stable/optimize-8.6"):
        r = subprocess.run(["git", "-C", self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], capture_output=True, text=True)
        return r.stdout.strip()

    def push_from_elsewhere(self, name="more.txt", pom=None):
        """Someone else adds a commit to origin's stable branch (a second clone); returns the new sha."""
        if not os.path.isdir(self.other):
            subprocess.run(["git", "clone", "-q", self.origin, self.other], check=True, capture_output=True, env=ENV)
        git(self.other, "checkout", "-q", "stable/optimize-8.6")
        sha = commit(self.other, name) if pom is None else commit(self.other, "optimize/pom.xml", pom)
        git(self.other, "push", "-q", "origin", "stable/optimize-8.6")
        return sha

    # ------------------------------------------------------------ prepare_branch

    def test_a_missing_local_branch_is_created_from_origins_branch_not_from_main(self):
        notes = L.prepare_branch(self.plan, dry=False)
        self.assertEqual(self.local(), self.stable_sha)
        self.assertNotEqual(self.local(), self.main_sha, "workmux would have made it from main")
        self.assertEqual(git(self.repo, "config", "--get", "branch.stable/optimize-8.6.remote"), "origin")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "stable/optimize-8.6@{upstream}"), "origin/stable/optimize-8.6")
        self.assertTrue(any("created local stable/optimize-8.6" in n for n in notes), notes)

    def test_a_branch_that_is_behind_is_fast_forwarded(self):
        L.prepare_branch(self.plan, dry=False)
        new = self.push_from_elsewhere()
        notes = L.prepare_branch(self.plan, dry=False)
        self.assertEqual(self.local(), new)
        self.assertTrue(any("fast-forwarded local stable/optimize-8.6 by 1 commit" in n for n in notes), notes)

    def test_a_branch_that_is_up_to_date_is_left_alone(self):
        L.prepare_branch(self.plan, dry=False)
        before = self.local()
        self.assertEqual(L.prepare_branch(self.plan, dry=False), [])
        self.assertEqual(self.local(), before)

    def test_a_diverged_branch_is_refused_and_not_touched(self):
        L.prepare_branch(self.plan, dry=False)
        git(self.repo, "checkout", "-q", "stable/optimize-8.6")
        mine = commit(self.repo, "mine.txt")
        git(self.repo, "checkout", "-q", "main")
        self.push_from_elsewhere()
        with self.assertRaises(L.Fail) as cm:
            L.prepare_branch(self.plan, dry=False)
        self.assertIn("diverged", str(cm.exception))
        self.assertEqual(self.local(), mine)

    def test_local_commits_origin_lacks_are_kept_with_a_note(self):
        L.prepare_branch(self.plan, dry=False)
        git(self.repo, "checkout", "-q", "stable/optimize-8.6")
        mine = commit(self.repo, "mine.txt")
        git(self.repo, "checkout", "-q", "main")
        notes = L.prepare_branch(self.plan, dry=False)
        self.assertEqual(self.local(), mine)
        self.assertTrue(any("1 commit(s) that origin does not" in n for n in notes), notes)

    def test_a_branch_checked_out_in_a_worktree_is_not_moved_from_outside(self):
        L.prepare_branch(self.plan, dry=False)
        wt = f"{self.tmp.name}/wt"
        git(self.repo, "worktree", "add", "-q", wt, "stable/optimize-8.6")
        before = self.local()
        self.push_from_elsewhere()
        notes = L.prepare_branch(self.plan, dry=False)
        self.assertEqual(self.local(), before)
        self.assertTrue(any("behind origin and checked out in" in n and "git merge --ff-only" in n for n in notes), notes)

    def test_a_branch_origin_does_not_have_is_refused_before_anything_is_created(self):
        for branch in ("stable/optimize-8.8", "stable/optimize", "optimize-8.6"):  # the last two only END like the real one
            with self.assertRaises(L.Fail, msg=branch) as cm:
                L.prepare_branch(dict(self.plan, branch=branch, case="8.8.1"), dry=False)
            self.assertIn(f"origin has no branch {branch}", str(cm.exception))
            self.assertEqual(self.local(branch), "")

    def test_a_glob_typed_in_the_tab_flow_is_not_a_branch(self):  # git ls-remote treats its pattern as a glob and would match the real one
        for branch in ("stable/optimize-*", "stable/optimize-8.?", "stable/*"):
            with self.assertRaises(L.Fail, msg=branch) as cm:
                L.prepare_branch(dict(self.plan, branch=branch), dry=False)
            self.assertIn(f"origin has no branch {branch}", str(cm.exception))
            self.assertEqual(self.local(), "", "nothing was created")

    def test_an_unreachable_origin_is_an_error_not_a_guess(self):
        git(self.repo, "remote", "set-url", "origin", f"{self.tmp.name}/nowhere.git")
        with self.assertRaises(L.Fail) as cm:
            L.prepare_branch(self.plan, dry=False)
        self.assertIn("cannot ask origin", str(cm.exception))

    def test_dry_run_looks_and_changes_nothing(self):
        refs = git(self.repo, "for-each-ref")
        notes = L.prepare_branch(self.plan, dry=True)
        self.assertEqual(git(self.repo, "for-each-ref"), refs)
        self.assertEqual(self.local(), "")
        self.assertTrue(any("would create local stable/optimize-8.6 tracking origin" in n for n in notes), notes)
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)

    def test_dry_run_still_refuses_a_line_origin_does_not_have(self):
        with self.assertRaises(L.Fail):
            L.prepare_branch(dict(self.plan, branch="stable/optimize-8.8", case="8.8.1"), dry=True)

    # ------------------------------------------------------------ the pom warning

    def test_no_warning_when_origin_names_the_typed_release_as_next(self):
        self.assertIsNone(L.pom_note(self.repo, "stable/optimize-8.6", "8.6.30"))
        self.assertEqual(L.prepare_branch(self.plan, dry=False), [f"created local stable/optimize-8.6 from origin/stable/optimize-8.6"])

    def test_a_different_next_version_warns_but_does_not_block(self):
        notes = L.prepare_branch(dict(self.plan, case="8.6.31"), dry=False)
        self.assertTrue(any("8.6.30-SNAPSHOT" in n and "not 8.6.31" in n for n in notes), notes)
        self.assertNotEqual(self.local(), "", "the launch goes on")

    def test_the_pom_is_read_after_the_fetch_so_a_moved_origin_is_noticed(self):
        self.push_from_elsewhere(pom=POM.format("8.6.31-SNAPSHOT"))  # 8.6.30 was released and merged back meanwhile
        notes = L.prepare_branch(self.plan, dry=False)  # the typed 8.6.30 is no longer the next one
        self.assertTrue(any("8.6.31-SNAPSHOT" in n and "not 8.6.30" in n for n in notes), notes)

    def test_a_missing_or_unreadable_pom_gives_no_warning(self):
        self.assertIsNone(L.pom_note(self.repo, "main", "8.6.30"))  # main has no optimize/pom.xml

    # ------------------------------------------------------------ worktree_of / relaunch

    def test_worktree_of_finds_the_branch_and_the_main_checkout(self):
        self.assertEqual(L.worktree_of(self.repo, "stable/optimize-8.6"), "")
        self.assertEqual(L.worktree_of(self.repo, "main"), self.repo)
        L.prepare_branch(self.plan, dry=False)
        wt = f"{self.tmp.name}/wt"
        git(self.repo, "worktree", "add", "-q", wt, "stable/optimize-8.6")
        self.assertEqual(L.worktree_of(self.repo, "stable/optimize-8.6"), os.path.realpath(wt))
        self.assertEqual(L.worktree_of(self.repo, "stable/optimize-8.6-x"), "", "an exact branch name, not a prefix")

    def with_worktree(self):
        L.prepare_branch(self.plan, dry=False)
        self.wt = os.path.realpath(f"{self.tmp.name}/wt")
        git(self.repo, "worktree", "add", "-q", self.wt, "stable/optimize-8.6")

    def with_windows(self, windows):
        saved = (L.windows_in, L.tmux)
        self.switched = []
        L.windows_in = lambda path: dict(windows)
        L.tmux = lambda *a: self.switched.append(a) or ""
        quiet = contextlib.redirect_stdout(io.StringIO())  # reopen_or_refuse reports "already open" on stdout
        quiet.__enter__()
        self.addCleanup(lambda: (quiet.__exit__(None, None, None), setattr(L, "windows_in", saved[0]), setattr(L, "tmux", saved[1])))

    def test_no_worktree_means_start_one(self):
        self.with_windows({"@9": "release-8-6-30"})  # even a stray window name does not matter without a worktree
        self.assertFalse(L.reopen_or_refuse(self.plan, True, False))

    def test_a_worktree_with_no_window_is_reopened_by_starting_one(self):
        self.with_worktree()
        self.with_windows({})
        self.assertFalse(L.reopen_or_refuse(self.plan, True, False))
        self.assertEqual(self.switched, [])

    def test_the_open_window_of_this_release_is_switched_to_and_nothing_starts(self):
        self.with_worktree()
        for name in ("release-8-6-30", "wm-release-8-6-30", "\uf135 release-8-6-30", "\U000f0c4crelease-8-6-30"):  # dots become dashes; a prefix may lead
            self.with_windows({"@9": name})
            self.assertTrue(L.reopen_or_refuse(self.plan, True, False), name)
            self.assertEqual(self.switched[-1], ("switch-client", "-t", "@9"))

    def test_with_no_switch_or_in_a_dry_run_it_does_not_touch_tmux(self):
        self.with_worktree()
        self.with_windows({"@9": "release-8-6-30"})
        self.assertTrue(L.reopen_or_refuse(self.plan, False, False))
        self.assertTrue(L.reopen_or_refuse(self.plan, True, True))
        self.assertEqual(self.switched, [])

    def test_another_release_of_the_same_line_is_refused_while_the_first_is_open(self):
        self.with_worktree()
        self.with_windows({"@9": "release-8-6-30"})
        with self.assertRaises(L.Fail) as cm:
            L.reopen_or_refuse(dict(self.plan, case="8.6.31", window="release-8.6.31"), True, False)
        self.assertIn("release-8-6-30", str(cm.exception))
        self.assertIn("One release per line at a time", str(cm.exception))
        self.assertEqual(self.switched, [])

    def test_the_other_line_is_not_in_the_way(self):  # 8.7 has its own branch, so its own worktree: nothing open under it
        self.with_worktree()
        self.with_windows({})
        p87 = dict(self.plan, branch="stable/optimize-8.7", case="8.7.32", window="release-8.7.32")
        self.assertFalse(L.reopen_or_refuse(p87, True, False))

    def test_a_branch_checked_out_in_the_main_checkout_is_refused_with_the_fix(self):
        git(self.repo, "branch", "-q", "--track", "stable/optimize-8.6", "origin/stable/optimize-8.6")
        git(self.repo, "checkout", "-q", "stable/optimize-8.6")
        self.with_windows({"@1": "change-1"})  # unrelated windows that sit in the main checkout
        with self.assertRaises(L.Fail) as cm:
            L.reopen_or_refuse(self.plan, True, False)
        self.assertIn("checked out in the main checkout", str(cm.exception))
        self.assertIn("switch main", str(cm.exception))


class WindowMatches(unittest.TestCase):
    def test_workmuxs_spellings_of_the_same_window_match(self):
        for name in ("release-8.6.30", "release-8-6-30", "wm-release-8-6-30", "\uf135 release-8-6-30", "\U000f0c4crelease-8-6-30"):
            self.assertTrue(L.window_matches(name, "release-8.6.30"), name)

    def test_another_release_or_a_longer_name_does_not(self):
        for name in ("release-8-6-3", "release-8-6-300", "xrelease-8-6-30", "release-8-7-30", "release-8-6-31", "wm-release-8-6-30-2", "zsh", ""):
            self.assertFalse(L.window_matches(name, "release-8.6.30"), name)


class WindowsIn(unittest.TestCase):
    def test_a_pane_inside_the_worktree_counts_a_lookalike_path_does_not(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH")) as d:
            d = os.path.realpath(d)
            wt, twin = f"{d}/stable-optimize-8-6", f"{d}/stable-optimize-8-6-old"
            os.makedirs(f"{wt}/optimize")
            os.makedirs(twin)
            out = f"@1\trelease-8-6-30\t{wt}\n@1\trelease-8-6-30\t{d}/files\n@2\tother\t{wt}/optimize\n@3\tlookalike\t{twin}\n@4\telsewhere\t/"
            saved = subprocess.run
            subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr="")
            try:
                self.assertEqual(L.windows_in(wt), {"@1": "release-8-6-30", "@2": "other"})
            finally:
                subprocess.run = saved

    def test_no_tmux_means_no_windows(self):
        saved = subprocess.run
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server")
        try:
            self.assertEqual(L.windows_in("/x"), {})
        finally:
            subprocess.run = saved


if __name__ == "__main__":
    unittest.main()
