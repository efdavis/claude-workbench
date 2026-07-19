#!/usr/bin/env bash
# Contract test for handler.sh — the dashboard's one external action handler.
# PATH-shims cmux/tmux (zero real side effects, records their argv) and asserts each
# (key)->action recipe plus every fail-visible path. Exits non-zero on any failure.
# Run: ./handler.test.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
H="$here/handler.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

# --- shim bin: fake cmux + tmux that log their argv, one arg per line ---
BIN="$TMP/bin"; mkdir -p "$BIN"
CMUX_LOG="$TMP/cmux.log"; TMUX_LOG="$TMP/tmux.log"
cat > "$BIN/cmux" <<EOF
#!/usr/bin/env bash
{ for a in "\$@"; do printf '%s\n' "\$a"; done; echo '---'; } >> "$CMUX_LOG"
# new-surface must print a parseable surface token for the handler to proceed
case "\$1" in new-surface) echo "OK surface:9 pane:1 workspace:1" ;; esac
# MOCK_SEND_FAIL makes 'cmux send' fail so we can exercise the fail-visible send guard.
if [ -n "\${MOCK_SEND_FAIL:-}" ] && [ "\$1" = send ]; then exit 1; fi
exit 0
EOF
cat > "$BIN/tmux" <<EOF
#!/usr/bin/env bash
{ for a in "\$@"; do printf '%s\n' "\$a"; done; echo '---'; } >> "$TMUX_LOG"
# has-session succeeds by default (a lane exists); MOCK_NO_LANE makes it fail so we can
# exercise the "live in cmux, no lane" branch (the can't-find-session guard).
# has-session is not \$1 (the call is 'tmux -L <sock> has-session -t <name>'), so scan args.
if [ -n "\${MOCK_NO_LANE:-}" ]; then
  for _a in "\$@"; do [ "\$_a" = "has-session" ] && exit 1; done
fi
exit 0
EOF
chmod +x "$BIN/cmux" "$BIN/tmux"
export PATH="$BIN:$PATH"

reset_logs() { : > "$CMUX_LOG"; : > "$TMUX_LOG"; }
cmux_has() { grep -qxF "$1" "$CMUX_LOG"; }   # exact line (one argv token) present?
tmux_has() { grep -qxF "$1" "$TMUX_LOG"; }

# 1. enter on a LIVE row -> attach recipe (tmux -X cancel on <issue>, cmux surface + send + rename)
reset_logs
out="$("$H" runs enter PROJ-76 implementing live "" /tmp/wt)"
tmux_has "send-keys" && tmux_has "PROJ-76" && tmux_has "cancel" && pass "attach: tmux -X cancel on <issue>" || fail "attach: -X cancel"
cmux_has "new-surface" && cmux_has "terminal" && pass "attach: cmux new-surface terminal" || fail "attach: new-surface"
cmux_has "tmux -L agent-lanes attach -t PROJ-76\\n" && pass "attach: send targets <issue> on lane socket + Enter" || fail "attach: send recipe"
cmux_has "rename-tab" && cmux_has "🎻 PROJ-76" && pass "attach: rename-tab to soloist glyph" || fail "attach: rename-tab"
case "$out" in *"attach → PROJ-76"*) pass "attach: status line" ;; *) fail "attach: status line (got: $out)" ;; esac

# 1b. enter on a GHOST row also attaches (the tmux session still exists)
reset_logs
out="$("$H" runs enter PROJ-76 implementing ghost "" /tmp/wt)"
{ cmux_has "new-surface" && tmux_has "cancel"; } && pass "ghost row attaches" || fail "ghost attach"

# 1c. AGENT_DASHBOARD_TMUX_SOCKET override -> attach targets the custom socket
reset_logs
out="$(AGENT_DASHBOARD_TMUX_SOCKET=alt "$H" runs enter PROJ-76 implementing live "" /tmp/wt)"
{ tmux_has "alt" && cmux_has "tmux -L alt attach -t PROJ-76\\n"; } \
  && pass "custom tmux socket honored on attach" || fail "custom socket (got: $out)"

# 1d. issue key with shell metacharacters is sanitized before it reaches the command string
reset_logs
out="$("$H" runs enter 'PROJ-76;rm -rf x' implementing live "" /tmp/wt)"
{ ! cmux_has "tmux -L agent-lanes attach -t PROJ-76;rm -rf x\\n" && cmux_has "new-surface"; } \
  && pass "issue key sanitized (no metachar injection into the sent command)" || fail "issue sanitize (got: $out)"

