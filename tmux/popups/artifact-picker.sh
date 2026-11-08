#!/usr/bin/env bash
# tmux popup: fuzzy-find an artifact your agents left behind and open it.
#   Enter   -> Plannotator annotate (browser annotation UI, no diff needed). When you
#              submit feedback there, it is pasted into the tmux pane this popup was
#              opened from (the agent), with no Enter pressed. Approve-with-notes counts.
#              Closing the session without feedback pastes nothing.
#   Ctrl-O  -> macOS default app (the browser for .html), e.g. when a page needs
#              localStorage/forms, which Plannotator's sandboxed viewer blocks
#
# Bound in tmux.conf as:  prefix + a
# Test modes (no UI):     --list | --preview FILE | --open FILE [PANE] | --deliver FILE PANE | --browse FILE
# PLANNOTATOR_BIN overrides the plannotator command (tests use a stub).

set -euo pipefail

# tmux's global PATH does not include ~/.local/bin (where plannotator lives).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

# Root searched for *.md and *.html. Everything the agents leave behind lives under camunda/
# (dagrunner's home moved to camunda/dagrunner in dagrunner v0.1.85). A legacy dagrunner worktree is
# a full repo checkout, not an artifact, so camunda/dagrunner/worktrees is pruned should one reappear.
ROOTS=(
  "$HOME/.local/share/camunda"
)

# Output: "<MM-DD HH:MM>\t<short path>\t<full path>", newest first.
list() {
  local r
  for r in "${ROOTS[@]}"; do
    [ -d "$r" ] || continue
    find "$r" \( -name node_modules -o -name .git -o -path "$r/dagrunner/worktrees" \) -prune -o \
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

PLANNOTATOR_BIN="${PLANNOTATOR_BIN:-plannotator}"

note() { tmux display-message -d 5000 "$1" 2>/dev/null || true; }

# Run Plannotator on FILE and wait for the reviewer. It prints one decision when the
# session ends: JSON {"decision": approved|annotated|dismissed, "feedback": ...} with
# --json. Feedback (annotated, or approved with notes) is pasted into PANE as a
# bracketed paste (so newlines do not submit) and NOT submitted: you read it, then Enter.
# If PANE is gone or unknown, the feedback goes to the clipboard instead.
deliver() {
  local file="$1" pane="${2:-}" out decision msg text
  out=$(NO_ANON_PING=1 "$PLANNOTATOR_BIN" annotate "$file" --json 2>/dev/null) || return 0
  [ -n "$out" ] || return 0
  if jq -e . >/dev/null 2>&1 <<<"$out"; then
    decision=$(jq -r '.decision // empty' <<<"$out")
    msg=$(jq -r '.feedback // empty' <<<"$out")
  else
    decision=annotated; msg="$out"            # plain-text output from an older build
    case "$out" in "The user approved."*) decision=approved; msg="" ;; esac
  fi
  case "$decision" in annotated|approved) ;; *) return 0 ;; esac
  [ -n "$msg" ] || { note "Plannotator: approved, no notes ($(basename "$file"))"; return 0; }

  text="Plannotator feedback on ${file}:

${msg}"
  # `display-message -t <missing pane>` still exits 0, so check the pane list instead.
  if [ -n "$pane" ] && tmux list-panes -a -F '#{pane_id}' 2>/dev/null | grep -qxF "$pane" &&
     tmux set-buffer -b plannotator-feedback -- "$text" &&
     tmux paste-buffer -p -d -b plannotator-feedback -t "$pane" 2>/dev/null; then
    note "Plannotator feedback pasted into $pane. Review it, then press Enter."
  else
    printf '%s' "$text" | pbcopy
    note "Plannotator feedback copied to the clipboard (source pane not available)."
  fi
}

# Plannotator keeps a local server until the session is closed in its UI, so the wait
# for its result must outlive this popup. A nohup'd child does not reliably survive the
# popup closing (measured: it never started), so tmux's server runs it: `run-shell -b`.
# Outside tmux (test modes) it falls back to nohup.
# NO_ANON_PING=1 keeps Plannotator's only telemetry off, as in the workmux config.
open_plannotator() {
  local cmd
  cmd=$(printf '%q ' "$SELF" --deliver "$1" "${2:-}")
  cmd=${cmd//\#/##}      # run-shell expands tmux formats; ## is a literal # (paths have #12345)
  if [ -n "${TMUX:-}" ] && tmux run-shell -b "$cmd >/dev/null 2>&1"; then
    return 0
  fi
  nohup "$SELF" --deliver "$1" "${2:-}" >/dev/null 2>&1 &
  disown
}

SELF="$0"
case "${1:-}" in
  --list)    list; exit 0 ;;
  --preview) preview "$2"; exit 0 ;;
  --open)    open_plannotator "$2" "${3:-}"; exit 0 ;;
  --deliver) deliver "$2" "${3:-}"; exit 0 ;;
  --browse)  open "$2"; exit 0 ;;
esac

# The popup opens over the pane you were in, which is still the active pane of the
# session, so that is where Enter-feedback goes.
origin=$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)

self="$SELF"
out=$(list | fzf \
  --delimiter=$'\t' --with-nth=1,2 --tiebreak=index \
  --expect=ctrl-o \
  --prompt='artifact> ' \
  --header='enter: Plannotator (feedback is pasted into the pane you came from)   ctrl-o: browser/default app' \
  --preview="$self --preview {3}" --preview-window='right:50%:wrap') || exit 0

key=$(sed -n 1p <<<"$out")
file=$(sed -n 2p <<<"$out" | awk -F'\t' '{print $3}')
[ -n "$file" ] || exit 0

if [ "$key" = "ctrl-o" ]; then
  open "$file"
else
  open_plannotator "$file" "$origin"
fi
