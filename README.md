# dotfiles

Personal macOS developer environment configuration. Extracted from a live setup and sanitized for portability. Supports both Apple Silicon and Intel Macs.

## What's included

| Directory | Contents |
|---|---|
| `zsh/` | `.zshrc` (tmux auto-launch, completion, history, aliases, fzf+fd, zoxide, vcs_info prompt), `.zprofile` (Homebrew init, auto-detects Apple Silicon vs Intel) |
| `git/` | `.gitconfig` with conventional commit aliases (feat, fix, chore, etc. via `_cc` helper), micro editor, git-delta pager, LFS, pull rebase |
| `tmux/` | `.tmux.conf` with Catppuccin Mocha theme, Ctrl-a prefix, TPM, resurrect, continuum |
| `lsd/` | `config.yaml` for the `lsd` ls replacement (icons, git column, header) |
| `gh/` | GitHub CLI config (https protocol, `co` alias) |
| `maven/` | `.mvn-flags.list` (flag definitions for Maven workflows) |
| `asdf/` | `.tool-versions` (node version) |
| `Brewfile` | Cross-machine Homebrew baseline (taps, formulae) |

## Key tools

- **Shell**: zsh (no frameworks) with autosuggestions + syntax highlighting
- **Terminal editor**: micro
- **Terminal multiplexer**: tmux (Catppuccin Mocha, Ctrl-a prefix, session restore via resurrect+continuum)
- **Git pager**: delta (side-by-side, line numbers)
- **File navigation**: fzf + fd, zoxide (`z` for smart cd)
- **Man pages**: bat as MANPAGER
- **Theme**: Catppuccin Mocha across tmux

## What's excluded

| Item | Reason |
|---|---|
| `~/.npmrc` | Contains npm auth token |
| `~/.config/configstore/snyk.json` | Contains OAuth tokens |
| `~/.config/gh/hosts.yml` | Contains GitHub auth credentials |
| `~/.ssh/config` | Machine-specific (only contains colima include) |
| `~/.ssh/known_hosts` | Machine-specific |
| `~/.config/starship.toml` | Not actively used; excluded by choice |
| `~/.config/iterm2/` | Auto-managed by iTerm2 |
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

| File | Sourced by |
|---|---|
| `~/.zshrc.local` | `zsh/.zshrc` |
| `~/.gitconfig.local` | `git/.gitconfig` (via `[include]`) |

Use these for:
- Work-specific aliases and paths
- Credential helpers
- Private repo URLs
- Google Cloud SDK integration
- Egnyte CLI integration
- Any machine-specific exports

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

```bash
# Install everything from the Brewfile
brew bundle --file=~/dotfiles/Brewfile

# On work machines, layer work-only packages from a local Brewfile
brew bundle --file=~/.Brewfile.local

# Check what's missing
brew bundle check --file=~/dotfiles/Brewfile

# Update the Brewfile from current machine state
brew bundle dump --file=~/dotfiles/Brewfile --force
```

The repo `Brewfile` is a minimal cross-machine baseline. For machine-specific packages (GUI apps, work tools, fonts, language runtimes), keep a local-only overlay at `~/.Brewfile.local` and run both `brew bundle` commands on that machine.

## Manual post-install steps

1. **tmux plugins**: Open tmux and press `Ctrl-a` then `I` (capital i) to install TPM plugins
2. **fzf**: The installer sets up fzf shell integration automatically for your architecture. If `~/.fzf.zsh` is missing, re-run `~/dotfiles/install.sh`
3. **asdf plugins**: Install the nodejs plugin: `asdf plugin add nodejs && asdf install`
4. **Git credentials**: Set up credential helpers in `~/.gitconfig.local`
5. **npm global dir**: The installer creates `~/.npm-global` — run `npm config set prefix ~/.npm-global` if not already set

## Notes

- Shell is **zsh only** -- no bash configs are managed
- Catppuccin Mocha is the consistent theme across tmux
- `zsh-autosuggestions` and `zsh-syntax-highlighting` are brew-installed and sourced in `.zshrc`
- Terminal editor is **micro** (set as `$EDITOR` and `$VISUAL`, configured as git core editor)
- **git-delta** is configured as the git pager with side-by-side diffs and line numbers
- **zoxide** provides smart `cd` via the `z` command
- **bat** is used as the MANPAGER for colorized man pages
- **fzf** uses **fd** as its default command for fast, `.gitignore`-aware file finding
- Conventional commit aliases (`git feat`, `git fix`, etc.) use a shared `cc` helper to avoid duplication
- Work-specific functions (Maven `mci`, `kibana` helper) are not included -- add them to `~/.zshrc.local` if needed
