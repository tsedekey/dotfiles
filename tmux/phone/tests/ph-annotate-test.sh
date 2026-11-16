#!/usr/bin/env bash
# Tests for ~/dotfiles/tmux/phone/ph-annotate. Uses a stand-in for plannotator (STUB) and a throwaway tmux server
# for the delivery checks. The only things touched on the real system: ports 8444-8450 on loopback (briefly), and
# the real `tailscale serve` config in T14/T15 (cleaned up and verified empty at the end).
set -uo pipefail
SP="$(cd "$(dirname "$0")" && pwd)"
PA="$SP/../ph-annotate"
# The live checks (T14/T15) need the real tailnet name; it is looked up here, never written down.
TSHOST="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)"
[ -n "$TSHOST" ] || { echo "needs a running Tailscale with HTTPS certificates (tailscale status --json has no Self.DNSName)"; exit 2; }
TSHOST_RE="${TSHOST//./\\.}"
W="$(mktemp -d "${TMPDIR:-/tmp}/phanno-work.XXXXXX")"; mkdir -p "$W/root" "$W/outside" "$W/state"
TD="$(mktemp -d "${TMPDIR:-/tmp}/phanno-tmux.XXXXXX")"
trap 'rm -rf "$W" "$TD"' EXIT
ST="$W/state"; ROOT="$W/root"
pass=0; fail=0
ck() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; pass=$((pass+1)); else echo "  FAIL  $1   (expected '$2', got '$3')"; fail=$((fail+1)); fi; }
T() { env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$TD" tmux -f /dev/null "$@"; }

# ---- the stand-in for plannotator
cat > "$W/stub-pn" <<'EOF'
#!/usr/bin/env bash
echo "$$ PORT=${PLANNOTATOR_PORT:-} AI=${PLANNOTATOR_AI:-unset} ARGS=$*" >> "${STUB_LOG:-/dev/null}"
file="${2:-}"; mode="${STUB_MODE:-annotate}"
if [ "$1" = review ]; then
  shift; while [ $# -gt 0 ]; do case "$1" in --patch-file) cp "$2" "${STUB_PATCH_COPY:-/dev/null}" 2>/dev/null; echo "TITLE-OF-REVIEW" >> "${STUB_LOG:-/dev/null}" ;; --title) echo "TITLE=$2" >> "${STUB_LOG:-/dev/null}" ;; esac; shift; done
  file="review"
fi
echo "Resolved: $file" >&2
if [ "$mode" = fail ]; then echo "tailscale serve: HTTPS not enabled" >&2; exit 1; fi
p="${PLANNOTATOR_PORT:-}"; [ "$mode" = badport ] && p=9999
printf '\n  Plannotator session ready: https://hub.example.ts.net:%s\n' "$p" >&2
case "$mode" in
  hang|badport)  trap 'exit 0' TERM; while :; do sleep 0.2; done ;;
  ignoreterm)    trap '' TERM; while :; do sleep 0.2; done ;;
  annotate)
    sleep "${STUB_DELAY:-1}"
    fb=$'# File Feedback\n\nI have reviewed this file and have 1 piece of feedback:\n\n## 1. (line 3) Feedback on: "x"\n> a comment with `backticks`, $dollar and rm -rf /tmp/should-not-exist\n'
    [ -n "${STUB_PAD:-}" ] && fb="$fb$(head -c "$STUB_PAD" /dev/zero | tr '\0' 'x')"
    jq -nc --arg f "$fb" --arg t "$file" '{decision:"annotated",feedback:$f,annotationCount:1,target:$t}' ;;
  approved_empty) sleep 1; echo '{"decision":"approved","feedback":"","annotationCount":0}' ;;
  dismissed)      sleep 1; echo '{"decision":"dismissed","feedback":"","annotationCount":0}' ;;
esac
EOF
chmod +x "$W/stub-pn"

