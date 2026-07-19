#!/usr/bin/env bash
# Contract test for transcript.py — the dashboard replay renderer. Feeds a synthetic CC
# .jsonl (user/assistant turns + the hook/mode/title metadata noise the raw file is full
# of) and asserts the render keeps the turns and drops the noise. Pure stdlib python3.
# Run: ./transcript.test.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
T="$here/transcript.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

jsonl="$TMP/t.jsonl"
cat > "$jsonl" <<'EOF'
{"type":"last-prompt","leafUuid":"x","sessionId":"s"}
{"type":"mode","mode":"normal","sessionId":"s"}
{"type":"attachment","attachment":{"type":"hook_success"},"sessionId":"s"}
{"type":"user","timestamp":"2026-07-16T19:32:45.000Z","message":{"role":"user","content":"please add the widget"}}
{"type":"assistant","timestamp":"2026-07-16T19:32:53.000Z","message":{"role":"assistant","content":[{"type":"thinking","thinking":"long internal reasoning here\nsecond line"},{"type":"text","text":"On it — running the build."},{"type":"tool_use","name":"Bash","input":{"command":"pnpm build"}}]}}
{"type":"user","timestamp":"2026-07-16T19:32:57.000Z","message":{"role":"user","content":[{"type":"tool_result","content":"build ok\nline2\nline3"}]}}
{"type":"ai-title","title":"some-noise-title"}
EOF

# strip ANSI for stable substring assertions
out="$(python3 "$T" "$jsonl" | sed 's/\x1b\[[0-9;]*m//g')"

case "$out" in *"please add the widget"*) pass "keeps user text" ;; *) fail "user text" ;; esac
case "$out" in *"On it — running the build."*) pass "keeps assistant text" ;; *) fail "assistant text" ;; esac
case "$out" in *"⏵ Bash"*"pnpm build"*) pass "shows tool call + arg hint" ;; *) fail "tool call" ;; esac
case "$out" in *"thinking:"*) pass "compact thinking marker (not the full block)" ;; *) fail "thinking marker" ;; esac
case "$out" in *"second line"*) fail "thinking block dumped in full (should be one-line marker)" ;; *) pass "thinking body not dumped" ;; esac
case "$out" in *"build ok"*) pass "shows tool result" ;; *) fail "tool result" ;; esac
case "$out" in *hook_success*|*some-noise-title*|*last-prompt*) fail "metadata noise leaked into output" ;; *) pass "drops hook/mode/title/metadata noise" ;; esac
case "$out" in *USER*ASSISTANT*) pass "role headers present" ;; *) fail "role headers" ;; esac

# Codex rollout shape: response_item/message with input_text + output_text blocks.
cat > "$TMP/codex.jsonl" <<'EOF'
{"timestamp":"2026-07-19T00:25:02.072Z","type":"response_item","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"hidden developer instructions"}]}}
{"timestamp":"2026-07-19T00:25:02.073Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"monitor this exact Codex seat"}]}}
{"timestamp":"2026-07-19T00:25:09.867Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Attached to the persistent lane."}]}}
EOF
co="$(python3 "$T" "$TMP/codex.jsonl" | sed 's/\x1b\[[0-9;]*m//g')"
case "$co" in *"monitor this exact Codex seat"*"Attached to the persistent lane."*) \
  pass "renders exact Codex rollout turns" ;; *) fail "Codex rollout rendering" ;; esac
case "$co" in *"hidden developer instructions"*) fail "Codex developer noise leaked" ;; *) \
  pass "drops Codex developer/system noise" ;; esac

# no-turns transcript -> friendly message, exit 0
echo '{"type":"mode","mode":"normal"}' > "$TMP/empty.jsonl"
o2="$(python3 "$T" "$TMP/empty.jsonl")"; rc=$?
{ [ "$rc" -eq 0 ] && case "$o2" in *"no user/assistant turns"*) true ;; *) false ;; esac; } \
  && pass "no-turns transcript -> friendly message, exit 0" || fail "no-turns transcript (rc=$rc)"

# a half-written / non-JSON line is skipped, never fatal
printf '%s\n' '{ this is not json' '{"type":"user","message":{"role":"user","content":"survived"}}' > "$TMP/bad.jsonl"
o3="$(python3 "$T" "$TMP/bad.jsonl" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g')"; rc=$?
{ [ "$rc" -eq 0 ] && case "$o3" in *survived*) true ;; *) false ;; esac; } \
  && pass "non-JSON line skipped, rest rendered" || fail "bad-line tolerance (rc=$rc)"

# missing file -> non-zero, clean stderr message (no traceback)
python3 "$T" "$TMP/does-not-exist.jsonl" >/dev/null 2>"$TMP/err"; rc=$?
{ [ "$rc" -ne 0 ] && grep -q "cannot open" "$TMP/err"; } && pass "missing file -> clean error, non-zero" || fail "missing file (rc=$rc)"

echo
if [ "$fails" -eq 0 ]; then echo "all transcript tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
