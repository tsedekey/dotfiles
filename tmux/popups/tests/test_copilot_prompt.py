"""workmux/copilot-prompt: workmux's trailing `-- <prompt>` becomes Copilot's `-i <prompt>`; everything else is untouched.

The real Copilot is replaced by a stub that prints its argv, NUL-separated, so spaces, quotes and newlines in a prompt
are compared exactly. Run: python3 -m unittest tmux/popups/tests/test_copilot_prompt.py
"""
import os
import stat
import subprocess
import tempfile
import unittest

WRAPPER = os.path.expanduser("~/dotfiles/workmux/copilot-prompt")


class CopilotPrompt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.stub = os.path.join(cls.tmp.name, "copilot-stub")
        with open(cls.stub, "w") as f:
            f.write("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\0' \"$a\"; done\n")  # a loop: printf with no args still prints once
        os.chmod(cls.stub, os.stat(cls.stub).st_mode | stat.S_IXUSR)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_wrapper(self, *argv):
        r = subprocess.run([WRAPPER, *argv], capture_output=True, env={**os.environ, "COPILOT_PROMPT_REAL": self.stub})
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode()
        return out.split("\0")[:-1] if out else []

    def test_workmuxs_dash_dash_prompt_becomes_dash_i(self):
        self.assertEqual(self.run_wrapper("--allow-all", "--", "/backport-resolve"), ["--allow-all", "-i", "/backport-resolve"])

    def test_the_args_before_it_keep_their_order(self):
        self.assertEqual(self.run_wrapper("--allow-all", "--context", "long_context", "--", "/review-companion 70143"),
                         ["--allow-all", "--context", "long_context", "-i", "/review-companion 70143"])

    def test_only_a_prompt(self):
        self.assertEqual(self.run_wrapper("--", "/x"), ["-i", "/x"])

    def test_prompt_is_passed_exactly(self):
        prompt = "line one\n  it's \"quoted\" $HOME `ls` *\n"
        self.assertEqual(self.run_wrapper("--allow-all", "--", prompt), ["--allow-all", "-i", prompt])

    def test_empty_prompt_drops_the_dash_dash_too(self):
        self.assertEqual(self.run_wrapper("--allow-all", "--", ""), ["--allow-all"])

    def test_the_launchers_own_dash_i_is_not_touched(self):
        self.assertEqual(self.run_wrapper("--allow-all", "--model", "m", "-i", "/review-companion 69548", "--add-dir", "/d"),
                         ["--allow-all", "--model", "m", "-i", "/review-companion 69548", "--add-dir", "/d"])

    def test_calls_without_a_prompt_pass_through(self):
        self.assertEqual(self.run_wrapper("--allow-all"), ["--allow-all"])
        self.assertEqual(self.run_wrapper("help", "config"), ["help", "config"])
        self.assertEqual(self.run_wrapper("--version"), ["--version"])
        self.assertEqual(self.run_wrapper(), [])

    def test_dash_dash_that_is_not_second_to_last_is_left_alone(self):
        # the rule is exactly "last two are `--` and a prompt"; anything else is not workmux's shape
        self.assertEqual(self.run_wrapper("--", "a", "b"), ["--", "a", "b"])
        self.assertEqual(self.run_wrapper("a", "--", "b", "c"), ["a", "--", "b", "c"])

    def test_the_real_copilot_is_exec_ed_not_wrapped(self):
        # exec: the process the pane runs is the real binary (workmux's agent_kind and tmux-resurrect look at it)
        probe = os.path.join(self.tmp.name, "pid-stub")
        with open(probe, "w") as f:
            f.write("#!/bin/sh\necho $PPID\n")
        os.chmod(probe, 0o755)
        p = subprocess.Popen([WRAPPER, "--", "x"], stdout=subprocess.PIPE, env={**os.environ, "COPILOT_PROMPT_REAL": probe})
        out = p.communicate()[0].decode().strip()
        # with exec the stub's parent is whoever started the wrapper (this test), not a wrapper process
        self.assertEqual(out, str(os.getpid()))


if __name__ == "__main__":
    unittest.main()