# ---- a pane app that turns bracketed paste on and records every byte it receives
cat > "$W/bp-recorder.py" <<'EOF'
import sys, os, tty, termios, time, select
fd = sys.stdin.fileno(); tty.setraw(fd)
sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()
end = time.time() + float(sys.argv[2])
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.1)
    if r:
        d = os.read(fd, 65536)
        with open(sys.argv[1], "ab") as f: f.write(d)
EOF

echo "hello" > "$ROOT/doc.md"; echo "<h1>x</h1>" > "$ROOT/page.html"; echo "secret" > "$ROOT/.env"
echo "x" > "$ROOT/run.sh"; echo "k" > "$ROOT/server.pem"; echo "outside" > "$W/outside/o.md"
ln -s "$W/outside/o.md" "$ROOT/link.md"
head -c 6000000 /dev/zero | tr '\0' 'a' > "$ROOT/big.md"

# pa [VAR=val ...] -- args...   runs ph-annotate with the test environment; sets RC, OUT, ERR
pa() {
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$TD" PH_ANNOTATE_PLANNOTATOR="$W/stub-pn" PH_ANNOTATE_STATE_DIR="$ST" \
      PH_ANNOTATE_ROOTS="$ROOT" STUB_LOG="$W/stub.log" ${envs[@]+"${envs[@]}"} "$PA" "$@" >"$W/out.txt" 2>"$W/err.txt"
  RC=$?; OUT=$(cat "$W/out.txt"); ERR=$(cat "$W/err.txt")
}
alive() { kill -0 "$1" 2>/dev/null && echo yes || echo no; }

echo "== T1 refusals (exit 3, nothing started) =="
: > "$W/stub.log"
pa -- "$ROOT/nope.md";            ck "missing file" 3 "$RC"
pa -- "$W/outside/o.md";          ck "file outside the allowed folders" 3 "$RC"
pa -- "$ROOT/link.md";            ck "symlink that leads outside" 3 "$RC"
pa -- "$ROOT/.env";               ck ".env refused" 3 "$RC"
pa -- "$ROOT/server.pem";         ck ".pem refused" 3 "$RC"
pa -- "$ROOT/run.sh";             ck "wrong extension refused" 3 "$RC"
pa -- "$ROOT/big.md";             ck "file over 5 MB refused" 3 "$RC"
pa -- "$ROOT/../outside/o.md";    ck "dot-dot escape refused" 3 "$RC"
ck "plannotator was never started by any of these" 0 "$(wc -l < "$W/stub.log" | tr -d ' ')"
pa -- ;                           ck "no argument = usage error" 2 "$RC"
pa -- --bogus "$ROOT/doc.md";     ck "unknown option = usage error" 2 "$RC"

echo "== T2 port choice =="
python3 - "$W" <<'PY' &
import socket, sys, time
socks = []
for p in (8444, 8445):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", p)); s.listen(1); socks.append(s)
time.sleep(25)
PY
HOLD=$!; sleep 1
: > "$W/stub.log"; pa STUB_MODE=annotate STUB_DELAY=0 -- "$ROOT/doc.md"
ck "skips the two busy ports, picks 8446" "PORT 8446" "$(grep '^PORT' "$W/out.txt")"
ck "plannotator was told PLANNOTATOR_PORT=8446" 1 "$(grep -c 'PORT=8446' "$W/stub.log")"
ck "plannotator was started with its AI features DISABLED" 1 "$(grep -c 'AI=disabled' "$W/stub.log")"
ck "URL line ends with :8446" 1 "$(grep -c '^URL https://.*:8446$' "$W/out.txt")"
kill $HOLD 2>/dev/null; wait $HOLD 2>/dev/null
python3 - <<'PY' &
import socket, time
socks = []
for p in range(8444, 8451):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", p)); s.listen(1); socks.append(s)
time.sleep(12)
PY
HOLD=$!; sleep 1
pa -- "$ROOT/doc.md";             ck "all 7 ports busy = exit 4" 4 "$RC"
kill $HOLD 2>/dev/null; wait $HOLD 2>/dev/null

