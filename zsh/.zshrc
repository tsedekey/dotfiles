# ------------------------------------------------------------
# Tmux auto-launch (attach to main or create it)
# ------------------------------------------------------------
if command -v tmux &>/dev/null && [ -z "$TMUX" ]; then
  tmux attach -t main 2>/dev/null || tmux new -s main
fi

# ------------------------------------------------------------
# Docker CLI completions (must be before compinit)
# ------------------------------------------------------------
fpath=("$HOME/.docker/completions" $fpath)

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
export MANPAGER="sh -c 'col -bx | bat -l man -p'"

# ------------------------------------------------------------
# Aliases
# ------------------------------------------------------------
alias lg='lazygit'
alias ld='lazydocker'
alias ls='lsd'
alias l='ls -l'
alias la='ls -a'
alias lla='ls -la'
alias k=kubectl
alias h=helm
alias c=clear
alias wrapon='tput rmam'
alias wrapoff='tput smam'

# ------------------------------------------------------------
# PATH
# ------------------------------------------------------------
export PATH="$HOME/.local/bin:$PATH"
export PATH="$HOME/.npm-global/bin:$PATH"
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin"

# ------------------------------------------------------------
# asdf (for Maven, Java, etc.)
# ------------------------------------------------------------
. /opt/homebrew/opt/asdf/libexec/asdf.sh

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
# Git branch indicator prompt
# ------------------------------------------------------------
autoload -Uz vcs_info
precmd() { vcs_info }

setopt PROMPT_SUBST

zstyle ':vcs_info:*' enable git
zstyle ':vcs_info:git:*' formats ' (%b)'

PROMPT='%~${vcs_info_msg_0_} > '

# ------------------------------------------------------------
# Plugins (brew-installed)
# ------------------------------------------------------------
source /opt/homebrew/share/zsh-autosuggestions/zsh-autosuggestions.zsh
source /opt/homebrew/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh

# ------------------------------------------------------------
# Local overrides (not tracked in dotfiles)
# ------------------------------------------------------------
# shellcheck disable=SC1090
[ -f ~/.zshrc.local ] && source ~/.zshrc.local
