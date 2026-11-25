"""Tests for trust_folder() in ~/dotfiles/tmux/popups/launcher.py: a fresh case folder is pre-trusted in the Claude or Copilot
agent's own config file, so the skill starts at once instead of waiting at "Do you trust this folder?" / "Confirm folder
trust" (INC-9355, 2026-11-25). OpenCode has no folder trust, so it is left alone.

Run:  python3 -B -m unittest tests.test_launcher_trust -v     (from tmux/popups)
Only throwaway config files are touched, never ~/.claude/.claude.json.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")  # the override is for mutation checks


def load():
    spec = importlib.util.spec_from_file_location("launcher", os.path.join(POPUPS, "launcher.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load()


class TrustFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfgdir = os.path.join(self.tmp.name, "profile")
        os.makedirs(self.cfgdir)
        self.cfg = os.path.join(self.cfgdir, ".claude.json")
        self.folder = os.path.join(self.tmp.name, "cases", "INC-1")
        os.makedirs(self.folder)
        self.key = os.path.realpath(self.folder)  # macOS: /var -> /private/var
        self.agent = {"type": "claude", "command": "claude", "env": {"CLAUDE_CONFIG_DIR": self.cfgdir}}

    def write(self, data, mode=0o600):
        with open(self.cfg, "w") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(self.cfg, mode)

    def read(self):
        with open(self.cfg) as f:
            return json.load(f)

    def test_adds_a_trusted_entry_and_keeps_everything_else(self):
        self.write({"numStartups": 5, "note": "é", "projects": {"/other": {"hasTrustDialogAccepted": False, "x": 1}}})
        note = L.trust_folder(self.agent, self.folder)
        self.assertIn("trusted", note)
        cfg = self.read()
        self.assertTrue(cfg["projects"][self.key]["hasTrustDialogAccepted"])
        self.assertEqual(cfg["projects"][self.key]["allowedTools"], [])
        self.assertEqual(cfg["projects"]["/other"], {"hasTrustDialogAccepted": False, "x": 1})
        self.assertEqual((cfg["numStartups"], cfg["note"]), (5, "é"))

    def test_output_keeps_claudes_formatting_and_the_file_mode(self):
        self.write({"projects": {}}, mode=0o644)
        L.trust_folder(self.agent, self.folder)
        with open(self.cfg) as f:
            raw = f.read()
        self.assertEqual(raw, json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
        self.assertFalse(raw.endswith("\n"))
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o644)

    def test_already_trusted_is_untouched(self):
        self.write({"projects": {self.key: {"hasTrustDialogAccepted": True, "keep": "me"}}})
        before = os.stat(self.cfg).st_mtime_ns
        self.assertEqual(L.trust_folder(self.agent, self.folder), "")
        self.assertEqual(os.stat(self.cfg).st_mtime_ns, before)

    def test_an_existing_untrusted_entry_is_upgraded_not_replaced(self):
        self.write({"projects": {self.key: {"hasTrustDialogAccepted": False, "allowedTools": ["Bash(ls)"]}}})
        L.trust_folder(self.agent, self.folder)
        e = self.read()["projects"][self.key]
        self.assertTrue(e["hasTrustDialogAccepted"])
        self.assertEqual(e["allowedTools"], ["Bash(ls)"])

    def test_missing_config_is_not_created(self):
        self.assertIn("agent will ask", L.trust_folder(self.agent, self.folder))
        self.assertEqual(os.listdir(self.cfgdir), [])

    def test_unreadable_config_is_left_alone_with_a_note(self):
        with open(self.cfg, "w") as f:
            f.write("{ not json")
        note = L.trust_folder(self.agent, self.folder)
        self.assertIn("agent will ask", note)
        with open(self.cfg) as f:
            self.assertEqual(f.read(), "{ not json")
        self.assertEqual([n for n in os.listdir(self.cfgdir) if n.endswith(".tmp")], [])

    def test_no_temp_file_is_left_behind(self):
        self.write({"projects": {}})
        L.trust_folder(self.agent, self.folder)
        self.assertEqual(os.listdir(self.cfgdir), [".claude.json"])

    def test_opencode_and_unknown_agents_are_skipped(self):
        self.write({"projects": {}})
        for a in ({"type": "opencode", "command": "opencode"}, {"command": "/x/opencode-openrouter"}, {"command": "other"}):
            self.assertEqual(L.trust_folder(a, self.folder), "")
        self.assertEqual(self.read(), {"projects": {}})

    def test_config_file_follows_the_profile(self):
        self.assertEqual(L.claude_config_file(self.agent), self.cfg)
        self.assertEqual(L.claude_config_file({"command": "claude"}), os.path.join(L.HOME, ".claude.json"))


HEADER = "// User settings belong in settings.json.\n// This file is managed automatically."


class TrustFolderCopilot(unittest.TestCase):
    """Copilot: `trustedFolders` in $COPILOT_HOME/config.json, which starts with // comment lines."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "copilot")
        os.makedirs(self.home)
        self.cfg = os.path.join(self.home, "config.json")
        self.folder = os.path.join(self.tmp.name, "cases", "INC-1")
        os.makedirs(self.folder)
        self.key = os.path.realpath(self.folder)
        # the wrapper's file name is what makes agent_kind say "copilot"
        self.agent = {"command": os.path.join(L.HOME, ".local/bin", L.COPILOT_WRAPPER), "env": {"COPILOT_HOME": self.home}}

    def write(self, data, header=HEADER, newline=True, mode=0o600):
        with open(self.cfg, "w") as f:
            f.write((header + "\n" if header else "") + json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if newline else ""))
        os.chmod(self.cfg, mode)

    def raw(self):
        with open(self.cfg) as f:
            return f.read()

    def folders(self):
        return json.loads("\n".join(l for l in self.raw().split("\n") if not l.startswith("//")))["trustedFolders"]

    def test_appends_the_folder_and_changes_nothing_else(self):
        self.write({"loggedInUsers": [{"login": "x"}], "trustedFolders": ["/a", "/b"], "recentModelIds": ["m"]})
        before = self.raw()
        self.assertIn("trusted", L.trust_folder(self.agent, self.folder))
        self.assertEqual(self.folders(), ["/a", "/b", self.key])
        self.assertEqual(self.raw(), before.replace('"/b"\n', '"/b",\n    "%s"\n' % self.key))  # one added line, nothing else

    def test_keeps_the_comment_header_the_final_newline_and_the_mode(self):
        self.write({"trustedFolders": []}, mode=0o600)
        L.trust_folder(self.agent, self.folder)
        raw = self.raw()
        self.assertTrue(raw.startswith(HEADER + "\n{"))
        self.assertTrue(raw.endswith("}\n"))
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        self.write({"trustedFolders": []}, newline=False)
        L.trust_folder(self.agent, self.folder)
        self.assertFalse(self.raw().endswith("\n"))

    def test_works_without_a_header_and_creates_a_missing_list(self):
        self.write({"other": 1}, header="")
        L.trust_folder(self.agent, self.folder)
        self.assertEqual(self.folders(), [self.key])
        self.assertTrue(self.raw().startswith("{"))

    def test_already_listed_is_untouched(self):
        self.write({"trustedFolders": [self.key]})
        before = os.stat(self.cfg).st_mtime_ns
        self.assertEqual(L.trust_folder(self.agent, self.folder), "")
        self.assertEqual(os.stat(self.cfg).st_mtime_ns, before)

    def test_unreadable_or_missing_config_is_left_alone_with_a_note(self):
        self.assertIn("agent will ask", L.trust_folder(self.agent, self.folder))  # missing
        self.assertFalse(os.path.exists(self.cfg))
        with open(self.cfg, "w") as f:
            f.write(HEADER + "\n{ not json")
        self.assertIn("agent will ask", L.trust_folder(self.agent, self.folder))
        self.assertEqual(self.raw(), HEADER + "\n{ not json")
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_a_held_lock_makes_it_give_up_and_leave_the_file_alone(self):
        self.write({"trustedFolders": []})
        before = self.raw()
        import fcntl
        with open(self.cfg + ".trusted-folders.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            note = L.trust_folder(self.agent, self.folder)
        self.assertIn("agent will ask", note)
        self.assertEqual(self.raw(), before)

    def test_config_file_follows_copilot_home(self):
        self.assertEqual(L.copilot_config_file(self.agent), self.cfg)
        self.assertEqual(L.copilot_config_file({"command": "copilot"}), os.path.join(L.HOME, ".copilot", "config.json"))


if __name__ == "__main__":
    unittest.main()
