"""A worktree launch of a Copilot agent: the first prompt reaches it only through the copilot-prompt wrapper.

Why (2026-11-23): `backport` launched with agent copilot started three bare Copilots, because workmux hands a worktree's
prompt over as `-- <prompt>`, which Copilot rejects; the launcher dropped the prompt (with a note) and the idle Copilots never
registered with workmux, so they never showed in the sidebar. The copilot agents' command is now the wrapper
(dotfiles/workmux/copilot-prompt), which turns `-- <prompt>` into `-i <prompt>`; the launcher keeps the prompt for it.

Run:  SCRATCH=<dir> python3 -B -m unittest tests.test_launcher_copilot_worktree -v     (from tmux/popups)
Nothing starts: make_plan and worktree_argv only build the command; the pure rules are the launcher's own.
"""
import importlib.util
import os
import sys
import tempfile
import textwrap
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(POPUPS, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load("launcher_copilot_worktree", "launcher.py")
SKILL = "backport-resolve"
TARGET = {"head": "backport-69953-to-stable/8.8", "number": 70001, "original": "69953", "target": "8.8", "title": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        self.dir = os.path.realpath(self.tmp.name)
        for home in ("claude", "copilot"):
            os.makedirs(os.path.join(self.dir, home, "skills", SKILL))
            with open(os.path.join(self.dir, home, "skills", SKILL, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {SKILL}\n---\n")
        os.makedirs(os.path.join(self.dir, "repo"))
        self.agents = os.path.join(self.dir, "agents.yaml")
        with open(self.agents, "w") as f:
            f.write(textwrap.dedent(f"""\
                agents:
                  claude: {{type: claude, command: claude, args: [], env: {{CLAUDE_CONFIG_DIR: {self.dir}/claude}}}}
                  wrapped: {{command: /home/x/.local/bin/copilot-prompt, args: [--allow-all], env: {{COPILOT_HOME: {self.dir}/copilot}}}}
                  plain: {{command: copilot, args: [--allow-all], env: {{COPILOT_HOME: {self.dir}/copilot}}}}
                """))
        with open(os.path.join(self.dir, "workflows.yaml"), "w") as f:
            f.write(textwrap.dedent(f"""\
                workflows:
                  backport:
                    backports: true
                    worktree: true
                    agent: wrapped
                    skill: {SKILL}
                    repo: {self.dir}/repo
                    key: github-pr:camunda/camunda
                    case_label: original PR
                    case_arg: false
                    ask: [case]
                """))
        self.saved = (L.WORKFLOWS, L.AGENTS_CONFIG, L.workmux_prompt_is_ignored)
        L.WORKFLOWS, L.AGENTS_CONFIG = os.path.join(self.dir, "workflows.yaml"), self.agents
        L.workmux_prompt_is_ignored = lambda repo: True  # the stub repo is no git repo; the real one ignores .workmux/ (info/exclude)

    def tearDown(self):
        L.WORKFLOWS, L.AGENTS_CONFIG, L.workmux_prompt_is_ignored = self.saved
        self.tmp.cleanup()

    def plan(self, agent=None):
        agents, wfs = L.load()
        return L.make_plan("backport", wfs, agents, {"case": "69953", "pr_target": TARGET, "agent": agent}), agents


class WorktreePrompt(Base):
    def test_a_wrapped_copilot_keeps_its_prompt(self):
        p, _ = self.plan()
        self.assertEqual(p["agent"], "wrapped")
        self.assertEqual(p["prompt"], f"/{SKILL}")
        self.assertEqual(p["notes"], [])  # no "type /backport-resolve yourself" note

    def test_the_prompt_goes_to_workmux_add(self):
        p, _ = self.plan()
        argv = L.worktree_argv(p, True)
        self.assertEqual(argv[argv.index("-a") + 1], "wrapped")
        self.assertEqual(argv[argv.index("-p") + 1], f"/{SKILL}")

    def test_a_plain_copilot_command_still_drops_it_with_a_note(self):
        # workmux would type `copilot --allow-all -- <prompt>` and Copilot rejects the `--`: say so instead of starting a broken agent
        p, _ = self.plan("plain")
        self.assertEqual(p["prompt"], "")
        self.assertTrue(any("cannot hand the prompt" in n and f"/{SKILL}" in n for n in p["notes"]), p["notes"])
        self.assertNotIn("-p", L.worktree_argv(p, True))

    def test_claude_is_unchanged(self):
        p, _ = self.plan("claude")
        self.assertEqual(p["prompt"], f"/{SKILL}")
        self.assertEqual(p["notes"], [])

    def test_the_wrapper_counts_as_copilot(self):
        agents, _ = L.load()
        self.assertEqual(L.agent_kind(agents["wrapped"]), "copilot")  # effort flag, model list, skill folder all follow from this
        self.assertEqual(L.skill_roots(agents["wrapped"]), [f"{self.dir}/copilot/skills"])
        self.assertTrue(L.takes_workmux_prompt(agents["wrapped"]))
        self.assertFalse(L.takes_workmux_prompt(agents["plain"]))
        self.assertTrue(L.takes_workmux_prompt(agents["claude"]))


class NonWorktreeLaunchStaysTheSame(Base):
    def test_the_launchers_own_command_line_uses_dash_i(self):
        agents, _ = L.load()
        line = L.agent_line(agents, "wrapped", "", f"/{SKILL}")
        self.assertIn("/home/x/.local/bin/copilot-prompt --allow-all -i /backport-resolve", line)  # the wrapper passes `-i` through untouched


class RealConfig(unittest.TestCase):
    """The decision itself (Eddie, 2026-11-23): backport defaults to Copilot, with the prompt, on this machine's real config."""

    def test_backport_defaults_to_copilot_and_keeps_the_prompt(self):
        real = os.path.expanduser("~/.config/workmux/config.yaml")
        if not os.path.exists(real):
            self.skipTest("no workmux agents config on this machine")
        saved = (L.WORKFLOWS, L.AGENTS_CONFIG)
        L.WORKFLOWS, L.AGENTS_CONFIG = os.environ.get("WORKFLOWS_FILE") or os.path.expanduser("~/dotfiles/workmux/workflows.yaml"), real
        try:
            agents, wfs = L.load()
            self.assertEqual(wfs["backport"]["agent"], "copilot")
            self.assertEqual(os.path.basename(agents["copilot"]["command"]), "copilot-prompt")
            self.assertEqual(os.path.basename(agents["copilot-long"]["command"]), "copilot-prompt")
            p = L.make_plan("backport", wfs, agents, {"case": "69953", "pr_target": TARGET})
            self.assertEqual((p["agent"], p["prompt"], p["notes"]), ("copilot", f"/{SKILL}", []))
        finally:
            L.WORKFLOWS, L.AGENTS_CONFIG = saved


if __name__ == "__main__":
    unittest.main()
