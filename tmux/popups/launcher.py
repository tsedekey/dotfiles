#!/usr/bin/env python3
"""tmux popup: start an agent window for a workflow.   Bound in tmux.conf as: prefix + N

  popup    Pick a workflow in fzf.  Enter = launch with its defaults (a workflow may first ask for its case,
           and for a repo).  Tab = edit every field first.  In the path fields (repo, files, case, skill)
           Tab lists what is in the folder typed so far and you pick from it instead of looking it up.
  CLI      launcher.py review [--agent A] [--skill S] [--model M] [--name N] [--repo R] [--files F]
           [--case C] [--branch B] [--prompt P] [--[no-]worktree] [--dry-run] [--no-switch].
           Naming a workflow skips the UI and NEVER prompts (scripts, tests and other agents use this;
           hidden workflows such as `dagrunner` can only be started this way).

A workflow is five things (defaults in dotfiles/workmux/workflows.yaml, explained there):
  agent + model   which harness runs, from the `agents:` block of ~/.config/workmux/config.yaml
  skill           started as the agent's first prompt: `/<skill> <case>`
  repo            optional git repo the agent works in; a worktree, when asked for, branches off it
  files + case    where the session's artifacts live: <files>/<case>/. The right-hand shell pane opens
                  there. The launcher creates the case folder; the files root must already exist
  extra_dirs      more folders the agent may read (passed as --add-dir)
The agent starts in the repo (or its new worktree) when there is one, otherwise in the case folder.
The session is the workflow's first word and is created when missing.  Every window is monitored
because the agents' own workmux hooks register the pane; nothing here talks to workmux except
`worktree: true`, which runs `workmux add`.

Env: LAUNCHER_WORKFLOWS / LAUNCHER_AGENTS_CONFIG override the two config paths (tests use them).
"""
import argparse
import datetime
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
FIELDS = {"worktree", "session", "agent", "model", "skill", "repo", "files", "case", "case_label",
          "ask", "extra_dirs", "hidden", "window"}
ASKABLE = {"case", "repo"}  # fields a workflow can make the plain Enter flow ask for
SPLIT = "20%"  # shell pane BESIDE the agent (right), like workmux's `split: horizontal` layout


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
        for flag in ("worktree", "hidden"):
            w[flag] = w.get(flag, False)
            if not isinstance(w[flag], bool):
                raise Fail(f"{where}: {flag} must be true or false, not {w[flag]!r}")
        if w.get("agent") not in agents:
            raise Fail(f"{where}: agent '{w.get('agent')}' is not in {AGENTS_CONFIG} (have: {', '.join(agents)})")
        for k in ("repo", "files", "skill", "model", "window", "case", "case_label"):
            w[k] = w.get(k) or ""
        w["case_label"] = w["case_label"] or "case"
        w["ask"] = list(w.get("ask") or [])
        w["extra_dirs"] = list(w.get("extra_dirs") or [])
        if set(w["ask"]) - ASKABLE:
            raise Fail(f"{where}: `ask` can hold {sorted(ASKABLE)}, not {sorted(set(w['ask']) - ASKABLE)}")
        if not (w["repo"] or w["files"]):
            raise Fail(f"{where}: needs a `repo`, a `files` folder, or both")
        if w["worktree"] and not w["repo"]:
            raise Fail(f"{where}: worktree: true needs a `repo` to branch from")
        if (w["case"] or "case" in w["ask"]) and not w["files"]:
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
    p = dict(wf, name=name, branch=o.get("branch") or "")
    notes = []
    for k in ("agent", "skill", "model", "window", "repo", "files", "worktree"):
        if o.get(k) is not None:
            p[k] = o[k]
    if p["agent"] != wf["agent"] and o.get("model") is None:
        p["model"] = ""  # the default model is the old agent's (Copilot ids are not Claude ids)
    if p["agent"] not in agents:
        raise Fail(f"agent '{p['agent']}' is not defined (have: {', '.join(agents)})")
    check_skill(agents, p["agent"], p["skill"], f"workflow '{name}'")

    p["repo"], p["files"] = norm(p["repo"]), norm(p["files"])
    if p["repo"] and not os.path.isdir(p["repo"]):
        raise Fail(f"repo does not exist: {p['repo']}")
    if p["files"] and not os.path.isdir(p["files"]):
        raise Fail(f"files folder does not exist: {p['files']} (create it, or fix `files:`; only the case folder is created for you)")
    if not (p["repo"] or p["files"]):
        raise Fail("needs a repo, a files folder, or both")
    case = clean_case(expand_case(o["case"] if o.get("case") is not None else wf["case"], p["files"]))
    if case and not p["files"]:
        raise Fail("a case needs a files folder to live in")
    p["case"] = case
    p["case_dir"] = os.path.join(p["files"], case) if case else p["files"]  # "" when there is no files folder
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
        if not p["branch"]:
            raise Fail(f"'{name}' creates a worktree and needs a branch (--branch)")
    else:
        if p["branch"]:
            raise Fail("a branch only applies to a worktree (add --worktree)")
        p["window"] = p["window"] or f"{name.split()[-1]}-{case or f'{datetime.datetime.now():%H%M}'}"

    extra = []
    for d in map(norm, p["extra_dirs"]):
        if not os.path.isdir(d):
            raise Fail(f"extra dir does not exist: {d}")
        if d != p["cwd"]:
            extra.append(d)

    kind = agent_kind(agents[p["agent"]])
    if o.get("prompt") is not None:
        prompt = o["prompt"]
    else:
        prompt = f"/{p['skill']}" + (f" {case}" if case else "") if p["skill"] else ""
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

