export COLORTERM=truecolor

# ------------------------------------------------------------
# Homebrew (ensure HOMEBREW_PREFIX is set for non-login shells)
# ------------------------------------------------------------
if [ -z "$HOMEBREW_PREFIX" ]; then
  if [[ "$(uname -m)" == "arm64" ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  else
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

# ------------------------------------------------------------
# Tmux auto-launch (attach to main, or give the window its own
# session if main is already attached elsewhere — avoids
# multiple differently-sized clients fighting over one session)
# ------------------------------------------------------------
if command -v tmux &>/dev/null && [ -z "$TMUX" ]; then
  if ! tmux has-session -t main 2>/dev/null; then
    tmux new -s main
  elif [ -z "$(tmux list-clients -t main 2>/dev/null)" ]; then
    tmux attach -t main
  else
    tmux new -s "term-$$"
  fi
fi

# ------------------------------------------------------------
# Docker CLI completions (must be before compinit)
# ------------------------------------------------------------

# ------------------------------------------------------------
# Completion
# ------------------------------------------------------------
autoload -Uz compinit
compinit

# ------------------------------------------------------------
# History
# ------------------------------------------------------------
HISTFILE=~/.zsh_history
HISTSIZE=10000
SAVEHIST=10000
setopt HIST_IGNORE_DUPS
setopt SHARE_HISTORY
setopt APPEND_HISTORY

# ------------------------------------------------------------
# Environment
# ------------------------------------------------------------
export EDITOR=micro
export VISUAL=micro

# ------------------------------------------------------------
# Aliases
# ------------------------------------------------------------
alias lg='lazygit'
alias l='lsd'
alias ll='lsd -l'
alias la='lsd -a'
alias lla='lsd -la'
alias lt='lsd --tree'
alias c=clear
alias m='micro'
alias md='leaf'
alias dr='dagrun'
alias wrapon='tput rmam'
alias wrapoff='tput smam'

# ------------------------------------------------------------
# PATH
# ------------------------------------------------------------
export PATH="$HOME/.local/bin:$PATH"
export PATH="$HOME/.npm-global/bin:$PATH"

# ------------------------------------------------------------
# asdf (for Maven, Java, etc.)
# asdf >= 0.16 is a compiled binary — no shell init script; shims just need to be on PATH
# ------------------------------------------------------------
export PATH="$HOME/.asdf/shims:$PATH"

# ------------------------------------------------------------
# fzf
# ------------------------------------------------------------
[ -f ~/.fzf.zsh ] && source ~/.fzf.zsh
export FZF_DEFAULT_COMMAND='fd --type f --hidden --follow --exclude .git'
export FZF_DEFAULT_OPTS='--height 40% --layout=reverse'

# ------------------------------------------------------------
# zoxide (smarter cd)
# ------------------------------------------------------------
eval "$(zoxide init zsh)"

# ------------------------------------------------------------
# Prompt
# ------------------------------------------------------------
autoload -Uz vcs_info
precmd() { vcs_info }
setopt PROMPT_SUBST
zstyle ':vcs_info:*' enable git
zstyle ':vcs_info:git:*' formats ' %%F{244}(%b)%%f'
PROMPT='%F{111}%~%f${vcs_info_msg_0_} %F{244}❯%f '

# ------------------------------------------------------------
# Plugins (brew-installed)
# ------------------------------------------------------------
source "${HOMEBREW_PREFIX}/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
source "${HOMEBREW_PREFIX}/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"

# ------------------------------------------------------------
# Local overrides (not tracked in dotfiles)
# ------------------------------------------------------------
# shellcheck disable=SC1090
[ -f ~/.zshrc.local ] && source ~/.zshrc.local

# bun completions
[ -s "/Users/eddie.tsedeke/.bun/_bun" ] && source "/Users/eddie.tsedeke/.bun/_bun"

# bun
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"

# ------------------------------------------------------------
# Yazi shell wrapper
# ------------------------------------------------------------
function y() {
	local tmp="$(mktemp -t "yazi-cwd.XXXXXX")" cwd
	command yazi "$@" --cwd-file="$tmp"
	IFS= read -r -d '' cwd < "$tmp"
	[ "$cwd" != "$PWD" ] && [ -d "$cwd" ] && builtin cd -- "$cwd"
	command rm -f -- "$tmp"
}

# --- Claude Code profiles ---
# Work profile (default ~/.claude — Desktop app compatible)
alias ccw='CLAUDE_CONFIG_DIR=~/.claude claude'
# Personal / machine-aware
alias ccp='CLAUDE_CONFIG_DIR=~/.claude-personal claude'
# Run claude -p from home dir so only user-level settings apply (bypasses project/local settings)
ccc() { (cd ~ && claude -p "$@"); }

eval "$(atuin init zsh)"