# 1e. a row that renders `live` via its cmux surface (a hands-on run) but has NO lane must
#     NOT attempt a doomed `tmux attach` ("can't find session: <issue>") — it JUMPS to that
#     surface's own cmux tab instead (surface.focus + flash), using the cmux_surface 8th arg.
reset_logs
out="$(MOCK_NO_LANE=1 "$H" runs enter PROJ-12 waiting live "" "" 1E51549E-1D5C-4A64-AE65-9EEB918F46D9)"
{ ! cmux_has "tmux -L agent-lanes attach -t PROJ-12\\n" && cmux_has "surface.focus" \
    && case "$out" in *"jumped to PROJ-12"*) true ;; *) false ;; esac; } \
  && pass "live-but-no-lane row: jumps to its cmux tab, no doomed attach" || fail "no-lane jump (got: $out)"

# 1e-2. same branch but with NO cmux_surface recorded -> clear message, no focus, no attach.
reset_logs
out="$(MOCK_NO_LANE=1 "$H" runs enter PROJ-12 waiting live "" "" "")"
{ ! cmux_has "surface.focus" && ! cmux_has "tmux -L agent-lanes attach -t PROJ-12\\n" \
    && case "$out" in *"no cmux surface recorded"*) true ;; *) false ;; esac; } \
  && pass "live-but-no-lane, no surface: clear message, no focus/attach" || fail "no-surface guard (got: $out)"

# 1f. a failed `cmux send` must be fail-visible: the attach reports 'cmux: send failed' and
#     NOT a false 'attach → <issue>' success (the send exit code is checked).
reset_logs
out="$(MOCK_SEND_FAIL=1 "$H" runs enter PROJ-76 implementing live "" /tmp/wt)"
{ case "$out" in *"cmux: send failed"*) true ;; *) false ;; esac \
  && case "$out" in *"attach → PROJ-76"*) false ;; *) true ;; esac; } \
  && pass "cmux send failure -> fail-visible, no false attach success" || fail "send-fail guard (got: $out)"

# 2. enter on a DEAD/terminal row -> replay the newest transcript in a pager tab
reset_logs
export HOME="$TMP/home"
wt="/tmp/acme-worktrees/proj-76"
slug="$(printf '%s' "$wt" | tr -c 'A-Za-z0-9-' '-')"
projdir="$HOME/.claude/projects/$slug"; mkdir -p "$projdir"
touch -t 202601010000 "$projdir/old.jsonl"; sleep 0.02; : > "$projdir/newest.jsonl"
out="$("$H" runs enter PROJ-1 merged - "" "$wt")"
cmux_has "python3 $here/transcript.py $projdir/newest.jsonl | less -R\\n" \
  && pass "recording: renders NEWEST jsonl via transcript.py in a pager" || fail "recording: newest jsonl"
case "$out" in *"replay "*newest.jsonl) pass "recording: status line" ;; *) fail "recording: status (got: $out)" ;; esac

# 3. enter dead, no worktree recorded -> visible message, no cmux call
reset_logs
out="$("$H" runs enter PROJ-1 done - "" "")"
{ [ ! -s "$CMUX_LOG" ] && case "$out" in *"no worktree recorded"*) true ;; *) false ;; esac; } \
  && pass "recording: empty worktree -> visible msg, no cmux" || fail "recording: empty worktree (got: $out)"

# 4. enter dead, worktree with no transcript dir -> visible message, no pager
reset_logs
out="$("$H" runs enter PROJ-1 done - "" /tmp/does-not-exist-xyz)"
case "$out" in *"no transcript under"*) pass "recording: no transcript -> visible msg" ;; *) fail "recording: no transcript (got: $out)" ;; esac

# 5. p with a pr + a PR url base -> cmux browser tab at the PR url
reset_logs
out="$(AGENT_DASHBOARD_PR_URL_BASE=https://github.com/acme/repo/pull "$H" runs p PROJ-76 implementing live 51 /tmp/wt)"
{ cmux_has "browser" && cmux_has "https://github.com/acme/repo/pull/51"; } \
  && pass "p: browser tab at PR url" || fail "p: PR url"
case "$out" in *"opened PR #51"*) pass "p: status line" ;; *) fail "p: status (got: $out)" ;; esac

# 5b. p with a pr but NO url base -> hint naming the var, no browser tab
reset_logs
out="$(env -u AGENT_DASHBOARD_PR_URL_BASE "$H" runs p PROJ-76 implementing live 51 /tmp/wt)"
{ ! cmux_has "browser" && case "$out" in *"set AGENT_DASHBOARD_PR_URL_BASE"*) true ;; *) false ;; esac; } \
  && pass "p: no url base -> hint, no tab" || fail "p: no url base (got: $out)"

# 6. p with no pr -> visible message, no browser tab
reset_logs
out="$(AGENT_DASHBOARD_PR_URL_BASE=https://github.com/acme/repo/pull "$H" runs p PROJ-76 implementing live "" /tmp/wt)"
{ ! cmux_has "browser" && case "$out" in *"no PR yet"*) true ;; *) false ;; esac; } \
  && pass "p: no pr -> visible msg, no tab" || fail "p: no pr (got: $out)"