echo "== T3 happy path, feedback pasted into an app pane with NO Enter =="
T new-session -d -s phone -x 120 -y 30 -n app "python3 $W/bp-recorder.py $W/recv.bin 120"
T new-window -d -t phone: -n sh "bash --norc --noprofile"
sleep 1.2
PAPP=$(T display-message -p -t phone:app.0 '#{pane_id}'); PSH=$(T display-message -p -t phone:sh.0 '#{pane_id}')
: > "$W/recv.bin"; pa STUB_MODE=annotate STUB_DELAY=1 -- "$ROOT/doc.md" --pane "$PAPP" --result-file "$W/result.json"
ck "exit 0" 0 "$RC"
ck "prints DECISION annotated" 1 "$(grep -c '^DECISION annotated$' "$W/out.txt")"
ck "prints DELIVERED <pane>" "DELIVERED $PAPP" "$(grep '^DELIVERED' "$W/out.txt")"
sleep 0.5
python3 - "$W/recv.bin" "$ROOT/doc.md" <<'PY' > "$W/recv.check"
import sys, os
d = open(sys.argv[1], "rb").read()
real = os.path.realpath(sys.argv[2]).encode()
start, end = b"\x1b[200~", b"\x1b[201~"
print("starts_with_paste_marker", d.startswith(start))
print("ends_with_end_marker_and_nothing_after", d.endswith(end))
body = d[len(start):-len(end)] if d.startswith(start) and d.endswith(end) else b""
body = body.replace(b"\r", b"\n")
print("header_line", body.startswith(b"Plannotator feedback on " + real + b":\n\n# File Feedback"))
print("comment_text_intact", b"> a comment with `backticks`, $dollar and rm -rf /tmp/should-not-exist" in body)
print("no_enter_after_paste", not d.endswith(end + b"\r") and b"\x1b[201~\r" not in d)
PY
ck "paste opens with the bracketed-paste marker" "starts_with_paste_marker True" "$(grep starts_with "$W/recv.check")"
ck "paste ends with the end marker and NOTHING after it (no Enter)" "ends_with_end_marker_and_nothing_after True" "$(grep ends_with "$W/recv.check")"
ck "header line matches the desk picker's format" "header_line True" "$(grep header_line "$W/recv.check")"
ck "special characters arrive intact" "comment_text_intact True" "$(grep comment_text "$W/recv.check")"
ck "no Enter after the paste" "no_enter_after_paste True" "$(grep no_enter "$W/recv.check")"
ck "result file: delivered true" true "$(jq -r .delivered "$W/result.json")"
ck "result file: mode 600" "-rw-------" "$(ls -l "$W/result.json" | cut -c1-10)"
ck "result file holds the decision and annotation count" "annotated 1" "$(jq -r '"\(.decision) \(.annotationCount)"' "$W/result.json")"
ck "lock released" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"
ck "work folders removed" 0 "$(ls -d "$ST"/run.* 2>/dev/null | wc -l | tr -d ' ')"

echo "== T4-T7 delivery refusals and other decisions =="
pa STUB_MODE=annotate STUB_DELAY=0 -- "$ROOT/doc.md" --pane "$PSH"
ck "pane that is a shell: exit 7" 7 "$RC"; ck "reason pane_is_a_shell" "NOT_DELIVERED pane_is_a_shell" "$(grep '^NOT_DELIVERED' "$W/out.txt")"
sleep 0.4; ck "nothing typed into the shell" 0 "$(T capture-pane -p -t "$PSH" | grep -c 'File Feedback')"
pa STUB_MODE=annotate STUB_DELAY=0 -- "$ROOT/doc.md" --pane "%999"
ck "pane that no longer exists: exit 7" 7 "$RC"; ck "reason pane_gone" "NOT_DELIVERED pane_gone" "$(grep '^NOT_DELIVERED' "$W/out.txt")"
pa STUB_MODE=annotate STUB_DELAY=0 PH_ANNOTATE_MAX_FEEDBACK=50 -- "$ROOT/doc.md" --pane "$PAPP"
ck "feedback too long: exit 7" 7 "$RC"; ck "reason feedback_too_long" "NOT_DELIVERED feedback_too_long" "$(grep '^NOT_DELIVERED' "$W/out.txt")"
pa STUB_MODE=annotate STUB_DELAY=0 -- "$ROOT/doc.md"
ck "no pane given: exit 0 (not an error)" 0 "$RC"; ck "reason no_pane_given" "NOT_DELIVERED no_pane_given" "$(grep '^NOT_DELIVERED' "$W/out.txt")"
pa STUB_MODE=approved_empty -- "$ROOT/doc.md" --pane "$PAPP"
ck "approved without notes: exit 0" 0 "$RC"; ck "DECISION approved, nothing to send" "NOT_DELIVERED nothing_to_send" "$(grep '^NOT_DELIVERED' "$W/out.txt")"
pa STUB_MODE=dismissed -- "$ROOT/doc.md" --pane "$PAPP"
ck "dismissed: exit 0" 0 "$RC"; ck "DECISION dismissed" "DECISION dismissed" "$(grep '^DECISION' "$W/out.txt")"

