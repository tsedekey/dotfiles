#!/usr/bin/env python3
"""tmux popup: start an agent window for a workflow.   Bound in tmux.conf as: prefix + N

  popup    Pick a workflow in fzf.  Enter = launch with its defaults.  Tab = edit agent, model,
           window name and directory first (each prefilled; Enter keeps it).
  CLI      launcher.py "change plan" [--agent A] [--model M] [--name N] [--dir D] [--branch B]
           [--dry-run] [--no-switch].  Naming a workflow skips the UI (scripts and tests).

Defaults live in dotfiles/workmux/workflows.yaml.  Agent command, args and env are read from the
`agents:` block of ~/.config/workmux/config.yaml, so the profiles stay defined in one place.
The session is the workflow's first word and is created when missing.  Every window is monitored
because the agents' own workmux hooks register the pane; nothing here talks to workmux except
`kind: worktree`, which runs `workmux add`.

Env: LAUNCHER_WORKFLOWS / LAUNCHER_AGENTS_CONFIG override the two config paths (tests use them).
"""
import argparse
import datetime
import os
import re
import shlex
import subprocess
import sys

HOME = os.path.expanduser("~")
# tmux's global PATH does not include ~/.local/bin (claude, plannotator) or Homebrew's fzf.
os.environ["PATH"] = f"{HOME}/.local/bin:/opt/homebrew/bin:{os.environ.get('PATH', '')}"
WORKFLOWS = os.environ.get("LAUNCHER_WORKFLOWS", f"{HOME}/dotfiles/workmux/workflows.yaml")
AGENTS_CONFIG = os.environ.get("LAUNCHER_AGENTS_CONFIG", f"{HOME}/.config/workmux/config.yaml")
KINDS = ("window", "worktree")
FIELDS = {"kind", "session", "agent", "model", "dir", "window"}
SPLIT = "20%"  # shell pane under the agent, like the pane layout in the workmux config


class Fail(Exception):
    """A loud, human-readable error (shown in the popup, exit 1)."""


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
        if set(w) - FIELDS:
            raise Fail(f"{where}: unknown field(s) {sorted(set(w) - FIELDS)}; allowed {sorted(FIELDS)}")
        w["kind"] = w.get("kind") or "window"
        if w["kind"] not in KINDS:
            raise Fail(f"{where}: kind '{w['kind']}' is not one of {KINDS}")
        if w.get("agent") not in agents:
            raise Fail(f"{where}: agent '{w.get('agent')}' is not in {AGENTS_CONFIG} (have: {', '.join(agents)})")
        if not w.get("dir"):
            raise Fail(f"{where}: dir is required")
        if w["kind"] == "worktree" and w.get("model"):
            raise Fail(f"{where}: model is not supported for kind worktree (workmux add has no model flag)")
        w["session"] = w.get("session") or name.split()[0]
        w["model"] = w.get("model") or ""
        w["window"] = w.get("window") or ""
    return agents, wfs


def make_plan(name, wfs, agents, o):
    """Workflow defaults + overrides (None = keep default, "" = blank) -> a validated plan."""
    if name not in wfs:
        raise Fail(f"unknown workflow '{name}'. Known: {', '.join(wfs)}")
    p = dict(wfs[name], name=name, branch=o.get("branch") or "")
    for k in ("agent", "model", "window", "dir"):
        if o.get(k) is not None:
            p[k] = o[k]
    if p["agent"] != wfs[name]["agent"] and o.get("model") is None:
        p["model"] = ""  # the default model is the old agent's (Copilot ids are not Claude ids)
    p["dir"] = os.path.expanduser(p["dir"])
    if p["agent"] not in agents:
        raise Fail(f"agent '{p['agent']}' is not defined (have: {', '.join(agents)})")
    if not os.path.isdir(p["dir"]):
        raise Fail(f"directory does not exist: {p['dir']}")
    if p["kind"] == "worktree":
        if p["model"]:
            raise Fail("model is not supported for worktree workflows")
        if not p["branch"]:
            raise Fail(f"'{name}' creates a worktree and needs a branch (--branch)")
    else:
        p["window"] = p["window"] or f"{name.split()[-1]}-{datetime.datetime.now():%H%M}"
    return p


def agent_line(agents, agent, model):
    """`env K=V ... <command> <args> [--model M]`, the same shape workmux types into a pane."""
    a = agents[agent]
    args, skip = [], False
    for x in map(str, a.get("args") or []):
        if skip:
            skip = False
        elif model and x == "--model":
            skip = True  # drop the agent's own `--model X` pair; the override replaces it
        elif not (model and x.startswith("--model=")):
            args.append(x)
    argv = [str(a["command"]), *args] + (["--model", model] if model else [])
    env = [f"{k}={v}" for k, v in (a.get("env") or {}).items()]
    return shlex.join((["env", *env] if env else []) + argv)


def tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"tmux {' '.join(args)}: {r.stderr.strip() or 'failed'}")
    return r.stdout.strip()


def ensure_session(session, cwd):
    if subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True).returncode:
        tmux("new-session", "-d", "-s", session, "-c", cwd)


