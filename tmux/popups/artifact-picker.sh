#!/usr/bin/env bash
# tmux popup: fuzzy-find an artifact your agents left behind and open it.
#   Enter   -> Plannotator annotate (browser annotation UI, no diff needed)
#   Ctrl-O  -> macOS default app (the browser for .html), e.g. when a page needs
#              localStorage/forms, which Plannotator's sandboxed viewer blocks
#
# Bound in tmux.conf as:  prefix + a
# Test modes (no UI):     --list | --preview FILE | --open FILE | --browse FILE

set -euo pipefail

# tmux's global PATH does not include ~/.local/bin (where plannotator lives).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# Roots searched for *.md and *.html. Target state: camunda/ only. The DagRunner
# entries go away once DAGRUNNER_HOME is moved under camunda/ (separate task).
# dagrunner/worktrees is deliberately absent: full repo checkouts, not artifacts.
ROOTS=(
  "$HOME/.local/share/camunda"
  "$HOME/.local/share/dagrunner/inbox"
  "$HOME/.local/share/dagrunner/runs"
  "$HOME/.local/share/dagrunner/ui-mockup-recovered"
)

# Output: "<MM-DD HH:MM>\t<short path>\t<full path>", newest first.
list() {
  local r
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || continue
    find "$r" \( -name node_modules -o -name .git \) -prune -o \
      -type f \( -name '*.md' -o -name '*.html' \) \
      -exec stat -t '%m-%d %H:%M' -f '%m%t%Sm%t%N' {} +
  done |
    sort -rn |
    awk -F'\t' -v home="$HOME/.local/share/" \
      '{ p = $3; sub("^" home, "", p); print $2 "\t" p "\t" $3 }'
}

preview() {
  case "$1" in
    *.html) perl -0pe 's/<(style|script)\b.*?<\/\1>//gsi; s/<[^>]+>//g' "$1" | grep -v '^[[:space:]]*$' | head -60 ;;
    *)      head -80 "$1" ;;
  esac
}

# Plannotator keeps a local server until the session is closed in its UI, so run
# it detached. NO_ANON_PING=1 keeps its only telemetry off, as in the workmux config.
open_plannotator() {
  NO_ANON_PING=1 nohup plannotator annotate "$1" >/dev/null 2>&1 &
  disown
}

case "${1:-}" in
  --list)    list; exit 0 ;;
  --preview) preview "$2"; exit 0 ;;
  --open)    open_plannotator "$2"; exit 0 ;;
  --browse)  open "$2"; exit 0 ;;
esac

self="$0"
out=$(list | fzf \
  --delimiter=$'\t' --with-nth=1,2 --tiebreak=index \
  --expect=ctrl-o \
  --prompt='artifact> ' \
  --header='enter: Plannotator   ctrl-o: browser/default app' \
  --preview="$self --preview {3}" --preview-window='right:50%:wrap') || exit 0

key=$(sed -n 1p <<<"$out")
file=$(sed -n 2p <<<"$out" | awk -F'\t' '{print $3}')
[ -n "$file" ] || exit 0

if [ "$key" = "ctrl-o" ]; then
  open "$file"
else
  open_plannotator "$file"
fi
