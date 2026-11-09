# dotfiles

Personal macOS developer environment configuration. Extracted from a live setup and sanitized for portability. Supports both Apple Silicon and Intel Macs.

## What's included

| Directory | Contents |
|---|---|
| `zsh/` | `.zshrc` (tmux auto-launch, completion, history, aliases, fzf+fd, zoxide, vcs_info prompt, yazi), `.zprofile` (Homebrew init, auto-detects Apple Silicon vs Intel) |
| `git/` | `.gitconfig` with conventional commit aliases (feat, fix, chore, etc. via `_cc` helper), micro editor, git-delta pager, LFS, pull rebase |
| `tmux/` | `.tmux.conf` with Catppuccin Mocha theme, Ctrl-a prefix, TPM, resurrect, continuum, popups: `prefix a` artifact picker, `prefix W` workmux dashboard, `prefix N` workflow launcher (`tmux/popups/launcher.py`, defaults in `workmux/workflows.yaml`), `prefix F` finish window (`tmux/popups/finish-window.py`), `prefix B` sidebar toggle |
| `lsd/` | `config.yaml` for the `lsd` ls replacement (icons, git column, header) |
| `gh/` | GitHub CLI config (https protocol, `co` alias) |
| `maven/` | `.mvn-flags.list` (flag definitions for Maven workflows) |
| `asdf/` | `.tool-versions` (node version) |
| `ghostty/` | `config` with Catppuccin Mocha theme, JetBrains Mono, shell integration, codepoint map for the agent-logo font |
| `fonts/agent-icons/` | `AgentIcons.otf`: Claude, OpenCode and Copilot logos as glyphs (U+E400-E403) for the workmux sidebar; rebuild with `./build.py` (`--box` sets the size); sources are CC0 Simple Icons SVGs; `install.sh` copies it to `~/Library/Fonts` |
| `Brewfile` | Core Homebrew tools (zsh, tmux, git, editors, dev tools, terminal) |

## Key tools

- **Terminal**: Ghosty (Catppuccin Mocha, JetBrains Mono, shell integration)
- **Shell**: zsh (no frameworks) with autosuggestions + syntax highlighting
- **Terminal editor**: micro
- **Terminal multiplexer**: tmux (Catppuccin Mocha, Ctrl-a prefix, session restore via resurrect+continuum)
- **File explorer**: yazi (`yy` for quick access)
- **Git pager**: delta (side-by-side, line numbers)
- **File navigation**: fzf + fd, zoxide (`z` for smart cd)
- **Man pages**: bat as MANPAGER
- **Theme**: Catppuccin Mocha across Ghosty and tmux

## What's excluded

| Item | Reason |
|---|---|
| `~/.npmrc` | Contains npm auth token |
| `~/.config/configstore/snyk.json` | Contains OAuth tokens |
| `~/.config/gh/hosts.yml` | Contains GitHub auth credentials |
| `~/.ssh/config` | Machine-specific (only contains colima include) |
| `~/.ssh/known_hosts` | Machine-specific |
| `~/.config/starship.toml` | Not actively used; excluded by choice |
| `~/.config/gcloud/` | Machine-specific cloud state |
| `~/.docker/` | Docker Desktop managed state |
| `~/.nvm/`, `~/.colima/` | Tool installations, not config |
| `~/.local/bin/` | Contains third-party binaries |
| `~/.yarnrc` | Auto-generated |

## Bootstrap a new macOS profile

Works on both Apple Silicon and Intel — `.zprofile` detects the architecture automatically.

```bash
# 1. Install Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# Homebrew will print the shellenv eval line for your arch — run it to activate

# 2. Clone dotfiles
git clone https://github.com/<your-username>/dotfiles.git ~/dotfiles

# 3. Install Homebrew packages
brew bundle --file=~/dotfiles/Brewfile

# 4. Run the installer (symlinks configs, installs TPM, sets up fzf)
~/dotfiles/install.sh

# 5. Restart your shell
exec zsh
```

## Local overrides

Machine-specific or private configuration goes in `.local` files that are sourced by the managed configs but **not tracked** in this repo:

| File | Purpose |
|---|---|
| `~/.zshrc.local` | Google Cloud SDK, Egnyte CLI, and other machine-specific shell config |
| `~/.Brewfile.local` | Machine-specific Homebrew packages (K8s tools, cloud tools, dev utilities) |
| `~/.gitconfig.local` | Git credential helpers, work-specific config |

Use `~/.zshrc.local` for:
- Google Cloud SDK integration
- Egnyte CLI integration
- Machine-specific environment variables
- Work or machine-specific shell functions

Use `~/.Brewfile.local` for:
- Kubernetes & container tools (k9s, helm, kubectx, kind, minikube, lazydocker)
- Cloud infrastructure tools (cloudflared, snyk, etc.)
- Specialized development utilities
- Monitoring & observability tools

Example `~/.gitconfig.local`:
```ini
[credential]
    helper = /usr/local/share/gcm-core/git-credential-manager
[credential "https://dev.azure.com"]
    useHttpPath = true
[credential "https://github.com"]
    username = your-username
```

## Homebrew packages

### Core packages (portable, in dotfiles)

```bash
# Install core Homebrew packages
brew bundle --file=~/dotfiles/Brewfile

# Check what's missing
brew bundle check --file=~/dotfiles/Brewfile
```

### Local machine-specific packages

```bash
# Install machine-specific packages (K8s, cloud tools, dev utilities)
brew bundle --file=~/.Brewfile.local

# Check what's installed vs expected
brew bundle check --file=~/dotfiles/Brewfile
brew bundle check --file=~/.Brewfile.local

# Update the core Brewfile from current machine state (use cautiously!)
brew bundle dump --file=~/dotfiles/Brewfile --force
```

The repo `Brewfile` is a minimal cross-machine baseline. For machine-specific packages (Kubernetes, cloud tools, specialized dev utilities), keep them in `~/.Brewfile.local` and run both `brew bundle` commands on that machine.

## Manual post-install steps

1. **tmux plugins**: Open tmux and press `Ctrl-a` then `I` (capital i) to install TPM plugins
2. **fzf**: The installer sets up fzf shell integration automatically for your architecture. If `~/.fzf.zsh` is missing, re-run `~/dotfiles/install.sh`
3. **asdf plugins**: Install the nodejs plugin: `asdf plugin add nodejs && asdf install`
4. **Git credentials**: Set up credential helpers in `~/.gitconfig.local`
5. **npm global dir**: The installer creates `~/.npm-global` — run `npm config set prefix ~/.npm-global` if not already set

## Notes

- **Shell**: zsh only — bash configs are not managed (all shell config lives in `.zshrc` and `.zshrc.local`)
- **Theme**: Catppuccin Mocha is consistent across Ghosty and tmux
- **Plugins**: `zsh-autosuggestions` and `zsh-syntax-highlighting` are brew-installed and sourced
- **Terminal editor**: micro (set as `$EDITOR` and `$VISUAL`, configured as git core editor)
- **File explorer**: yazi (`yy` alias) for interactive file browsing
- **Git pager**: git-delta with side-by-side diffs and line numbers
- **Smart cd**: zoxide provides `z` command for directory jumping
- **Man pages**: bat is used as MANPAGER for colorized output
- **Fuzzy finding**: fzf uses fd as default command for fast, `.gitignore`-aware file finding
- **Git aliases**: Conventional commit aliases (`git feat`, `git fix`, etc.)
- **TMUX**: Auto-launches on shell startup, connects to `main` session
- **Brewfile strategy**: Core tools in `dotfiles/Brewfile` (portable), specialized tools in `~/.Brewfile.local` (machine-specific)