echo "== T8 timeout: session closed, nothing left behind =="
: > "$W/stub.log"; pa STUB_MODE=hang -- "$ROOT/doc.md" --timeout-secs 2 --result-file "$W/r-timeout.json"
ck "exit 6" 6 "$RC"; ck "DECISION none" "DECISION none" "$(grep '^DECISION' "$W/out.txt")"
SPID=$(awk '{print $1}' "$W/stub.log" | tail -1); sleep 0.5
ck "plannotator stand-in was stopped" no "$(alive "$SPID")"; ck "lock removed" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"
ck "result file says timeout" timeout "$(jq -r .reason "$W/r-timeout.json")"

echo "== T9 an interrupted caller: TERM, with a plannotator that ignores TERM =="
: > "$W/stub.log"
env -u TMUX -u TMUX_PANE TMUX_TMPDIR="$TD" PH_ANNOTATE_PLANNOTATOR="$W/stub-pn" PH_ANNOTATE_STATE_DIR="$ST" PH_ANNOTATE_ROOTS="$ROOT" STUB_LOG="$W/stub.log" STUB_MODE=ignoreterm "$PA" "$ROOT/doc.md" >"$W/out.txt" 2>"$W/err.txt" &
PAP=$!
for _ in $(seq 1 30); do grep -q '^URL' "$W/out.txt" && break; sleep 0.3; done
SPID=$(awk '{print $1}' "$W/stub.log" | tail -1); ck "stand-in is running" yes "$(alive "$SPID")"
kill -TERM $PAP; for _ in $(seq 1 30); do kill -0 $PAP 2>/dev/null || break; sleep 0.3; done
ck "ph-annotate exited after TERM" no "$(alive $PAP)"
ck "the TERM-ignoring stand-in was force-stopped" no "$(alive "$SPID")"
ck "lock removed" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== T10-T12 start failures and stale locks =="
pa STUB_MODE=fail -- "$ROOT/doc.md"
ck "plannotator fails to start: exit 5" 5 "$RC"; ck "error text is passed on" 1 "$(grep -c 'HTTPS not enabled' "$W/err.txt")"
ck "lock released after the failure" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"
: > "$W/stub.log"; pa STUB_MODE=badport -- "$ROOT/doc.md"
ck "wrong port in the URL: exit 5" 5 "$RC"; SPID=$(awk '{print $1}' "$W/stub.log" | tail -1); sleep 0.5
ck "and the stand-in was stopped" no "$(alive "$SPID")"
mkdir -p "$ST/lock-8444"; echo 99999 > "$ST/lock-8444/pid"
pa STUB_MODE=annotate STUB_DELAY=0 -- "$ROOT/doc.md"
ck "stale lock (dead pid) is taken over: 8444" "PORT 8444" "$(grep '^PORT' "$W/out.txt")"

