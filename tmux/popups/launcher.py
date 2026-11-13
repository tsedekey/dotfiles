#!/usr/bin/env python3
"""tmux popup: start an agent window for a workflow.   Bound in tmux.conf as: prefix + N

  popup    Pick a workflow in fzf.  Enter = launch with its defaults (a workflow may first ask for its case,
           and for a repo).  Tab = edit every field first.  In the path fields (repo, files, case, skill)
           Tab lists what is in the folder typed so far and you pick from it instead of looking it up.
  CLI      launcher.py review [--agent A] [--skill S] [--model M] [--effort E] [--name N] [--repo R] [--files F]
           [--case C] [--branch B] [--prompt P] [--[no-]worktree] [--dry-run] [--no-switch].
           Naming a workflow skips the UI and NEVER prompts (scripts, tests and other agents use this;
           hidden workflows such as `dagrunner` can only be started this way).

A workflow is five things (defaults in dotfiles/workmux/workflows.yaml, explained there):
  agent + model   which harness runs, from the `agents:` block of ~/.config/workmux/config.yaml
  effort          optional reasoning effort (low|medium|high|xhigh|max): `--effort` for Claude,
                  `--reasoning-effort` for Copilot; empty = the harness' own setting (effortLevel)
  skill           started as the agent's first prompt: `/<skill> <case>`
  repo            optional git repo the agent works in; a worktree, when asked for, branches off it
  files + case    where the session's artifacts live: <files>/<case>/. The right-hand shell pane opens
                  there. The launcher creates the case folder; the files root must already exist
  extra_dirs      more folders the agent may read (passed as --add-dir)
The case is the one value you type, and where a workflow has a `key:` (GitHub issue or PR, incident.io
ref, CVE ID) it is a KEY: the case folder, the window (<workflow>-<key>) and the skill's argument are all
that same value, and a pasted URL or a wrong-case spelling is turned into the canonical one (normalize_key).
`key_repo_root` (change) nests the key: an issue in another repo is `<short repo>/<number>` (pdp-3517/153), so the
case folder is <files>/pdp-3517/153, the window change-pdp-3517-153, and the prompt carries `repo=<full name>`.
The epic workflow is the exception: it asks WHICH epic first (`workspaces:`, the one launched last is offered),
and the key is a task issue of that epic; the task has no folder of its own (see workflows.yaml).
The agent starts in the repo (or its new worktree) when there is one, otherwise in the case folder.
`reuse: true` (daily): launching again switches to the window that is already open instead of starting a
second one; `case_arg: false` keeps the case out of the skill's prompt.
The session is the workflow's first word and is created when missing.  Every window is monitored
because the agents' own workmux hooks register the pane; nothing here talks to workmux except
`worktree: true`, which runs `workmux add`.

Env: LAUNCHER_WORKFLOWS / LAUNCHER_AGENTS_CONFIG override the two config paths (tests use them).
"""
import argparse
import datetime
import json
import os
import re
import select
import shlex
import subprocess
import sys