def agent_line(agents, agent, model, prompt="", extra=()):
    """`env K=V ... <command> [prompt] <args> [--model M]`, the same shape workmux types into a pane.
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
    tail = (["--model", model] if model else [])
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
    out.append(f"  agent  {p['agent']}" + (f" ({p['model']})" if p["model"] else ""))
    out.append("  skill  " + (p["prompt"] if p["prompt"] else (f"{p['skill']} (not started automatically)" if p["skill"] else "(none)")))
    out.append(f"  starts {short(p['cwd'])}" + ("  -> new worktree off it" if wt else ""))
    if p["case_dir"]:
        out.append(f"  files  {short(p['case_dir'])}  (right-hand pane{', created if missing' if not os.path.isdir(p['case_dir']) else ''})")
    if p["extra"]:
        out.append("  also   " + ", ".join(map(short, p["extra"])))
    out.append("  run    " + (f"cd {short(p['cwd'])} && {shlex.join(worktree_argv(p, switch))}" if wt
                              else agent_line(agents, p["agent"], p["model"], p["prompt"], p["extra"])))
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
    if show:
        print(summary(p, agents, switch))
    if dry:
        return
    s, cwd = p["session"], p["cwd"]
    if p["case_dir"]:
        os.makedirs(p["case_dir"], exist_ok=True)  # only the case folder; the files root must already exist
    ensure_session(s, cwd)
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
    line = agent_line(agents, p["agent"], p["model"], p["prompt"], p["extra"])
    win, pane = tmux("new-window", "-P", "-F", "#{window_id} #{pane_id}",
                     "-t", f"={s}:", "-n", p["window"], "-c", cwd).split()
    tmux("send-keys", "-t", pane, "-l", line)
    tmux("send-keys", "-t", pane, "Enter")
    tmux("split-window", "-h", "-d", "-l", SPLIT, "-t", pane, "-c", p["pane_dir"])  # -h = side by side
    if switch:
        tmux("switch-client", "-t", win)


# ---------------------------------------------------------------- the popup

def cols(*c):
    """workflow, agent, model, skill, wt, repo, files: the last column is not padded."""
    widths = (14, 15, 17, 30, 3, 26)
    return " ".join(f"{x:<{w}}" for x, w in zip(c, widths)) + " " + c[len(widths)]


def pick(wfs):
    """fzf over the workflows. Returns (workflow, edit?) or None when cancelled."""
    rows = []
    for n, w in wfs.items():
        if w["hidden"]:
            continue
        model = "-" if w["worktree"] else (w["model"] or "(default)")
        rows.append(cols(n, w["agent"], model, w["skill"] or "-", "yes" if w["worktree"] else "no",
                         short(norm(w["repo"])) or "-", short(norm(w["files"])) or "-"))
    # Columns are space-padded; two+ spaces end the first field, so --nth=1 matches the workflow
    # name only (typing "bet" must not fuzzy-match a path or agent column). --tiebreak=index: on a
    # tie YAML order wins, so keep the workflow you launch most often above its siblings.
    r = subprocess.run(
        ["fzf", "--delimiter=\\s{2,}", "--nth=1", "--tiebreak=index", "--expect=tab", "--layout=reverse",
         "--info=hidden", "--prompt=workflow> ",
         "--header=enter: launch with defaults    tab: edit fields    esc: cancel\n"
                   + cols("workflow", "agent", "model", "skill", "wt", "repo", "files")],
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


def quick(wf, o):
    """The plain Enter flow: only the fields the workflow marks `ask:` (its case, maybe its repo)."""
    for key in wf["ask"]:
        if key == "case":
            o["case"] = ask(wf["case_label"] + " (empty = none)", expand_case(wf["case"], files_root(wf, o)),
                            names_completer(files_root(wf, o), newest=True))
        elif key == "repo":
            o["repo"] = ask("repo (empty = none)", wf["repo"], path_completer)


def edit(wf, agents, o):
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
    o["repo"] = ask("repo (empty = none)", wf["repo"], path_completer)
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
        o["window"] = ask("window name (empty = auto)", wf["window"])
    else:
        o["window"] = ask("window name (empty = branch name)", wf["window"])
    o["files"] = ask("files folder (empty = none)", wf["files"], path_completer)
    o["case"] = ask(wf["case_label"] + " (empty = none)", expand_case(wf["case"], norm(o["files"])),
                    names_completer(norm(o["files"]), newest=True))


def run(args):
    agents, wfs = load()
    name = " ".join(args.workflow)
    o = {k: getattr(args, k) for k in ("agent", "skill", "model", "repo", "files", "case", "branch", "prompt", "worktree")}
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
    ap.add_argument("--skill", help="skill to start (empty = none)")
    ap.add_argument("--repo", help="git repo the agent works in; a worktree branches off it")
    ap.add_argument("--files", help="folder that holds the cases; the right-hand pane opens in <files>/<case>")
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