echo "== T13 two at once get different ports =="
rm -f "$W/o1.txt" "$W/o2.txt"
env -u TMUX -u TMUX_PANE PH_ANNOTATE_PLANNOTATOR="$W/stub-pn" PH_ANNOTATE_STATE_DIR="$ST" PH_ANNOTATE_ROOTS="$ROOT" STUB_MODE=annotate STUB_DELAY=3 "$PA" "$ROOT/doc.md" >"$W/o1.txt" 2>/dev/null &
env -u TMUX -u TMUX_PANE PH_ANNOTATE_PLANNOTATOR="$W/stub-pn" PH_ANNOTATE_STATE_DIR="$ST" PH_ANNOTATE_ROOTS="$ROOT" STUB_MODE=annotate STUB_DELAY=3 "$PA" "$ROOT/page.html" >"$W/o2.txt" 2>/dev/null &
wait
P1=$(grep '^PORT' "$W/o1.txt"); P2=$(grep '^PORT' "$W/o2.txt")
ck "both got a port" 2 "$(printf '%s\n%s\n' "$P1" "$P2" | grep -c '^PORT')"
ck "and they are different" 1 "$([ "$P1" != "$P2" ] && echo 1 || echo 0)"

echo "== T14 orphaned tailnet publication from an interrupted run is swept (real tailscale serve) =="
tailscale serve --bg --https=8447 http://127.0.0.1:9 >/dev/null 2>&1
ck "orphan mapping created on 8447" 1 "$(tailscale serve status 2>&1 | grep -c ':8447')"
pa STUB_MODE=annotate STUB_DELAY=0 PH_ANNOTATE_PORT_LO=8447 PH_ANNOTATE_PORT_HI=8447 -- "$ROOT/doc.md"
ck "ph-annotate used 8447" "PORT 8447" "$(grep '^PORT' "$W/out.txt")"
ck "the orphan mapping is gone afterwards" 0 "$(tailscale serve status 2>&1 | grep -c ':8447')"

echo "== T16 review mode (stand-in plannotator): the helper builds the patch itself =="
RP="$W/repos"; mkdir -p "$RP"; export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
R1="$RP/r1"; mkdir "$R1"; ( cd "$R1" && git init -q -b main && printf 'one\ntwo\n' > a.txt && printf 'ignored.log\n' > .gitignore && git add -A && git commit -qm base && git checkout -qb feature \
  && printf 'one\ntwo CHANGED\nthree\n' > a.txt && git commit -qam "branch change" && printf 'later EDIT\n' >> a.txt \
  && printf 'brand new file\n' > new.txt && printf 'SECRET=1\n' > .env && printf 'k\n' > server.pem && printf 'x\n' > ignored.log && printf 'PK\000\001binary\000data\n' > bin.dat )   # contains NUL bytes: binary in every locale (random bytes were not)