HOME = os.path.expanduser("~")
# tmux's global PATH does not include ~/.local/bin (claude, plannotator) or Homebrew's fzf.
os.environ["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
WORKFLOWS = os.environ.get("LAUNCHER_WORKFLOWS", f"{HOME}/dotfiles/workmux/workflows.yaml")
AGENTS_CONFIG = os.environ.get("LAUNCHER_AGENTS_CONFIG", f"{HOME}/.config/workmux/config.yaml")
FIELDS = {"worktree", "session", "agent", "model", "effort", "key_repo_root", "skill", "repo", "files", "workspaces", "case", "case_label",
          "key", "ask", "extra_dirs", "hidden", "window", "reuse", "case_arg"}
ASKABLE = {"epic", "case", "repo"}  # fields a workflow can make the plain Enter flow ask for
SPLIT = "30%"  # shell pane BESIDE the agent (right), like workmux's `split: horizontal` layout; was 20%, widened by half


class Fail(Exception):
    """A loud, human-readable error (shown in the popup, exit 1)."""


# ---------------------------------------------------------------- config

def norm(path):
    return os.path.normpath(os.path.expanduser(path)) if path else ""


def expand_case(case, root=""):
    """`{date}` = today. `{latest:N}` = the newest YYYY-MM-DD folder under root that is less than N days
    old (a shift or duty that is still running: resume it), otherwise today (start a new one)."""
    today = datetime.date.today()
    case = (case or "").replace("{date}", f"{today:%Y-%m-%d}")
    m = re.fullmatch(r"\{latest:(\d+)\}", case)
    if m:
        newest = ""
        for name in list_names(root):
            try:
                age = (today - datetime.date.fromisoformat(name)).days
            except ValueError:
                continue  # not a date folder
            if 0 <= age < int(m[1]) and name > newest:
                newest = name
        case = newest or f"{today:%Y-%m-%d}"
    return case


def clean_case(case):
    """A case is ONE folder name: it must not be able to leave the files root."""
    case = re.sub(r"\s+", "-", (case or "").strip())
    if case and (case.startswith(".") or "/" in case or "\\" in case):
        raise Fail(f"case '{case}' must be a single folder name (no '/', not starting with '.')")
    return case


# The case KEY is the one value typed per launch. It becomes the case folder, the window name
# (<workflow>-<key>) and the skill's argument, so each workflow says what a key looks like (`key:` in
# workflows.yaml) and the launcher turns whatever was typed or pasted into that one canonical spelling.
KEY_KINDS = {  # kind -> (what it is, example)
    "github-issue": ("GitHub issue", "12345, repo#12345 or the issue URL"),
    "github-pr": ("GitHub pull request", "12345, repo#12345 or the PR URL"),
    "incident": ("incident.io reference", "INC-8745 or the incident.io URL"),
    "cve": ("CVE ID", "CVE-2026-47848"),
}
GITHUB_URL = re.compile(r"https?://github\.com/[\w.-]+/([\w.-]+)/(?:issues|pull)/(\d+)")
GITHUB_REPO_NUM = re.compile(r"(?:[\w.-]+/)?([\w.-]+)#(\d+)")  # repo#123 or org/repo#123
GITHUB_FOLDER = re.compile(r"([A-Za-z](?:[\w.-]*[\w.])?)-(\d+)")  # a canonical folder: repo-123. The number is the LAST
# dash-part, so a repo name may hold digits and dashes of its own (pdp-3517-variable-trace-153)
GITHUB_NESTED = re.compile(r"([A-Za-z][\w.-]*)/(\d+)")  # repo/123: the nested key (change), same as its folder
EPIC_PREFIX = re.compile(r"[A-Za-z]+-\d+")  # pdp-3517: the short name of an epic repo (pdp-3517-variable-trace)
INCIDENT_URL = re.compile(r"https?://app\.incident\.io/[\w.-]+/incidents/(\d+)")


def parse_key_kind(kind, where="key", runtime_repo=False):
    """`github-issue:camunda/camunda` -> ("github-issue", "camunda"). Raises Fail on anything unknown.
    runtime_repo: a GitHub kind may leave the repo out, because the workflow learns it at launch (an epic)."""
    base, _, arg = (kind or "").partition(":")
    if base not in KEY_KINDS:
        raise Fail(f"{where}: unknown key '{kind}'. Known: github-issue:<owner>/<repo>, github-pr:<owner>/<repo>, incident, cve")
    repo = arg.rsplit("/", 1)[-1]
    if base.startswith("github") and not (arg.count("/") == 1 and repo and arg.split("/")[0]):
        if arg or not runtime_repo:
            raise Fail(f"{where}: '{kind}' needs the default repo, e.g. {base}:camunda/camunda")
        return base, ""
    return base, repo


def short_repo(repo):
    """The short name an epic repo goes by in folders and windows: pdp-3517-variable-trace -> pdp-3517. A repo
    without a `<letters>-<digits>` prefix (dmn-scala) keeps its whole name."""
    m = re.match(r"[A-Za-z]+-\d+(?=-|$)", repo)
    return m[0] if m else repo


def normalize_key(kind, text, label="", default_repo=None, nested=False):
    """The canonical case key for `text`, or a Fail that says what was expected. "" stays "".
    See key_info for the spelling."""
    return key_info(kind, text, label, default_repo, nested)[0]


def key_info(kind, text, label="", default_repo=None, nested=False):
    """(canonical key, the other repo the issue lives in or "" for the default repo). "" stays ("", "").
    GitHub: the default repo is a bare number (`65755`). Any other repo is `repo-65755`, or, nested (`change`),
    `<short repo>/65755` with the number as its own sub-folder (`pdp-3517/153`). The repo comes back as the
    typed or pasted one: the full name from a URL, maybe only the short name from `pdp-3517/153`. The default
    repo is the one in `kind`, unless the caller knows better (an epic's issue repo; "" = it has none)."""
    text = (text or "").strip()
    if not text:
        return "", ""
    base, kind_repo = parse_key_kind(kind, runtime_repo=True)
    default_repo = kind_repo if default_repo is None else default_repo
    what, example = KEY_KINDS[base]
    if nested:
        example = "12345, repo#12345, repo/12345 or the issue URL"
    bad = Fail(f"'{text}' is not a valid {label or what} (expected {example})")
    if base.startswith("github"):
        if text.upper().startswith(("INC-", "CVE-", "SUPPORT-")):  # would otherwise parse as a repo named INC
            raise bad
        m = GITHUB_URL.fullmatch(text)
        if m:
            repo, num = m[1], m[2]
        elif re.fullmatch(r"#?\d+", text):
            if not default_repo:
                raise Fail(f"'{text}' needs a repo: this epic has no `issues:` repo in its epic.yaml. "
                           f"Type repo#{text.lstrip('#')} or the issue URL")
            repo, num = default_repo, text.lstrip("#")
        elif GITHUB_REPO_NUM.fullmatch(text) or (GITHUB_NESTED if nested else GITHUB_FOLDER).fullmatch(text):
            m = GITHUB_REPO_NUM.fullmatch(text) or (GITHUB_NESTED if nested else GITHUB_FOLDER).fullmatch(text)
            repo, num = m[1], m[2]
        else:
            raise bad
        if repo == default_repo:
            return str(int(num)), ""
        return (f"{short_repo(repo)}/{int(num)}" if nested else f"{repo}-{int(num)}"), repo
    if base == "incident":
        if re.fullmatch(r"(?i)SUPPORT-?\d+", text):
            raise Fail(f"'{text}' is a Jira support ticket. Use its incident.io reference (INC-nnnn); the skill finds the Jira from it")
        m = INCIDENT_URL.fullmatch(text) or re.fullmatch(r"(?i)INC-?(\d+)", text)
        if not m:
            raise bad
        return f"INC-{int(m[1])}", ""
    m = re.fullmatch(r"(?i)CVE-(\d{4})-(\d{4,})", text)  # cve
    if not m:
        raise bad
    return f"CVE-{m[1]}-{m[2]}", ""


def resolve_repo(repo, root):
    """(full repo name, its checkout under `root` or "") for an issue in another repo. `repo` is what was parsed: a
    full name (a URL, `pdp-3517-variable-trace#153`) or only the short epic name (`pdp-3517/153`), which is
    looked up as a checkout: `<root>/pdp-3517` or the one `<root>/pdp-3517-*`. Several matches are an error."""
    full = repo
    if EPIC_PREFIX.fullmatch(repo):  # the short name of an epic repo: find the checkout
        full = ""
        if os.path.isdir(os.path.join(root, repo)):
            full = repo
        else:
            hits = sorted(n for n in list_names(root) if n.startswith(repo + "-"))
            if len(hits) > 1:
                raise Fail(f"'{repo}' matches several checkouts in {short(root)} ({', '.join(hits)}): paste the issue URL")
            full = hits[0] if hits else ""
    checkout = os.path.join(root, full) if full else ""
    return full, checkout if checkout and os.path.isdir(checkout) else ""


# A workflow with `workspaces:` (epic) keeps one folder per epic under that root. Which epic is picked at launch,
# the case is a task issue of it, and the case gets no folder of its own: the task's notes live in the epic
# workspace, in the files the skill keeps there.
LAST_FILE = ".last"         # in the workspaces root: the epic launched last (a folder's mtime says nothing, state sits deeper)
SETTINGS_FILE = "epic.yaml"  # in an epic's folder: `repo:` its docs checkout, `issues:` owner/repo where bare task numbers live


def last_workspace(root):
    """The epic offered first: the one launched last, else the only one there is, else "" (Tab lists them)."""
    try:
        with open(os.path.join(root, LAST_FILE)) as f:
            name = f.read().strip()
    except OSError:
        name = ""
    if name and os.path.isdir(os.path.join(root, name)):
        return name
    names = list_names(root)
    return names[0] if len(names) == 1 else ""


def remember_workspace(root, name):
    try:
        with open(os.path.join(root, LAST_FILE), "w") as f:
            f.write(name + "\n")
    except OSError:
        pass  # only a convenience


def workspace_settings(ws_dir):
    """{repo, issues} from <epic>/epic.yaml; both optional, both empty when the file is missing."""
    path = os.path.join(ws_dir, SETTINGS_FILE)
    try:
        import yaml
    except ImportError:
        raise Fail(f"PyYAML is not importable from {sys.executable}")
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {"repo": "", "issues": ""}
    except (OSError, yaml.YAMLError) as e:
        raise Fail(f"cannot read {path}: {e}")
    if not isinstance(data, dict) or set(data) - {"repo", "issues"}:
        raise Fail(f"{path}: only `repo:` and `issues:` are allowed")
    issues = str(data.get("issues") or "")
    check_issues_repo(issues, path)
    return {"repo": str(data.get("repo") or ""), "issues": issues}


def check_issues_repo(issues, where):
    if issues and not re.fullmatch(r"[\w.-]+/[\w.-]+", issues):
        raise Fail(f"{where}: issues must be owner/repo, not '{issues}'")


def save_workspace_settings(ws_dir, settings):
    lines = ["# Read by the workflow launcher (tmux/popups/launcher.py) whenever this epic is picked."]
    if settings.get("repo"):
        lines.append(f"repo: {settings['repo']}        # the epic's docs checkout: the agent works here")
    if settings.get("issues"):
        lines.append(f"issues: {settings['issues']}        # where a bare task number lives (65 -> this repo's issue 65)")
    with open(os.path.join(ws_dir, SETTINGS_FILE), "w") as f:
        f.write("\n".join(lines) + "\n")


# Reasoning effort is a per-launch flag, and each harness spells it differently. OpenCode and unknown agents
# have none, so an effort for them is ignored (with a note), like the model is for a worktree.
EFFORT_FLAG = {"claude": "--effort", "copilot": "--reasoning-effort"}
EFFORTS = {"claude": ("low", "medium", "high", "xhigh", "max"),
           "copilot": ("none", "minimal", "low", "medium", "high", "xhigh", "max")}


def agent_kind(a):
    cmd = os.path.basename(str(a.get("command", "")))
    if a.get("type") == "claude" or cmd == "claude":
        return "claude"
    if cmd == "copilot":
        return "copilot"
    if a.get("type") == "opencode" or "opencode" in cmd:
        return "opencode"
    return "other"


def skill_roots(a):
    """Where this agent looks for user-level skills."""
    env = a.get("env") or {}
    kind = agent_kind(a)
    if kind == "claude":
        return [os.path.join(env.get("CLAUDE_CONFIG_DIR", f"{HOME}/.claude"), "skills")]
    if kind == "copilot":
        return [os.path.join(env.get("COPILOT_HOME", f"{HOME}/.copilot"), "skills")]
    return [f"{HOME}/.claude/skills"]  # OpenCode reads ~/.claude/skills (checked 2026-11-08)


def check_skill(agents, agent, skill, where):
    """A skill the agent cannot see would silently do nothing, so it is an error, like an agent typo."""
    if not skill:
        return
    roots = skill_roots(agents[agent])
    if not any(os.path.isfile(os.path.join(r, skill, "SKILL.md")) for r in roots):
        raise Fail(f"{where}: skill '{skill}' is not installed for agent '{agent}' (looked in {', '.join(roots)})")


def skill_switched_off(a, skill):
    """The settings file that switches this Claude skill off, or None. `skillOverrides: {skill: off}` in the
    profile's settings.json makes a typed /skill print "disabled via skillOverrides" and start nothing
    (tested 2026-11-10); `user-invocable-only` hides it from the model but still lets /skill run."""
    if not skill or agent_kind(a) != "claude":
        return None
    path = os.path.join((a.get("env") or {}).get("CLAUDE_CONFIG_DIR", f"{HOME}/.claude"), "settings.json")
    try:
        with open(path) as f:
            return path if (json.load(f).get("skillOverrides") or {}).get(skill) == "off" else None
    except (OSError, ValueError, AttributeError):
        return None


def load():
    try:
        import yaml
    except ImportError:
        raise Fail(f"PyYAML is not importable from {sys.executable}")
    docs = {}
    for label, path, key in (("agents", AGENTS_CONFIG, "agents"), ("workflows", WORKFLOWS, "workflows")):
        try:
            with open(path) as f:
                docs[label] = (yaml.safe_load(f) or {}).get(key) or {}
        except (OSError, yaml.YAMLError) as e:
            raise Fail(f"cannot read {label} from {path}: {e}")
        if not docs[label]:
            raise Fail(f"no `{key}:` entries in {path}")
    agents, wfs = docs["agents"], docs["workflows"]
    for name, w in list(wfs.items()):
        w = wfs[name] = dict(w or {})
        where = f"{WORKFLOWS}: workflow '{name}'"
        if "kind" in w:
            raise Fail(f"{where}: `kind` was replaced by `worktree: true|false` (default false)")
        if "dir" in w:
            raise Fail(f"{where}: `dir` was split into `repo` (the git repo) and `files` (where the session's files live)")
        if set(w) - FIELDS:
            raise Fail(f"{where}: unknown field(s) {sorted(set(w) - FIELDS)}; allowed {sorted(FIELDS)}")
        for flag, default in (("worktree", False), ("hidden", False), ("reuse", False), ("case_arg", True)):
            w[flag] = w.get(flag, default)
            if not isinstance(w[flag], bool):
                raise Fail(f"{where}: {flag} must be true or false, not {w[flag]!r}")
        if w["reuse"] and w["worktree"]:
            raise Fail(f"{where}: `reuse` applies to plain windows; `workmux add` already handles an existing worktree")
        if w.get("agent") not in agents:
            raise Fail(f"{where}: agent '{w.get('agent')}' is not in {AGENTS_CONFIG} (have: {', '.join(agents)})")
        for k in ("repo", "files", "workspaces", "skill", "model", "effort", "key_repo_root", "window", "case", "case_label", "key"):
            w[k] = w.get(k) or ""
        w["effort"] = str(w["effort"]).lower()
        if w["effort"] and w["effort"] not in set().union(*EFFORTS.values()):
            raise Fail(f"{where}: effort '{w['effort']}' is not one of {', '.join(EFFORTS['copilot'])}")
        w["case_label"] = w["case_label"] or "case"
        w["ask"] = list(w.get("ask") or [])
        if w["workspaces"] and (w["files"] or w["repo"]):
            raise Fail(f"{where}: `workspaces` (one folder per epic) replaces `files` and `repo`; the epic's epic.yaml holds its repo")
        if "epic" in w["ask"] and not w["workspaces"]:
            raise Fail(f"{where}: `ask: [epic]` needs `workspaces`")
        if w["workspaces"] and "epic" in w["ask"] and "case" in w["ask"] and w["ask"].index("epic") > w["ask"].index("case"):
            raise Fail(f"{where}: ask the epic before the case")
        if w["key"]:
            parse_key_kind(w["key"], where, runtime_repo=bool(w["workspaces"]))
            if w["case"]:
                raise Fail(f"{where}: `key` and a default `case` exclude each other (a key is always typed)")
        if w["key_repo_root"] and not w["key"].startswith("github"):
            raise Fail(f"{where}: `key_repo_root` needs a GitHub `key` (it maps a repo-<number> key to a checkout)")
        w["extra_dirs"] = list(w.get("extra_dirs") or [])
        if set(w["ask"]) - ASKABLE:
            raise Fail(f"{where}: `ask` can hold {sorted(ASKABLE)}, not {sorted(set(w['ask']) - ASKABLE)}")
        if not (w["repo"] or w["files"] or w["workspaces"]):
            raise Fail(f"{where}: needs a `repo`, a `files` folder, or both")
        if w["worktree"] and not (w["repo"] or w["workspaces"]):
            raise Fail(f"{where}: worktree: true needs a `repo` to branch from")
        if (w["case"] or "case" in w["ask"]) and not (w["files"] or w["workspaces"]):
            raise Fail(f"{where}: a case needs a `files` folder to live in")
        check_skill(agents, w["agent"], w["skill"], where)
        w["session"] = w.get("session") or name.split()[0]
    return agents, wfs


def workmux_prompt_is_ignored(repo):
    """`workmux add -p` writes .workmux/PROMPT-<branch>.md into the new worktree. Where that is not
    git-ignored it would show up as a dirty file (and `git add -A` would commit it), so no prompt then."""
    r = subprocess.run(["git", "-C", repo, "check-ignore", "-q", ".workmux/PROMPT-launcher.md"], capture_output=True)
    return r.returncode == 0


def make_plan(name, wfs, agents, o):
    """Workflow defaults + overrides (None = keep default, "" = blank) -> a validated plan."""
    if name not in wfs:
        raise Fail(f"unknown workflow '{name}'. Known: {', '.join(wfs)}")
    wf = wfs[name]
    p = dict(wf, name=name, branch=o.get("branch") or "", issue_repo="")
    notes = []
    for k in ("agent", "skill", "model", "effort", "window", "repo", "files", "worktree"):
        if o.get(k) is not None:
            p[k] = o[k]
    p["effort"] = p["effort"].lower()
    if p["agent"] != wf["agent"] and o.get("model") is None:
        p["model"] = ""  # the default model is the old agent's (Copilot ids are not Claude ids)
    if p["agent"] != wf["agent"] and o.get("effort") is None:
        p["effort"] = ""  # same for the effort: what a level means depends on the agent and its model
    if p["agent"] not in agents:
        raise Fail(f"agent '{p['agent']}' is not defined (have: {', '.join(agents)})")
    check_skill(agents, p["agent"], p["skill"], f"workflow '{name}'")

    settings = {}
    if wf["workspaces"]:  # an epic: --files names its folder directly; else the picked one, else the one launched last
        root = norm(wf["workspaces"])
        if not os.path.isdir(root):
            raise Fail(f"workspaces folder does not exist: {root}")
        if o.get("files"):
            ws = norm(o["files"])
        else:
            folder = clean_case(o.get("workspace") or last_workspace(root))
            if not folder:
                raise Fail(f"'{name}' needs its epic: name its folder with --files {short(root)}/<epic>")
            ws = os.path.join(root, folder)
        if not os.path.isdir(ws):
            raise Fail(f"epic folder does not exist: {ws}")
        settings = o.get("epic_settings") or workspace_settings(ws)
        p["files"] = ws
        if o.get("repo") is None:
            p["repo"] = settings.get("repo", "")
        p["workspace_root"], p["workspace"] = root, os.path.basename(ws)
        # an epic that had no epic.yaml and was just asked for its repos: saved once the launch succeeds
        p["new_settings"] = o.get("epic_settings") if o.get("epic_settings") and not os.path.exists(os.path.join(ws, SETTINGS_FILE)) else None

    p["repo"], p["files"] = norm(p["repo"]), norm(p["files"])
    if p["repo"] and not os.path.isdir(p["repo"]):
        raise Fail(f"repo does not exist: {p['repo']}")
    if p["files"] and not os.path.isdir(p["files"]):
        raise Fail(f"files folder does not exist: {p['files']} (create it, or fix `files:`; only the case folder is created for you)")
    if not (p["repo"] or p["files"]):
        raise Fail("needs a repo, a files folder, or both")
    case, issue_checkout = expand_case(o["case"] if o.get("case") is not None else wf["case"], p["files"]), ""
    if wf["key"]:  # normalise first: a pasted URL holds slashes that a plain folder name must not
        if not case.strip():
            raise Fail(f"'{name}' needs its {wf['case_label']} (--case)")
        issues_repo = (settings.get("issues") or "").rsplit("/", 1)[-1] if wf["workspaces"] else None
        case, other_repo = key_info(wf["key"], case, wf["case_label"], issues_repo, bool(wf["key_repo_root"]))
        if wf["key_repo_root"] and other_repo:  # an issue in another repo (an epic's docs repo): its checkout and full name
            p["issue_repo"], issue_checkout = resolve_repo(other_repo, norm(wf["key_repo_root"]))
            if not issue_checkout:
                notes.append(f"no checkout of {p['issue_repo'] or other_repo + '-*'} under {short(norm(wf['key_repo_root']))}: "
                             "clone it there to give the session its files" + ("" if p["issue_repo"] else " (and paste the issue URL, so the skill gets the full repo name)"))
    case = "/".join(clean_case(part) for part in case.split("/")) if wf["key_repo_root"] else clean_case(case)  # nested keys: `repo/number`
    if case and not p["files"]:
        raise Fail("a case needs a files folder to live in")
    p["case"] = case
    # "" when there is no files folder; an epic's task gets no folder of its own
    p["case_dir"] = os.path.join(p["files"], case) if case and not wf["workspaces"] else p["files"]
    p["cwd"] = p["repo"] or p["case_dir"]  # where the agent starts (the repo; a worktree is made from it)
    p["pane_dir"] = p["case_dir"] or p["cwd"]  # where the right-hand shell opens

    if p["worktree"]:
        if not p["repo"]:
            raise Fail("a worktree needs a repo to branch from")
        if o.get("model"):
            raise Fail("--model cannot be combined with a worktree (workmux add has no model flag)")
        if p["model"]:  # only the workflow's default: yield to the worktree, visibly
            notes.append(f"default model '{p['model']}' ignored: workmux add has no model flag")
            p["model"] = ""
        if o.get("effort"):
            raise Fail("--effort cannot be combined with a worktree (workmux add has no effort flag)")
        if p["effort"]:
            notes.append(f"default effort '{p['effort']}' ignored: workmux add has no effort flag")
            p["effort"] = ""
        if not p["branch"]:
            raise Fail(f"'{name}' creates a worktree and needs a branch (--branch)")
    else:
        if p["branch"]:
            raise Fail("a branch only applies to a worktree (add --worktree)")
        p["window"] = p["window"] or f"{name.split()[-1]}-{case.replace('/', '-') or f'{datetime.datetime.now():%H%M}'}"

    extra_dirs = list(p["extra_dirs"])
    if issue_checkout:
        extra_dirs.append(issue_checkout)
    extra = []
    for d in map(norm, extra_dirs):
        if not os.path.isdir(d):
            raise Fail(f"extra dir does not exist: {d}")
        if d != p["cwd"]:
            extra.append(d)

    kind = agent_kind(agents[p["agent"]])
    if p["effort"] and kind not in EFFORT_FLAG:
        if o.get("effort"):
            raise Fail(f"agent '{p['agent']}' has no effort flag (only {', '.join(EFFORT_FLAG)} do)")
        notes.append(f"default effort '{p['effort']}' ignored: '{p['agent']}' has no effort flag")
        p["effort"] = ""
    elif p["effort"] and p["effort"] not in EFFORTS[kind]:
        raise Fail(f"effort '{p['effort']}' is not valid for {kind} (use {', '.join(EFFORTS[kind])})")
    if o.get("prompt") is not None:
        prompt = o["prompt"]
    else:
        arg = " ".join(filter(None, [case if wf["case_arg"] else "", f"epic={p['workspace']}" if wf["workspaces"] else "", f"repo={p['issue_repo']}" if p["issue_repo"] else ""]))
        prompt = f"/{p['skill']}" + (f" {arg}" if arg else "") if p["skill"] else ""
    off = skill_switched_off(agents[p["agent"]], p["skill"]) if prompt.startswith(f"/{p['skill']}") else None
    if off:
        raise Fail(f"skill '{p['skill']}' is switched off in skillOverrides ({short(off)}), so the session would "
                   f"start and only print an error. Remove that entry, or set it to \"user-invocable-only\" "
                   f"(hidden from the model, still runs when typed).")
    if prompt and kind == "other":
        notes.append(f"agent '{p['agent']}' cannot take a first prompt: type it yourself: {prompt}")
        prompt = ""
    if extra and (p["worktree"] or kind in ("opencode", "other")):
        notes.append("extra dirs not passed: " + ("workmux add builds the agent command" if p["worktree"]
                                                  else f"'{p['agent']}' has no --add-dir"))
        extra = []
    if p["worktree"] and prompt and kind != "claude":
        # workmux runs `<agent> -- "$(cat .workmux/PROMPT-x.md)"`. Claude takes that as its first prompt;
        # the real Copilot CLI answers "unrecognized subcommand" and exits (tested 2026-11-10).
        notes.append(f"workmux cannot hand the prompt to '{p['agent']}' (it passes `-- <prompt>`): type {prompt}")
        prompt = ""
    elif p["worktree"] and prompt and not workmux_prompt_is_ignored(p["repo"]):
        notes.append(f".workmux/ is not git-ignored in {p['repo']}, so the prompt is not injected: type {prompt}")
        prompt = ""
    p["extra"], p["prompt"], p["notes"] = extra, prompt, notes
    return p


# ---------------------------------------------------------------- what to run

def agent_line(agents, agent, model, prompt="", extra=(), effort=""):
    """`env K=V ... <command> [prompt] <args> [--model M] [--effort E]`, the same shape workmux types into a pane.
    Claude takes the prompt as its first positional argument and must get it BEFORE --add-dir, which
    swallows every following value. Copilot takes -i, OpenCode --prompt."""
    a = agents[agent]
    kind = agent_kind(a)
    args, skip = [], False
    for x in map(str, a.get("args") or []):
        if skip:
            skip = False
        elif model and x == "--model":
            skip = True  # drop the agent's own `--model X` pair; the override replaces it
        elif not (model and x.startswith("--model=")):
            args.append(x)
    tail = (["--model", model] if model else []) + ([EFFORT_FLAG[kind], effort] if effort and kind in EFFORT_FLAG else [])
    adds = [x for d in extra for x in ("--add-dir", d)]
    if kind == "claude":
        argv = [str(a["command"]), *([prompt] if prompt else []), *args, *tail, *adds]
    elif kind == "copilot":
        argv = [str(a["command"]), *args, *tail, *(["-i", prompt] if prompt else []), *adds]
    else:
        argv = [str(a["command"]), *args, *tail, *(["--prompt", prompt] if prompt else [])]
    env = [f"{k}={v}" for k, v in (a.get("env") or {}).items()]
    return shlex.join((["env", *env] if env else []) + argv)


def worktree_argv(p, switch):
    argv = ["workmux", "add", p["branch"], "-a", p["agent"], "--parent-session", p["session"]]
    argv += ["--target-name", p["window"]] if p["window"] else []
    argv += ["-p", p["prompt"]] if p["prompt"] else []
    return argv + ([] if switch else ["-b"])


def short(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def summary(p, agents, switch=True):
    """What launch() is about to do, as text (shown before the confirmation and by --dry-run)."""
    wt = p["worktree"]
    head = f"[{p['name']}] session {p['session']}" + ("" if wt else f" | window {p['window']}")
    out = [head] + [f"  note: {n}" for n in p["notes"]]
    out.append(f"  agent  {p['agent']}" + (f" ({p['model']})" if p["model"] else "") + (f", effort {p['effort']}" if p["effort"] else ""))
    out.append("  skill  " + (p["prompt"] if p["prompt"] else (f"{p['skill']} (not started automatically)" if p["skill"] else "(none)")))
    out.append(f"  starts {short(p['cwd'])}" + ("  -> new worktree off it" if wt else ""))
    if p.get("workspace"):
        out.append(f"  epic   {p['workspace']}" + ("  (epic.yaml is saved with the launch)" if p.get("new_settings") else ""))
    if p["reuse"] and not wt:
        out.append("  reuse  " + (("the window is open: nothing started" + (", you would be switched to it" if switch else ""))
                                   if open_window(p["session"], p["window"]) else "no such window yet: a new one is started"))
    if p["case_dir"]:
        out.append(f"  files  {short(p['case_dir'])}  (right-hand pane{', created if missing' if not os.path.isdir(p['case_dir']) else ''})")
    if p["extra"]:
        out.append("  also   " + ", ".join(map(short, p["extra"])))
    out.append("  run    " + (f"cd {short(p['cwd'])} && {shlex.join(worktree_argv(p, switch))}" if wt
                              else agent_line(agents, p["agent"], p["model"], p["prompt"], p["extra"], p["effort"])))
    return "\n".join(out)


# ---------------------------------------------------------------- tmux

def tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"tmux {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout.strip()


def ensure_session(session, cwd):
    if subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True).returncode:
        tmux("new-session", "-d", "-s", session, "-c", cwd)


def open_window(session, name):
    """The id of the window called `name` in `session`, or None (also when there is no such session)."""
    r = subprocess.run(["tmux", "list-windows", "-t", f"={session}:", "-F", "#{window_id} #{window_name}"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines() if not r.returncode else []:
        wid, _, wname = line.partition(" ")
        if wname == name:
            return wid
    return None


def window_ids(session):
    return set(tmux("list-windows", "-t", f"={session}:", "-F", "#{window_id}").split())


def right_hand_pane(window):
    """The shell pane beside the agent: the rightmost pane that is not the workmux sidebar. The sidebar
    sits at the far left, so 'rightmost' never picks it. None when the layout is not recognisable."""
    panes = []
    for line in tmux("list-panes", "-t", window, "-F", "#{pane_left} #{pane_index} #{pane_id} #{pane_current_command}").splitlines():
        left, index, pid, cmd = line.split(None, 3)
        if cmd != "workmux":
            panes.append((int(left), int(index), pid))
    if len(panes) < 2 or max(panes)[0] == min(panes)[0]:
        return None
    return max(panes)[2]


def launch(p, agents, dry, switch, show=True):
    start_window(p, agents, dry, switch, show)
    if dry or not p.get("workspace_root"):
        return
    if p.get("new_settings"):
        save_workspace_settings(p["files"], p["new_settings"])
    remember_workspace(p["workspace_root"], p["workspace"])


def start_window(p, agents, dry, switch, show=True):
    if show:
        print(summary(p, agents, switch))
    if dry:
        return
    s, cwd = p["session"], p["cwd"]
    if p["case_dir"]:
        os.makedirs(p["case_dir"], exist_ok=True)  # only the case folder; the files root must already exist
    ensure_session(s, cwd)
    if p["reuse"] and not p["worktree"]:
        have = open_window(s, p["window"])
        if have:  # today's window is already open: go back to it instead of starting a second agent
            if switch:
                tmux("switch-client", "-t", have)
            print(f"  already open: {s}:{p['window']}" + (", switched to it" if switch else ""))
            return
    if p["worktree"]:
        argv, before = worktree_argv(p, switch), window_ids(s)
        # Inherit the terminal: workmux may ask something (e.g. trust a project config) and its own
        # errors should be visible in the popup. Captured output would hang on a prompt, unseen.
        r = subprocess.run(argv, cwd=cwd)
        if r.returncode:
            raise Fail(f"{shlex.join(argv)} exited {r.returncode}")
        if p["case_dir"]:  # workmux opens its shell pane in the worktree; move it to the files folder
            new = window_ids(s) - before
            pane = right_hand_pane(next(iter(new))) if len(new) == 1 else None
            if pane:
                tmux("send-keys", "-t", pane, "-l", f"cd {shlex.quote(p['case_dir'])} && clear")
                tmux("send-keys", "-t", pane, "Enter")
            else:
                print(f"  note: could not find the right-hand pane; it stays in the worktree (files: {p['case_dir']})")
        return
    line = agent_line(agents, p["agent"], p["model"], p["prompt"], p["extra"], p["effort"])
    win, pane = tmux("new-window", "-P", "-F", "#{window_id} #{pane_id}",
                     "-t", f"={s}:", "-n", p["window"], "-c", cwd).split()
    tmux("send-keys", "-t", pane, "-l", line)
    tmux("send-keys", "-t", pane, "Enter")
    tmux("split-window", "-h", "-d", "-l", SPLIT, "-t", pane, "-c", p["pane_dir"])  # -h = side by side
    if switch:
        tmux("switch-client", "-t", win)


# ---------------------------------------------------------------- the popup

def cols(*c):
    """workflow, agent, model, effort, skill, wt, repo, files: the last column is not padded."""
    widths = (14, 15, 17, 6, 30, 3, 26)
    return " ".join(f"{x:<{w}}" for x, w in zip(c, widths)) + " " + c[len(widths)]


def pick(wfs):
    """fzf over the workflows. Returns (workflow, edit?) or None when cancelled."""
    rows = []
    for n, w in wfs.items():
        if w["hidden"]:
            continue
        model = "-" if w["worktree"] else (w["model"] or "(default)")
        effort = "-" if w["worktree"] else (w["effort"] or "(def)")
        rows.append(cols(n, w["agent"], model, effort, w["skill"] or "-", "yes" if w["worktree"] else "no",
                         short(norm(w["repo"])) or "-", short(norm(w["files"] or w["workspaces"])) or "-"))
    # Columns are space-padded; two+ spaces end the first field, so --nth=1 matches the workflow
    # name only (typing "bet" must not fuzzy-match a path or agent column). --tiebreak=index: on a
    # tie YAML order wins, so keep the workflow you launch most often above its siblings.
    r = subprocess.run(
        ["fzf", "--delimiter=\\s{2,}", "--nth=1", "--tiebreak=index", "--expect=tab", "--layout=reverse",
         "--info=hidden", "--prompt=workflow> ",
         "--header=enter: launch with defaults    tab: edit fields    esc: cancel\n"
                   + cols("workflow", "agent", "model", "effort", "skill", "wt", "repo", "files")],
        input="\n".join(rows), text=True, stdout=subprocess.PIPE)
    if r.returncode in (1, 130):
        return None
    if r.returncode:
        raise Fail(f"fzf exited {r.returncode}")
    key, _, chosen = r.stdout.partition("\n")
    return re.split(r"\s{2,}", chosen.strip())[0], key == "tab"


class Cancelled(Exception):
    """The user backed out (Esc, or Ctrl-D on an empty field). Nothing has been created yet: the
    session and window only come into existence in launch(), after the last question."""


def read_token(fd):
    """One keypress: a character, or ENTER / TAB / ESC / BS / KILL_LINE / KILL_WORD / EOF / IGNORE.
    A lone Esc is told apart from arrow and function keys (Esc [ ...) by waiting 50 ms for more."""
    b = os.read(fd, 1)
    if not b:
        return "EOF"
    c = b[0]
    if c == 0x1B:
        if select.select([fd], [], [], 0.05)[0]:
            os.read(fd, 16)  # the rest of an arrow/function-key sequence
            return "IGNORE"
        return "ESC"
    if c in (0x0D, 0x0A):
        return "ENTER"
    if c == 0x09:
        return "TAB"
    if c in (0x7F, 0x08):
        return "BS"
    if c == 0x15:
        return "KILL_LINE"
    if c == 0x17:
        return "KILL_WORD"
    if c == 0x04:
        return "EOF"
    if c < 0x20:
        return "IGNORE"
    extra = 0 if c < 0xC0 else 1 if c < 0xE0 else 2 if c < 0xF0 else 3  # UTF-8 continuation bytes
    return (b + (os.read(fd, extra) if extra else b"")).decode(errors="ignore")


KEYS = {"ENTER", "TAB", "ESC", "BS", "KILL_LINE", "KILL_WORD", "EOF", "IGNORE"}


def key_mode():
    """cbreak: no echo and no line buffering, but Ctrl-C still raises KeyboardInterrupt."""
    import contextlib
    import termios
    import tty

    @contextlib.contextmanager
    def mode():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            yield fd
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return mode()


# --- path completion: Tab lists what is in the folder typed so far, fzf picks one

def list_names(root, hidden=False, newest=False, need=None):
    """Sub-folders of root (symlinks followed). need: a file the folder must contain (skills: SKILL.md)."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    found = []
    for e in entries:
        try:
            if not e.is_dir() or (e.name.startswith(".") and not hidden):
                continue
            if need and not os.path.isfile(os.path.join(e.path, need)):
                continue
            found.append((-e.stat().st_mtime if newest else 0, e.name))
        except OSError:
            continue
    return [n for _, n in sorted(found)]


def path_completer(text):
    """Completes full paths: ~/dev/ca<Tab> lists the folders in ~/dev that start with 'ca'."""
    i = text.rfind("/")
    head, prefix = (text[:i + 1], text[i + 1:]) if i >= 0 else ("~/", text)
    return head, prefix, list_names(os.path.expanduser(head), hidden=prefix.startswith(".")), "/"


def names_completer(root, newest=False, need=None):
    """Completes one flat name from the folders under root (existing cases, installed skills)."""
    def complete(text):
        return "", text, list_names(root, hidden=text.startswith("."), newest=newest, need=need) if root else [], ""
    return complete


def case_completer(root):
    """Existing cases of a nested workflow, newest first: `65755`, and `pdp-3517/153` for an issue in another repo."""
    def complete(text):
        names = []
        for top in list_names(root, newest=True):
            if top[0].isalpha():  # a repo folder: its cases are the numbers inside
                names += [f"{top}/{n}" for n in list_names(os.path.join(root, top), newest=True)]
            else:
                names.append(top)
        return "", text, names, ""
    return complete


def skill_completer(agents, agent):
    names = sorted({n for r in skill_roots(agents[agent]) for n in list_names(r, need="SKILL.md")})
    return lambda text: ("", text, names, "")


def pick_one(items, label):
    r = subprocess.run(["fzf", "--height=50%", "--layout=reverse", "--info=hidden", f"--prompt={label}> ",
                        "--header=enter: pick    esc: back"],
                       input="\n".join(items), text=True, stdout=subprocess.PIPE)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def browse(label, text, complete):
    """Tab on a field. One match: complete it. Several, or none (or only what is already typed): fzf over
    them. Returns the new field text, or None to leave it alone."""
    head, prefix, names, tail = complete(text)
    if not names:
        return None
    hits = [n for n in names if n.lower().startswith(prefix.lower())]
    if len(hits) == 1 and hits[0] != prefix:
        return head + hits[0] + tail
    choice = pick_one(hits if len(hits) > 1 else names, label)
    return None if choice is None else head + choice + tail


def kill_word(text, path):
    if path:
        return text[:text.rstrip("/").rfind("/") + 1] if "/" in text.rstrip("/") else ""
    return text.rstrip().rsplit(" ", 1)[0] + " " if " " in text.rstrip() else ""


def ask(label, default="", complete=None):
    """`label [default]: ` -- Enter keeps the default, `-` clears it, anything else replaces it.
    With `complete` the field starts out holding the default so you can edit it (Backspace, Ctrl-U to
    clear) and press Tab to pick from a list; Enter then takes whatever the field holds, empty = none.
    Esc cancels (raises Cancelled). Own line editor: Python on macOS uses libedit, whose input()
    can neither prefill nor treat Esc as cancel."""
    edit_in_place = complete is not None
    hint = "  (Tab = pick from a list)" if edit_in_place else ""
    shown = f" [{default}]" if default and not edit_in_place else ""
    prompt = f"  {label}{hint}{shown}: "
    if not sys.stdin.isatty():  # piped input (never the popup): plain line
        sys.stdout.write(prompt)
        sys.stdout.flush()
        answer = input().strip()
        return default if answer == "" else ("" if answer == "-" else answer)
    text = default if edit_in_place else ""

    def draw():
        sys.stdout.write("\r\033[K" + prompt + text)
        sys.stdout.flush()
    draw()
    with key_mode() as fd:
        while True:
            t = read_token(fd)
            if t == "ENTER":
                break
            if t == "ESC" or (t == "EOF" and not text):
                sys.stdout.write("\n")
                raise Cancelled
            if t == "BS":
                text = text[:-1]
            elif t == "KILL_LINE":
                text = ""
            elif t == "KILL_WORD":
                text = kill_word(text, edit_in_place)
            elif t == "TAB":
                if complete:
                    picked = browse(label, text, complete)
                    text = text if picked is None else picked
            elif t not in KEYS:
                text += t
            draw()
    sys.stdout.write("\n")
    answer = text.strip()
    if edit_in_place:
        return "" if answer == "-" else answer
    return default if answer == "" else ("" if answer == "-" else answer)


def confirm_launch():
    """After the edit flow: Enter launches, any other key cancels."""
    sys.stdout.write("\n  Enter = launch     Esc = cancel ")
    sys.stdout.flush()
    with key_mode() as fd:
        t = read_token(fd)
    sys.stdout.write("\n")
    return t == "ENTER"


def press_any_key():
    sys.stdout.write("\n  press any key to close ")
    sys.stdout.flush()
    try:
        with key_mode() as fd:
            read_token(fd)
    except Exception:
        pass


def files_root(wf, o):
    return norm(o["files"] if o.get("files") is not None else wf["files"])


def ask_workspace(wf, o):
    """Which epic: the one launched last is offered; Tab lists the others."""
    root = norm(wf["workspaces"])
    default = last_workspace(root)
    while True:
        name = ask("epic", default, names_completer(root, newest=True))
        try:
            name = clean_case(name)
        except Fail as e:
            print(f"  {e}")
            default = name
            continue
        if name and os.path.isdir(os.path.join(root, name)):
            return name
        print(f"  no epic folder '{name}' in {short(root)}" if name else "  the epic is required")
        default = name


def settings_for(wf, o):
    """The picked epic's settings (empty for a workflow without epics)."""
    if not wf["workspaces"]:
        return {}
    if o.get("epic_settings"):
        return o["epic_settings"]
    root = norm(wf["workspaces"])
    name = o.get("workspace") or last_workspace(root)
    return workspace_settings(os.path.join(root, name)) if name else {}


def first_time_settings(wf, o):
    """An epic with no epic.yaml yet: ask for its two repos once. They are saved when the launch succeeds."""
    ws = os.path.join(norm(wf["workspaces"]), o["workspace"])
    if os.path.exists(os.path.join(ws, SETTINGS_FILE)):
        return
    print(f"\n  {o['workspace']} has no {SETTINGS_FILE} yet; asked once, saved with the launch")
    repo = ask("docs repo checkout (empty = none)", "", path_completer)
    while True:
        issues = ask("repo that holds its task issues, owner/repo (empty = none)", "")
        try:
            check_issues_repo(issues, "launch")
            break
        except Fail as e:
            print(f"  {e}")
    o["epic_settings"] = {"repo": repo, "issues": issues}


def ask_case(wf, root, issues=""):
    """The case field. A workflow with a `key:` re-asks until the answer is a valid key (shown in its
    canonical spelling), so a typo costs one line, not the whole launch. An epic's task field offers no Tab list:
    the folders under the epic are not tasks."""
    complete = None if wf["workspaces"] else (case_completer(root) if wf["key_repo_root"] else names_completer(root, newest=True))
    if not wf["key"]:
        return ask(wf["case_label"] + " (empty = none)", expand_case(wf["case"], root), complete)
    issues_repo = (issues or "").rsplit("/", 1)[-1] if wf["workspaces"] else None
    default = ""
    while True:
        answer = ask(wf["case_label"], default, complete)
        try:
            key = normalize_key(wf["key"], answer, wf["case_label"], issues_repo, bool(wf["key_repo_root"]))
            if key:
                if key != answer:
                    print(f"  -> {key}")
                return answer.strip()  # as typed: a pasted URL still names the full repo when the launch re-reads it
            print(f"  {wf['case_label']} is required")
        except Fail as e:
            print(f"  {e}")
            default = answer


def quick(wf, o):
    """The plain Enter flow: only the fields the workflow marks `ask:` (its case, maybe its repo)."""
    for key in wf["ask"]:
        if key == "epic":
            o["workspace"] = ask_workspace(wf, o)
            first_time_settings(wf, o)
        elif key == "case":
            o["case"] = ask_case(wf, files_root(wf, o), settings_for(wf, o).get("issues", ""))
        elif key == "repo":
            o["repo"] = ask("repo (empty = none)", wf["repo"], path_completer)


def edit(wf, agents, o):
    ws = bool(wf["workspaces"])
    if ws:
        o["workspace"] = ask_workspace(wf, o)
        first_time_settings(wf, o)
    settings = settings_for(wf, o)
    while True:
        o["agent"] = ask(f"agent ({', '.join(agents)})", wf["agent"])
        if o["agent"] in agents:
            break
        print(f"  unknown agent '{o['agent']}'")
    while True:
        o["skill"] = ask("skill (empty = none)", wf["skill"], skill_completer(agents, o["agent"]))
        try:
            check_skill(agents, o["agent"], o["skill"], "launch")
            break
        except Fail as e:
            print(f"  {e}")
    o["repo"] = ask("repo (empty = none)", settings.get("repo", "") if ws else wf["repo"], path_completer)
    o["worktree"] = False
    if o["repo"]:
        while True:
            answer = ask("new worktree off the repo? (y/n)", "y" if wf["worktree"] else "n").lower()
            if answer in ("y", "yes", "n", "no"):
                break
            print("  answer y or n")
        o["worktree"] = answer.startswith("y")
    if not o["worktree"]:
        same = o["agent"] == wf["agent"]  # the workflow's model belongs to its own agent
        o["model"] = ask("model (empty = harness default)" if same else "model (agent changed: harness default)",
                         wf["model"] if same else "")
        kind = agent_kind(agents[o["agent"]])
        if kind in EFFORT_FLAG:
            while True:
                o["effort"] = ask(f"effort ({', '.join(EFFORTS[kind])}; empty = harness default)"
                                  if same else "effort (agent changed: harness default)", wf["effort"] if same else "").lower()
                if not o["effort"] or o["effort"] in EFFORTS[kind]:
                    break
                print(f"  {kind} has: {', '.join(EFFORTS[kind])}")
        o["window"] = ask("window name (empty = auto)", wf["window"])
    else:
        o["window"] = ask("window name (empty = branch name)", wf["window"])
    if not ws:
        o["files"] = ask("files folder (empty = none)", wf["files"], path_completer)
    o["case"] = ask_case(wf, "" if ws else norm(o["files"]), settings.get("issues", ""))


def run(args):
    agents, wfs = load()
    name = " ".join(args.workflow)
    o = {k: getattr(args, k) for k in ("agent", "skill", "model", "effort", "repo", "files", "case", "branch", "prompt", "worktree")}
    o["window"] = args.name
    if not name:
        chosen = pick(wfs)
        if chosen is None:
            return
        name, want_edit = chosen
        wf = wfs[name]
        if want_edit:
            print(f"\n  {name}:  Enter keeps the value   '-' clears it   Esc cancels")
            edit(wf, agents, o)
        wants_worktree = wf["worktree"] if o["worktree"] is None else o["worktree"]
        needs_branch = wants_worktree and not o["branch"]
        if not want_edit and (wf["ask"] or needs_branch):
            print(f"\n  {name}:  Enter keeps the value   Esc cancels")
            quick(wf, o)
            wants_worktree = wf["worktree"] if o["worktree"] is None else o["worktree"]
        if wants_worktree and not o["branch"]:
            o["branch"] = ask("branch (required)")
        if want_edit:  # edited by hand: show the result and let the user back out before anything exists
            plan = make_plan(name, wfs, agents, o)
            print("\n" + summary(plan, agents, not args.no_switch))
            if not confirm_launch():
                raise Cancelled
            return launch(plan, agents, args.dry_run, not args.no_switch, show=False)
    launch(make_plan(name, wfs, agents, o), agents, args.dry_run, not args.no_switch)


def main():
    ap = argparse.ArgumentParser(description="Start an agent window for a workflow (see module docstring).")
    ap.add_argument("workflow", nargs="*", help='e.g. review; omit to open the picker')
    ap.add_argument("--agent"), ap.add_argument("--model"), ap.add_argument("--name", help="window name")
    ap.add_argument("--effort", help="reasoning effort (low|medium|high|xhigh|max; Copilot also none|minimal); "
                                     "empty = the harness default")
    ap.add_argument("--skill", help="skill to start (empty = none)")
    ap.add_argument("--repo", help="git repo the agent works in; a worktree branches off it")
    ap.add_argument("--files", help="folder that holds the cases; the right-hand pane opens in <files>/<case>. "
                                    "For an epic workflow: the epic's folder (default: the epic launched last)")
    ap.add_argument("--case", help="case folder name under --files (PR number, issue-slug, date, ...)")
    ap.add_argument("--branch", help="branch for a new worktree")
    ap.add_argument("--prompt", help="first prompt, instead of '/<skill> <case>'")
    ap.add_argument("--worktree", action=argparse.BooleanOptionalAction, default=None,
                    help="create a worktree (or not), overriding the workflow's default")
    ap.add_argument("--dry-run", action="store_true", help="print what would run, change nothing")
    ap.add_argument("--no-switch", action="store_true", help="leave the client where it is")
    args = ap.parse_args()
    try:
        run(args)
    except Fail as e:
        print(f"\033[31mlauncher: {e}\033[0m", file=sys.stderr)
        if not args.workflow:  # in the popup: keep the message on screen
            press_any_key()
        sys.exit(1)
    except (Cancelled, KeyboardInterrupt, EOFError):
        print("\n  cancelled, nothing created")
        sys.exit(0)


if __name__ == "__main__":
    main()
