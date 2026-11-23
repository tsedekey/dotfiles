"""Press the real Tab key on the launcher's model field: the real ask() line editor and the real fzf, in a pseudo-terminal.
test_launcher_models.py covers which models are offered; this covers that the key, the picker and the field work together.

Run:  SCRATCH=<dir> python3 -B -m unittest tests.test_launcher_model_tab -v     (from tmux/popups; skipped when fzf is missing)
The agent is a fixture Claude profile (a catalog and a settings.json in a temp folder), so no login, network or tmux is involved.
Two things a driver for this must do, both learned the hard way on 2026-11-19:
  - wait before pressing Tab: ask() draws the field and only THEN switches the terminal to key mode, which flushes pending
    input, so a Tab sent at once is thrown away (no human is that fast);
  - answer fzf's cursor-position query (ESC [ 6 n): in --height mode it waits for the terminal to reply.
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import sys
import tempfile
import termios
import time
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")
FZF = shutil.which("fzf", path=f"/opt/homebrew/bin:{os.environ.get('PATH', '')}")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")

CHILD = r'''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("L", sys.argv[1] + "/launcher.py")
L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
a = {"type": "claude", "command": "claude", "env": {"CLAUDE_CONFIG_DIR": sys.argv[2]}}
d = L.default_model(a)
print("RESULT=" + repr(L.ask("model", d, L.model_completer(a, [d]))))
'''


def catalog(*ids):
    return {"version": 2, "fetchedAt": 1, "catalog": {"config": {"models": [
        {"id": i, "short_name": i.split("-")[1].title(), "section": "main"} for i in ids]}}}


@unittest.skipUnless(FZF, "fzf is not installed")
class ModelTab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        self.profile = os.path.realpath(self.tmp.name)
        os.makedirs(f"{self.profile}/cache/model-catalog")
        with open(f"{self.profile}/cache/model-catalog/a-cc.json", "w") as f:
            json.dump(catalog("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"), f)
        with open(f"{self.profile}/settings.json", "w") as f:
            json.dump({"model": "sonnet"}, f)
        self.addCleanup(self.tmp.cleanup)

    def session(self):
        """Start the field in a pty. Returns (fd, pid); the field is drawn and ready for a key when it comes back."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-I", "-B", "-c", CHILD, POPUPS, self.profile])
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        self.buf, self.answered = b"", 0
        self.addCleanup(lambda: (os.close(fd), os.waitpid(pid, 0)))
        self.assertTrue(self.wait(fd, r"model\s+\(Tab = pick from a list\): sonnet", 5), self.text())
        self.settle(fd, 0.5)  # see the docstring: a Tab sent before ask() is in key mode is flushed
        return fd

    def text(self):
        return ANSI.sub("", self.buf.decode(errors="ignore"))

    def wait(self, fd, pattern, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return bool(re.search(pattern, self.text()))
                if not chunk:
                    break
                self.buf += chunk
                while self.answered < self.buf.count(b"\x1b[6n"):  # fzf's "where is the cursor?"
                    os.write(fd, b"\x1b[10;1R")
                    self.answered += 1
            if re.search(pattern, self.text()):
                return True
        return False

    def settle(self, fd, seconds):
        """Wait, but keep draining the terminal: a driver that sleeps without reading lets fzf's redraws back up, and fzf then
        lags behind the keys it was sent (Enter lands before the filter has caught up)."""
        self.wait(fd, r"(?!x)x", seconds)  # a pattern that never matches: wait() just reads until the time is up

    def result(self, fd):
        os.write(fd, b"\r")
        self.assertTrue(self.wait(fd, r"RESULT=", 5), self.text())
        return re.search(r"RESULT=(.*)", self.text())[1].strip()

    def test_tab_opens_the_list_and_the_picked_model_lands_in_the_field(self):
        fd = self.session()
        os.write(fd, b"\t")
        self.assertTrue(self.wait(fd, r"enter: pick", 5), "Tab did not open the picker: " + self.text())
        os.write(fd, b"haiku-5")  # filter the list down to one line (the alias `haiku` has no "-5")
        self.assertTrue(self.wait(fd, r"model> haiku-5", 5), self.text())
        self.settle(fd, 0.6)
        os.write(fd, b"\r")  # pick it
        self.assertTrue(self.wait(fd, r"model.*: claude-haiku-5-5", 5), self.text())
        self.assertEqual(self.result(fd), "'claude-haiku-5-5'")

    def test_the_list_holds_the_default_the_aliases_and_the_catalogs_models(self):
        fd = self.session()
        os.write(fd, b"\t")
        self.assertTrue(self.wait(fd, r"claude-haiku-5-5", 5), self.text())
        shown = self.text()
        for name in ("sonnet", "opus", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
            self.assertIn(name, shown)

    def test_leaving_the_picker_with_esc_keeps_what_the_field_held(self):
        fd = self.session()
        os.write(fd, b"\t")
        self.assertTrue(self.wait(fd, r"enter: pick", 5), self.text())
        os.write(fd, b"\x1b")
        self.settle(fd, 0.5)
        self.assertEqual(self.result(fd), "'sonnet'")

    def test_enter_without_tab_takes_the_default_the_field_started_with(self):
        self.assertEqual(self.result(self.session()), "'sonnet'")


if __name__ == "__main__":
    unittest.main()