R2="$RP/clean"; mkdir "$R2"; ( cd "$R2" && git init -q -b main && printf 'x\n' > f.txt && git add -A && git commit -qm base )
R3="$RP/nocommits"; mkdir "$R3"; ( cd "$R3" && git init -q -b main )
OUTREPO="$W/outsiderepo"; mkdir "$OUTREPO"; ( cd "$OUTREPO" && git init -q -b main && printf 'x\n' > f.txt && git add -A && git commit -qm base && printf 'y\n' >> f.txt )
mkdir -p "$R1/sub"; printf 'in a subfolder\n' > "$R1/sub/s.txt"
rv() { PATCH_COPY="$W/patch-copy.txt"; : > "$PATCH_COPY"; pa PH_ANNOTATE_REPO_ROOTS="$RP" STUB_PATCH_COPY="$PATCH_COPY" STUB_MODE=annotate STUB_DELAY=0 "$@"; }
: > "$W/stub.log"; rv -- --review "$R1" --diff-type uncommitted
ck "uncommitted: finishes" 0 "$RC"; ck "plannotator was started with its AI features disabled" 1 "$(grep -c 'AI=disabled' "$W/stub.log")"
ck "the stand-in was started in review mode with a patch file" 1 "$(grep -c 'ARGS=review --patch-file' "$W/stub.log")"
ck "the page title names the repo and what is shown" 1 "$(grep -c 'TITLE=r1 · Uncommitted changes' "$W/stub.log")"
P="$(cat "$W/patch-copy.txt")"
ck "uncommitted patch has the working-tree edit" 1 "$(grep -c '^+later EDIT' <<<"$P")"
ck "uncommitted patch has the new untracked file" 1 "$(grep -c '^+brand new file' <<<"$P")"
ck "uncommitted patch has the new file in a subfolder" 1 "$(grep -c '^+in a subfolder' <<<"$P")"
ck "uncommitted patch does NOT have the already-committed branch change" 0 "$(grep -c '^+two CHANGED' <<<"$P")"
ck "a secret-looking untracked file (.env) is NOT included" 0 "$(grep -c 'SECRET=1' <<<"$P")"
ck "a .pem file is NOT included" 0 "$(grep -c 'server.pem' <<<"$P")"
ck "a git-ignored file is NOT included" 0 "$(grep -c 'ignored.log' <<<"$P")"
ck "a binary untracked file is NOT included" 0 "$(grep -c 'bin.dat' <<<"$P")"
rv -- --review "$R1" --diff-type branch
P="$(cat "$W/patch-copy.txt")"
ck "branch: the committed branch change is included" 1 "$(grep -c '^+two CHANGED' <<<"$P")"
ck "branch: plus the uncommitted edit" 1 "$(grep -c '^+later EDIT' <<<"$P")"
ck "branch: the page title says what it is compared with" 1 "$(grep -c 'TITLE=r1 · This branch vs main' "$W/stub.log")"
rv -- --review "$R1" --diff-type last-commit
P="$(cat "$W/patch-copy.txt")"
ck "last-commit: shows the last commit" 1 "$(grep -c '^+two CHANGED' <<<"$P")"
ck "last-commit: does NOT show the uncommitted edit" 0 "$(grep -c 'later EDIT' <<<"$P")"
rv -- --review "$R1" --diff-type branch --result-file "$W/rv-result.json"
ck "the result file says it is a review and keeps the label" "review|branch" "$(jq -r '"\(.kind)|\(.diff_type)"' "$W/rv-result.json")"
rv -- --review "$R2" --diff-type uncommitted; ck "an empty diff is refused (3)" 3 "$RC"; ck "with a clear reason" 1 "$(grep -c 'no changes to review' "$W/err.txt")"
rv -- --review "$R3" --diff-type uncommitted; ck "a repo with no commits is refused (3)" 3 "$RC"
rv -- --review "$OUTREPO" --diff-type uncommitted; ck "a repo outside the allowed repo roots is refused (3)" 3 "$RC"
rv -- --review "$R1/sub" --diff-type uncommitted; ck "a sub-folder of a repo (not its top folder) is refused (3)" 3 "$RC"
rv -- --review "$W" --diff-type uncommitted; ck "a folder that is not a git repo is refused (3)" 3 "$RC"
rv -- --review "$R1" --diff-type "uncommitted;rm -rf /"; ck "an unknown diff type is a usage error (2)" 2 "$RC"
rv -- --review "$R1" "$ROOT/doc.md"; ck "FILE and --review together are a usage error (2)" 2 "$RC"
rv -- --review "$R1" --diff-type branch; ck "(sanity) a normal review still works after the refusals" 0 "$RC"
ln -s "$OUTREPO" "$RP/linked"; rv -- --review "$RP/linked" --diff-type uncommitted; ck "a symlink to a repo outside the roots is refused (3)" 3 "$RC"
( cd "$R1" && cp a.txt "$W/a.txt.keep" && head -c 3500000 /dev/zero | tr '\0' 'z' >> a.txt && printf '\n' >> a.txt ); rv -- --review "$R1" --diff-type uncommitted
ck "a diff over 3 MB is refused (3)" 3 "$RC"; ck "and says to review it on the Mac" 1 "$(grep -c 'review it on the Mac' "$W/err.txt")"; cp "$W/a.txt.keep" "$R1/a.txt"
( cd "$R1" && head -c 300000 /dev/zero | tr '\0' 'q' > big-untracked.txt ); rv -- --review "$R1" --diff-type uncommitted
ck "an untracked file over 200 KB is skipped, not fatal" "0 0" "$RC $(grep -c 'qqqqqqqq' "$W/patch-copy.txt")"; rm "$R1/big-untracked.txt"
# a repository that tries to run programs through its own configuration must not be able to
( cd "$R1" && git config core.fsmonitor "touch $W/FSMONITOR-RAN" && git config diff.external "touch $W/EXTDIFF-RAN" && git config core.hooksPath "$W/hooks" && mkdir -p "$W/hooks" && printf '#!/bin/sh\ntouch %s/HOOK-RAN\n' "$W" > "$W/hooks/post-checkout" && chmod +x "$W/hooks/post-checkout" )
rv -- --review "$R1" --diff-type uncommitted; ck "a repo configured to run fsmonitor / an external diff program: still works" 0 "$RC"
ck "its fsmonitor program was NOT run" "no" "$([ -e "$W/FSMONITOR-RAN" ] && echo yes || echo no)"
ck "its external diff program was NOT run" "no" "$([ -e "$W/EXTDIFF-RAN" ] && echo yes || echo no)"
( cd "$R1" && git config --unset core.fsmonitor && git config --unset diff.external && git config --unset core.hooksPath )
ck "no port lock left behind" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"

