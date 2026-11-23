"""Tests for the model list of ~/dotfiles/tmux/popups/launcher.py: what Tab offers on the model field of the edit flow, and
which model that field starts with.

Run:  SCRATCH=<dir> python3 -B -m unittest tests.test_launcher_models -v     (from tmux/popups)
Nothing here reads the real profiles or logins: Claude's catalog and settings are fixture folders, and `copilot` / `opencode`
are stub scripts that print what the real ones print (checked by hand against copilot 1.0.114 and opencode on 2026-11-19).
The Tab key and fzf themselves are covered in a pseudo-terminal by tests/test_launcher_model_tab.py, not here.
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
POPUPS = os.environ.get("POPUPS_DIR") or os.path.expanduser("~/dotfiles/tmux/popups")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(POPUPS, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load("launcher_models_under_test", "launcher.py")

COPILOT_HELP = """\
  `renderMarkdown`: Render markdown.
  `model`: AI model to use for Copilot CLI. Refer to `copilot help providers`.
    - "claude-sonnet-5.5"
    - "claude-opus-5.5"
    - "gpt-6.2-sol"
  `modelProviderId`: Provider owning the selected model.
    - "not-a-model"
"""


def catalog(*models, fetched=1):
    return {"version": 2, "fetchedAt": fetched, "catalog": {"config": {"models": [
        {"id": i, "short_name": s, "section": sec} for i, s, sec in models]}}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("SCRATCH"))
        self.dir = os.path.realpath(self.tmp.name)
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.saved_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.saved_path}"  # the stubs win over a real copilot / opencode

    def tearDown(self):
        os.environ["PATH"] = self.saved_path
        self.tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def stub(self, name, body):
        """An executable `name` on PATH; it appends its argv and COPILOT_HOME to <dir>/<name>.calls first."""
        path = self.write(f"bin/{name}", f'#!/bin/sh\necho "$@ home=$COPILOT_HOME" >> "{self.dir}/{name}.calls"\n{body}\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def calls(self, name):
        try:
            with open(os.path.join(self.dir, f"{name}.calls")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def claude(self, **settings):
        """An agent whose profile is <dir>/profile, with the given settings.json."""
        if settings:
            self.write("profile/settings.json", json.dumps(settings))
        return {"type": "claude", "command": "claude", "env": {"CLAUDE_CONFIG_DIR": f"{self.dir}/profile"}}


class ClaudeCatalog(Fixture):
    def put(self, name, doc):
        self.write(f"profile/cache/model-catalog/{name}", json.dumps(doc))

    def test_main_models_come_first_with_their_aliases_then_the_overflow(self):
        self.put("a-cc.json", catalog(("claude-opus-5-5", "Opus", "main"), ("claude-sonnet-5-5", "Sonnet", "main"),
                                       ("claude-opus-4-8", "Opus", "overflow")))
        self.assertEqual(L.claude_models(self.claude()), ["opus", "sonnet", "claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8"])

    def test_the_newest_catalog_wins_when_there_are_several(self):
        self.put("old-cc.json", catalog(("claude-sonnet-4-6", "Sonnet", "main"), fetched=1))
        self.put("new-cc.json", catalog(("claude-sonnet-5-5", "Sonnet", "main"), fetched=2))
        self.assertEqual(L.claude_models(self.claude()), ["sonnet", "claude-sonnet-5-5"])

    def test_other_cache_files_are_not_catalogs(self):  # the profile also holds tok-*-ccd.json, published-floor.json
        self.put("tok-5a-ccd.json", catalog(("claude-wrong", "Wrong", "main"), fetched=99))
        self.put("published-floor.json", {"version": 1})
        self.put("a-cc.json", catalog(("claude-sonnet-5-5", "Sonnet", "main")))
        self.assertEqual(L.claude_models(self.claude()), ["sonnet", "claude-sonnet-5-5"])

    def test_masked_models_are_hidden_like_in_the_picker(self):
        self.put("a-cc.json", catalog(("claude-sonnet-5-5", "Sonnet", "main"), ("claude-mythos-5-2", "Mythos", "main")))
        self.put("masked-ids.json", {"version": 1, "ids": ["claude-mythos-5-2"]})
        self.assertNotIn("claude-mythos-5-2", L.claude_models(self.claude()))

    def test_a_broken_or_missing_catalog_is_an_empty_list_not_an_error(self):
        self.assertEqual(L.claude_models(self.claude()), [])
        self.write("profile/cache/model-catalog/a-cc.json", "{not json")
        self.write("profile/cache/model-catalog/b-cc.json", json.dumps({"fetchedAt": 1}))
        self.assertEqual(L.claude_models(self.claude()), [])

    def test_the_default_is_listed_first_and_once(self):
        self.put("a-cc.json", catalog(("claude-opus-5-5", "Opus", "main"), ("claude-sonnet-5-5", "Sonnet", "main")))
        self.assertEqual(L.models_for(self.claude(model="sonnet"))[:3], ["sonnet", "opus", "claude-opus-5-5"])
        self.assertEqual(L.models_for(self.claude(model="sonnet")).count("sonnet"), 1)

    def test_without_a_catalog_the_default_is_still_there_to_pick(self):
        self.assertEqual(L.models_for(self.claude(model="sonnet")), ["sonnet"])


class Defaults(Fixture):
    def test_an_agents_own_model_flag_beats_the_harness_setting(self):
        a = {"type": "opencode", "command": "opencode-x", "args": ["--model", "openrouter/x/y"]}
        self.assertEqual(L.default_model(a), "openrouter/x/y")
        self.assertEqual(L.default_model({**a, "args": ["--model=openrouter/x/z"]}), "openrouter/x/z")

    def test_claude_and_copilot_read_the_model_from_their_own_profile(self):
        self.assertEqual(L.default_model(self.claude(model="opus")), "opus")
        self.write("copilot/settings.json", json.dumps({"model": "claude-sonnet-5.5"}))
        self.assertEqual(L.default_model({"command": "copilot", "env": {"COPILOT_HOME": f"{self.dir}/copilot"}}), "claude-sonnet-5.5")

    def test_no_settings_means_no_default(self):
        self.assertEqual(L.default_model(self.claude()), "")
        self.write("profile/settings.json", "{broken")
        self.assertEqual(L.default_model(self.claude()), "")
        self.write("profile/settings.json", json.dumps({"model": 5}))
        self.assertEqual(L.default_model(self.claude()), "")

    def test_an_agent_that_is_neither_has_no_default_and_no_list(self):
        self.assertEqual((L.default_model({"command": "mystery"}), L.models_for({"command": "mystery"})), ("", []))


class Copilot(Fixture):
    agent = {"command": "copilot", "env": {}}

    def test_the_models_come_from_the_model_block_of_help_config_and_auto_is_added(self):
        self.stub("copilot", f"cat <<'EOF'\n{COPILOT_HELP}EOF")
        self.assertEqual(L.copilot_models(self.agent), ["auto", "claude-sonnet-5.5", "claude-opus-5.5", "gpt-6.2-sol"])
        self.assertEqual(self.calls("copilot"), ["help config home="])

    def test_the_agents_copilot_home_is_passed_on(self):
        self.stub("copilot", f"cat <<'EOF'\n{COPILOT_HELP}EOF")
        L.copilot_models({"command": "copilot", "env": {"COPILOT_HOME": "/some/home"}})
        self.assertEqual(self.calls("copilot"), ["help config home=/some/home"])

    def test_a_failing_copilot_gives_no_list_and_models_for_keeps_the_default(self):
        self.stub("copilot", "exit 3")
        self.assertEqual(L.copilot_models(self.agent), [])
        self.write("copilot/settings.json", json.dumps({"model": "claude-sonnet-5.5"}))
        a = {"command": "copilot", "env": {"COPILOT_HOME": f"{self.dir}/copilot"}}
        self.assertEqual(L.models_for(a), ["claude-sonnet-5.5"])

    def test_a_help_text_without_a_model_block_gives_no_list(self):
        self.stub("copilot", "echo nothing to see")
        self.assertEqual(L.copilot_models(self.agent), [])


class OpenCode(Fixture):
    def agent(self):
        return {"type": "opencode", "command": f"{self.dir}/opencode-wrapper", "args": ["--model", "openrouter/deepseek/flash"]}

    def test_it_lists_the_provider_of_the_agents_own_model_with_the_plain_opencode(self):
        self.stub("opencode", "printf 'openrouter/a/one\\nopenrouter/b/two\\n\\n'")
        self.assertEqual(L.opencode_models(self.agent()), ["openrouter/a/one", "openrouter/b/two"])
        self.assertEqual(self.calls("opencode"), ["models openrouter home="], "the wrapper would have read the keychain; only `opencode models <provider>` runs")

    def test_without_a_provider_in_the_default_every_provider_is_listed(self):
        self.stub("opencode", "echo anthropic/x")
        L.opencode_models({"type": "opencode", "command": "opencode"})
        self.assertEqual(self.calls("opencode"), ["models home="])

    def test_the_default_leads_the_list_without_being_repeated(self):
        self.stub("opencode", "printf 'openrouter/a/one\\nopenrouter/deepseek/flash\\n'")
        self.assertEqual(L.models_for(self.agent()), ["openrouter/deepseek/flash", "openrouter/a/one"])

    def test_a_missing_opencode_is_not_an_error(self):
        os.environ["PATH"] = self.bin  # nothing on PATH at all
        self.assertEqual(L.models_for(self.agent()), ["openrouter/deepseek/flash"])


class Completer(Fixture):
    def setUp(self):
        super().setUp()
        self.stub("copilot", f"cat <<'EOF'\n{COPILOT_HELP}EOF")
        self.write("copilot/settings.json", json.dumps({"model": "claude-sonnet-5.5"}))
        self.a = {"command": "copilot", "env": {"COPILOT_HOME": f"{self.dir}/copilot"}}

    def test_nothing_is_looked_up_until_tab_is_pressed_and_then_only_once(self):
        complete = L.model_completer(self.a, ["claude-sonnet-5.5"])
        self.assertEqual(self.calls("copilot"), [])
        complete("")
        complete("gpt")
        self.assertEqual(len(self.calls("copilot")), 1)

    def test_the_field_holding_one_known_model_lists_them_all_a_typed_start_narrows(self):
        complete = L.model_completer(self.a, ["claude-sonnet-5.5"])
        head, prefix, names, tail = complete("claude-sonnet-5.5")
        self.assertEqual((head, prefix, tail), ("", "", ""))
        self.assertEqual(names, ["claude-sonnet-5.5", "auto", "claude-opus-5.5", "gpt-6.2-sol"])
        self.assertEqual(complete("gpt")[1], "gpt")

    def test_a_blank_default_is_never_a_list_entry(self):
        self.assertNotIn("", L.model_completer(self.a, [""])("")[2])

    def test_tab_on_the_field_replaces_it_with_the_picked_model(self):
        complete = L.model_completer(self.a, ["claude-sonnet-5.5"])
        shown = []
        saved = L.pick_one
        L.pick_one = lambda items, label: shown.append(list(items)) or "gpt-6.2-sol"
        self.addCleanup(lambda: setattr(L, "pick_one", saved))
        self.assertEqual(L.browse("model", "claude-sonnet-5.5", complete), "gpt-6.2-sol")
        self.assertEqual(shown, [["claude-sonnet-5.5", "auto", "claude-opus-5.5", "gpt-6.2-sol"]])

    def test_a_unique_start_is_completed_without_asking(self):
        self.assertEqual(L.browse("model", "gpt", L.model_completer(self.a)), "gpt-6.2-sol")


class AskModel(Fixture):
    """ask_model_effort: which model the field starts with, and whether it can offer a list."""

    def run_ask(self, wf_agent, wf_model, agent_name, agents):
        asked = []

        def fake_ask(label, default="", complete=None):
            asked.append((label, default, complete))
            return default
        saved = L.ask
        L.ask = fake_ask
        self.addCleanup(lambda: setattr(L, "ask", saved))
        o = {"agent": agent_name, "effort": ""}
        with contextlib.redirect_stdout(io.StringIO()):
            L.ask_model_effort({"agent": wf_agent, "model": wf_model, "effort": ""}, agents, o)
        return o, asked[0]

    def agents(self):
        self.write("copilot/settings.json", json.dumps({"model": "claude-sonnet-5.5"}))
        return {"claude": self.claude(model="sonnet"),
                "copilot": {"command": "copilot", "env": {"COPILOT_HOME": f"{self.dir}/copilot"}},
                "mystery": {"command": "mystery"}}

    def test_the_workflows_own_model_is_what_the_field_starts_with(self):
        o, (label, default, complete) = self.run_ask("copilot", "claude-opus-5.5", "copilot", self.agents())
        self.assertEqual((o["model"], default), ("claude-opus-5.5", "claude-opus-5.5"))
        self.assertIsNotNone(complete)

    def test_a_workflow_without_a_model_shows_the_harness_default_but_does_not_pin_it(self):
        o, (_, default, _) = self.run_ask("claude", "", "claude", self.agents())
        self.assertEqual((default, o["model"]), ("sonnet", ""), "Enter on the shown default sends no --model: the harness decides, as before")

    def test_another_agent_starts_from_its_own_default_never_the_old_agents_model(self):
        # the workflow's claude-sonnet-5-5 is a Claude spelling; Copilot's is claude-sonnet-5.5
        o, (label, default, _) = self.run_ask("claude", "claude-sonnet-5-5", "copilot", self.agents())
        self.assertEqual((default, o["model"]), ("claude-sonnet-5.5", ""))
        self.assertIn("agent changed", label)

    def test_a_model_you_pick_is_sent(self):
        agents = self.agents()
        saved = L.ask
        L.ask = lambda label, default="", complete=None: "claude-opus-5-5" if label.startswith("model") else default
        self.addCleanup(lambda: setattr(L, "ask", saved))
        for wf_model in ("", "sonnet"):  # with and without a model of its own
            o = {"agent": "claude", "effort": ""}
            L.ask_model_effort({"agent": "claude", "model": wf_model, "effort": ""}, agents, o)
            self.assertEqual(o["model"], "claude-opus-5-5")

    def test_the_workflows_own_model_is_kept_even_when_it_equals_the_agents_default(self):
        o, _ = self.run_ask("claude", "sonnet", "claude", self.agents())
        self.assertEqual(o["model"], "sonnet", "the workflow chose it; it must not turn into 'whatever the harness does'")

    def test_an_agent_with_neither_a_list_nor_a_default_is_asked_plainly(self):
        o, (_, default, complete) = self.run_ask("claude", "x", "mystery", self.agents())
        self.assertEqual((o["model"], default, complete), ("", "", None))

    def test_clearing_the_field_still_means_the_harness_default(self):
        agents = self.agents()
        saved = L.ask
        L.ask = lambda label, default="", complete=None: "" if label.startswith("model") else default
        self.addCleanup(lambda: setattr(L, "ask", saved))
        o = {"agent": "claude", "effort": ""}
        L.ask_model_effort({"agent": "claude", "model": "", "effort": ""}, agents, o)
        self.assertEqual(o["model"], "")
        self.assertEqual(L.agent_line(agents, "claude", o["model"]).count("--model"), 0)


if __name__ == "__main__":
    unittest.main()