def launch(p, agents, dry, switch):
    s, d = p["session"], p["dir"]
    if p["kind"] == "worktree":
        argv = ["workmux", "add", p["branch"], "-a", p["agent"], "--parent-session", s]
        argv += ["--target-name", p["window"]] if p["window"] else []
        argv += [] if switch else ["-b"]
        print(f"[{p['name']}] session {s} | cd {d} && {shlex.join(argv)}")
        if dry:
            return
        ensure_session(s, d)
        # Inherit the terminal: workmux may ask something (e.g. trust a project config) and its own
        # errors should be visible in the popup. Captured output would hang on a prompt, unseen.
        r = subprocess.run(argv, cwd=d)
        if r.returncode:
            raise Fail(f"{shlex.join(argv)} exited {r.returncode}")
        return
    line = agent_line(agents, p["agent"], p["model"])
    print(f"[{p['name']}] session {s} | window {p['window']} | cd {d}\n  {line}")
    if dry:
        return
    ensure_session(s, d)
    win, pane = tmux("new-window", "-P", "-F", "#{window_id} #{pane_id}",
                     "-t", f"={s}:", "-n", p["window"], "-c", d).split()
    tmux("send-keys", "-t", pane, "-l", line)
    tmux("send-keys", "-t", pane, "Enter")
    tmux("split-window", "-v", "-d", "-l", SPLIT, "-t", pane, "-c", d)
    if switch:
        tmux("switch-client", "-t", win)


def short(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def pick(wfs):
    """fzf over the workflows. Returns (workflow, edit?) or None when cancelled."""
    rows = []
    for n, w in wfs.items():
        model = w["model"] or ("-" if w["kind"] == "worktree" else "(default)")
        rows.append(f"{n:<14} {w['agent']:<14} {model:<18} {short(os.path.expanduser(w['dir']))}")
    # Columns are space-padded; two+ spaces end the first field, so --nth=1 matches the workflow
    # name only (typing "bet" must not fuzzy-match a path or agent column). --tiebreak=index: on a
    # tie YAML order wins, so typing "change" + Enter picks `change plan`, the first listed.
    r = subprocess.run(
        ["fzf", "--delimiter=\\s{2,}", "--nth=1", "--tiebreak=index", "--expect=tab", "--layout=reverse",
         "--info=hidden", "--prompt=workflow> ",
         "--header=enter: launch with defaults    tab: edit fields    esc: cancel\n"
                   "workflow        agent          model              directory"],
        input="\n".join(rows), text=True, stdout=subprocess.PIPE)
    if r.returncode in (1, 130):
        return None
    if r.returncode:
        raise Fail(f"fzf exited {r.returncode}")
    key, _, chosen = r.stdout.partition("\n")
    return re.split(r"\s{2,}", chosen.strip())[0], key == "tab"


def ask(label, default=""):
    """`label [default]: ` -- Enter keeps the default, `-` clears it, anything else replaces it.
    (No prefill: Python on macOS uses libedit, which ignores readline's startup hook.)"""
    import readline  # noqa: F401  -- importing it gives input() line editing
    shown = f" [{default}]" if default else ""
    answer = input(f"  {label}{shown}: ").strip()
    return default if answer == "" else ("" if answer == "-" else answer)


def edit(wf, agents, o):
    while True:
        o["agent"] = ask(f"agent ({', '.join(agents)})", wf["agent"])
        if o["agent"] in agents:
            break
        print(f"  unknown agent '{o['agent']}'")
    if wf["kind"] == "window":
        same = o["agent"] == wf["agent"]  # the workflow's model belongs to its own agent
        o["model"] = ask("model (empty = harness default)" if same else "model (agent changed: harness default)",
                         wf["model"] if same else "")
        o["window"] = ask("window name (empty = auto)", wf["window"])
    else:
        o["window"] = ask("window name (empty = branch name)", wf["window"])
    o["dir"] = ask("directory", wf["dir"])


def run(args):
    agents, wfs = load()
    name = " ".join(args.workflow)
    o = {"agent": args.agent, "model": args.model, "window": args.name, "dir": args.dir, "branch": args.branch}
    if not name:
        chosen = pick(wfs)
        if chosen is None:
            return
        name, want_edit = chosen
        if want_edit:
            print(f"\n  {name}: Enter keeps the [value], '-' clears it")
            edit(wfs[name], agents, o)
        if wfs[name]["kind"] == "worktree" and not o["branch"]:
            o["branch"] = ask("branch (required)")
    launch(make_plan(name, wfs, agents, o), agents, args.dry_run, not args.no_switch)


def main():
    ap = argparse.ArgumentParser(description="Start an agent window for a workflow (see module docstring).")
    ap.add_argument("workflow", nargs="*", help='e.g. change plan; omit to open the picker')
    ap.add_argument("--agent"), ap.add_argument("--model"), ap.add_argument("--name", help="window name")
    ap.add_argument("--dir"), ap.add_argument("--branch", help="for worktree workflows")
    ap.add_argument("--dry-run", action="store_true", help="print what would run, change nothing")
    ap.add_argument("--no-switch", action="store_true", help="leave the client where it is")
    args = ap.parse_args()
    try:
        run(args)
    except Fail as e:
        print(f"\033[31mlauncher: {e}\033[0m", file=sys.stderr)
        if not args.workflow:  # in the popup: keep the message on screen
            try:
                input("press Enter to close")
            except (EOFError, KeyboardInterrupt):
                pass
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)


if __name__ == "__main__":
    main()