# 7. t with an issue + an issue url base -> cmux browser tab at the issue url
reset_logs
out="$(AGENT_DASHBOARD_ISSUE_URL_BASE=https://tracker.example/browse "$H" runs t PROJ-76 implementing live "" /tmp/wt)"
{ cmux_has "browser" && cmux_has "https://tracker.example/browse/PROJ-76"; } \
  && pass "t: browser tab at issue url" || fail "t: issue url"
case "$out" in *"opened PROJ-76"*) pass "t: status line" ;; *) fail "t: status (got: $out)" ;; esac

# 7b. t with an issue but NO url base -> hint naming the var, no browser tab
reset_logs
out="$(env -u AGENT_DASHBOARD_ISSUE_URL_BASE "$H" runs t PROJ-76 implementing live "" /tmp/wt)"
{ ! cmux_has "browser" && case "$out" in *"set AGENT_DASHBOARD_ISSUE_URL_BASE"*) true ;; *) false ;; esac; } \
  && pass "t: no url base -> hint, no tab" || fail "t: no url base (got: $out)"

# 8. t with no issue -> visible message
reset_logs
out="$("$H" runs t "" implementing live "" /tmp/wt)"
case "$out" in *"no issue for this row"*) pass "t: no issue -> visible msg" ;; *) fail "t: no issue (got: $out)" ;; esac

# 9. unknown key -> visible message
out="$("$H" runs x PROJ-76 implementing live "" /tmp/wt)"
case "$out" in *"no action for key 'x'"*) pass "unknown key -> visible msg" ;; *) fail "unknown key (got: $out)" ;; esac

# 9b. r on live hands-on run (surface, no lane): focus + flash + close-surface
reset_logs
out="$(MOCK_NO_LANE=1 "$H" runs r PROJ-12 implementing live "" "" 1E51549E-1D5C-4A64-AE65-9EEB918F46D9)"
{ cmux_has "surface.focus" && cmux_has "close-surface" && cmux_has "1E51549E-1D5C-4A64-AE65-9EEB918F46D9" \
    && case "$out" in *"ended"*|*"closed cmux tab"*) true ;; *) false ;; esac; } \
  && pass "r live+surface: focus + close-surface" || fail "r live surface (got: $out)"

# 9c. r on live dispatch lane (tmux session exists): kill-session
reset_logs
out="$("$H" runs r PROJ-76 implementing live "" /tmp/wt "")"
{ tmux_has "kill-session" && tmux_has "PROJ-76" \
    && case "$out" in *"killed lane"*|*"ended"*) true ;; *) false ;; esac; } \
  && pass "r live+lane: kill-session" || fail "r live lane (got: $out)"

# 9d. r on stale/tombstone: no kill (dashboard only reaps the card) — must not close-surface
reset_logs
out="$("$H" runs r PROJ-1 done stale "" /tmp/wt 1E51549E-1D5C-4A64-AE65-9EEB918F46D9)"
{ ! cmux_has "close-surface" && ! tmux_has "kill-session" \
    && case "$out" in *"nothing live to end"*) true ;; *) false ;; esac; } \
  && pass "r stale: no kill, visible msg" || fail "r stale guard (got: $out)"

# 9e. r on ghost behaves like live (still focus+close when surface present)
reset_logs
out="$(MOCK_NO_LANE=1 "$H" runs r PROJ-12 implementing ghost "" "" 1E51549E-1D5C-4A64-AE65-9EEB918F46D9)"
{ cmux_has "close-surface" && case "$out" in *"ended"*|*"closed cmux tab"*) true ;; *) false ;; esac; } \
  && pass "r ghost+surface: close-surface" || fail "r ghost surface (got: $out)"

# 9f. r rejects a garbage cmux_surface (no close-surface; no JSON injection)
reset_logs
out="$(MOCK_NO_LANE=1 "$H" runs r PROJ-12 implementing live "" "" 'not-a-uuid";rm -rf /')"
{ ! cmux_has "close-surface" && ! cmux_has "surface.focus" \
    && case "$out" in *"bad cmux_surface"*) true ;; *) false ;; esac; } \
  && pass "r bad surface id: refuse close" || fail "r bad surface (got: $out)"

# 10. cmux ABSENT (tmux present, so the attach path IS reached) -> cmux install hint
NOCMUX="$TMP/nocmux"; mkdir -p "$NOCMUX"; cp "$BIN/tmux" "$NOCMUX/tmux"
out="$(PATH="$NOCMUX:/usr/bin:/bin" "$H" runs enter PROJ-76 implementing live "" /tmp/wt)"
case "$out" in *"install cmux (brew install cmux)"*) pass "cmux absent -> install hint" ;; *) fail "cmux absent (got: $out)" ;; esac

# 11. always exit 0 (fire-and-forget contract) — even on the failure paths
"$H" runs p PROJ-76 implementing live "" "" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && pass "always exit 0" || fail "exit code (rc=$rc)"

echo
if [ "$fails" -eq 0 ]; then echo "all handler tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
