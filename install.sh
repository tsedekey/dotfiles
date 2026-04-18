#!/usr/bin/env bash
set -euo pipefail

# =============================================================
# dotfiles installer
# Symlinks repo-managed files into the home directory.
# Backs up existing targets before replacing them.
# Idempotent: safe to run multiple times.
# =============================================================

DOTFILES_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="$HOME/.dotfiles-backup/$(date +%Y%m%d-%H%M%S)"

info()  { printf "\033[0;34m[info]\033[0m  %s\n" "$1"; }
ok()    { printf "\033[0;32m[ok]\033[0m    %s\n" "$1"; }
warn()  { printf "\033[0;33m[warn]\033[0m  %s\n" "$1"; }

# link_file <source> <target>
# If target exists and is not already the correct symlink, back it up first.
link_file() {
  local src="$1" dst="$2"
  local dst_dir
  dst_dir="$(dirname "$dst")"

  # Ensure parent directory exists
  if [ ! -d "$dst_dir" ]; then
    mkdir -p "$dst_dir"
    info "Created directory $dst_dir"
  fi

  # Already correctly linked
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    ok "$dst -> $src (already linked)"
    return
  fi

  # Backup existing file/symlink/directory
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    mkdir -p "$BACKUP_DIR"
    local backup_path="$BACKUP_DIR/$(basename "$dst")"
    mv "$dst" "$backup_path"
    warn "Backed up $dst -> $backup_path"
  fi

  ln -s "$src" "$dst"
  ok "$dst -> $src"
}

# =============================================================
# Symlink mappings
# =============================================================

info "Installing dotfiles from $DOTFILES_DIR"
echo ""

# --- Shell (zsh only) ---
info "Shell configuration"
link_file "$DOTFILES_DIR/zsh/.zshrc"           "$HOME/.zshrc"
link_file "$DOTFILES_DIR/zsh/.zprofile"        "$HOME/.zprofile"
echo ""

# --- Git ---
info "Git configuration"
link_file "$DOTFILES_DIR/git/.gitconfig"       "$HOME/.gitconfig"
echo ""

# --- Tmux ---
info "Tmux configuration"
link_file "$DOTFILES_DIR/tmux/.tmux.conf"      "$HOME/.tmux.conf"
echo ""

# --- LSD ---
info "LSD configuration"
link_file "$DOTFILES_DIR/lsd/config.yaml"      "$HOME/.config/lsd/config.yaml"
echo ""

# --- aichat ---
info "aichat configuration"
link_file "$DOTFILES_DIR/aichat/config.yaml"   "$HOME/Library/Application Support/aichat/config.yaml"
echo ""

# --- VS Code ---
info "VS Code configuration"
VSCODE_USER_DIR="$HOME/Library/Application Support/Code/User"
link_file "$DOTFILES_DIR/vscode/settings.json"     "$VSCODE_USER_DIR/settings.json"
link_file "$DOTFILES_DIR/vscode/keybindings.json"  "$VSCODE_USER_DIR/keybindings.json"
echo ""

# --- GitHub CLI ---
info "GitHub CLI configuration"
link_file "$DOTFILES_DIR/gh/config.yml"        "$HOME/.config/gh/config.yml"
echo ""

# --- Maven ---
info "Maven flags"
link_file "$DOTFILES_DIR/maven/.mvn-flags.list" "$HOME/.mvn-flags.list"
echo ""

# --- asdf ---
info "asdf tool versions"
link_file "$DOTFILES_DIR/asdf/.tool-versions"  "$HOME/.tool-versions"
echo ""

# =============================================================
# Post-install: TPM (tmux plugin manager)
# =============================================================

if [ ! -d "$HOME/.tmux/plugins/tpm" ]; then
  info "Installing tmux plugin manager (TPM)..."
  git clone https://github.com/tmux-plugins/tpm "$HOME/.tmux/plugins/tpm"
  ok "TPM installed. Press prefix + I inside tmux to install plugins."
else
  ok "TPM already installed"
fi
echo ""

# =============================================================
# Post-install: fzf keybindings and completion
# =============================================================

if command -v fzf >/dev/null 2>&1; then
  if [ ! -f "$HOME/.fzf.zsh" ]; then
    info "Setting up fzf shell integration..."
    /opt/homebrew/opt/fzf/install --key-bindings --completion --no-update-rc --no-fish
    ok "fzf shell integration installed"
  else
    ok "fzf shell integration already present"
  fi
else
  warn "fzf not found -- install via: brew bundle --file=$DOTFILES_DIR/Brewfile"
fi
echo ""

# =============================================================
# Post-install: npm global directory
# =============================================================

if [ ! -d "$HOME/.npm-global" ]; then
  mkdir -p "$HOME/.npm-global"
  npm config set prefix "$HOME/.npm-global" 2>/dev/null || true
  ok "Created $HOME/.npm-global for npm global packages"
else
  ok "$HOME/.npm-global already exists"
fi
echo ""

# =============================================================
# Summary
# =============================================================

echo "------------------------------------------------------------"
ok "Dotfiles installation complete."
echo ""
if [ -d "$BACKUP_DIR" ]; then
  info "Backups saved to: $BACKUP_DIR"
fi
echo ""
info "Next steps:"
echo "  1. Restart your shell or run: exec zsh"
echo "  2. Install Homebrew packages: brew bundle --file=$DOTFILES_DIR/Brewfile"
echo "  3. Create local overrides as needed:"
echo "       ~/.zshrc.local"
echo "       ~/.gitconfig.local"
echo "------------------------------------------------------------"