echo "== T15 REAL plannotator: start, publish, then interrupt =="
HUB443_BEFORE=$(tailscale serve status 2>&1 | grep -E '^https://[^:]+ \(tailnet only\)|^\|-- / proxy' | head -2)
cp "$SP/phone-test.md" "$ROOT/phone-test.md" 2>/dev/null || echo "# real smoke test" > "$ROOT/phone-test.md"
env -u TMUX -u TMUX_PANE PH_ANNOTATE_STATE_DIR="$ST" PH_ANNOTATE_ROOTS="$ROOT" "$PA" "$ROOT/phone-test.md" >"$W/real.out" 2>"$W/real.err" &
RPID=$!
for _ in $(seq 1 60); do grep -q '^URL' "$W/real.out" && break; sleep 0.5; done
RPORT=$(awk '/^PORT/ {print $2}' "$W/real.out")
ck "real plannotator is on a port in 8444-8450" 1 "$([ -n "$RPORT" ] && [ "$RPORT" -ge 8444 ] && [ "$RPORT" -le 8450 ] && echo 1 || echo 0)"
ck "URL uses this machine's tailnet name" 1 "$(grep -c "^URL https://${TSHOST_RE}:" "$W/real.out")"
ck "tailscale serve shows the mapping (https line, tailnet only)" 1 "$(tailscale serve status 2>&1 | grep -c "^https://$TSHOST:$RPORT (tailnet only)")"
ck "page answers on the loopback port" 200 "$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$RPORT/")"
kill -TERM $RPID; for _ in $(seq 1 30); do kill -0 $RPID 2>/dev/null || break; sleep 0.3; done
sleep 1
ck "ph-annotate exited" no "$(alive $RPID)"
ck "NO publication left on ITS port" 0 "$(tailscale serve status 2>&1 | grep -c "^https://$TSHOST:$RPORT ")"
ck "the 443 mapping (the hub) was never touched" "$HUB443_BEFORE" "$(tailscale serve status 2>&1 | grep -E '^https://[^:]+ \(tailnet only\)|^\|-- / proxy' | head -2)"
ck "port is free again" 0 "$(lsof -nP -iTCP:"$RPORT" -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')"
ck "lock removed" 0 "$(ls -d "$ST"/lock-* 2>/dev/null | wc -l | tr -d ' ')"
ck "no plannotator process left" 0 "$(pgrep -f 'plannotator annotate' | wc -l | tr -d ' ')"

T kill-server 2>/dev/null; rm -rf "$TD"
echo; echo "FINAL tailscale serve state: $(tailscale serve status 2>&1 | grep -c '^https://') mapping(s) left (expected: 1, the hub on 443 if it is published)"
echo "PASS=$pass FAIL=$fail"
