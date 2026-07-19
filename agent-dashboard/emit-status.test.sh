#!/usr/bin/env bash
# Contract test for emit-status.sh. Asserts the atomic-write + best-effort behavior
# the dashboard wiring relies on. Exits non-zero if any assertion fails.
# Run: ./emit-status.test.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
E="$here/emit-status.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

# jq is a TEST-ONLY dependency (assertions); the emitter itself needs only python3
command -v jq >/dev/null 2>&1 || { echo "jq required to run this test"; exit 2; }
export AGENT_DASHBOARD_STATE_DIR="$TMP/state"

# 1. happy path writes valid JSON with the required + passed fields
"$E" --session s1 --role worker --state implementing --ticket PROJ-23 --note hi
f="$AGENT_DASHBOARD_STATE_DIR/s1.json"
{ [ -f "$f" ] && jq -e . "$f" >/dev/null 2>&1; } && pass "happy path -> valid JSON" || fail "happy path"
[ "$(jq -r '.state' "$f" 2>/dev/null)" = "implementing" ] && pass "state field" || fail "state field"
[ "$(jq -r '.ticket' "$f" 2>/dev/null)" = "PROJ-23" ] && pass "ticket field" || fail "ticket field"
jq -e 'has("iso_timestamp") and has("epoch")' "$f" >/dev/null 2>&1 && pass "timestamps present" || fail "timestamps"

# 2. optional fields absent unless passed; present when passed
jq -e 'has("pr_number") | not' "$f" >/dev/null 2>&1 && pass "pr omitted when not passed" || fail "pr omission"
"$E" --session s2 --role finisher --state pr-open --pr 51 --worktree /tmp/wt
jq -e '.pr_number == "51" and .worktree_path == "/tmp/wt"' "$AGENT_DASHBOARD_STATE_DIR/s2.json" >/dev/null 2>&1 \
  && pass "optional fields present when passed" || fail "optional fields"

"$E" --session s2ids --role worker --state implementing \
  --tmux-session ember-codex-1 --codex-session-id 01234567-89ab-cdef-0123-456789abcdef \
  --activity-stream-path '/tmp/seat one/current.jsonl'
jq -e '.tmux_session == "ember-codex-1" and .codex_session_id == "01234567-89ab-cdef-0123-456789abcdef" and .activity_stream_path == "/tmp/seat one/current.jsonl"' \
  "$AGENT_DASHBOARD_STATE_DIR/s2ids.json" >/dev/null 2>&1 \
  && pass "tmux/Codex identity and activity stream fields present" || fail "tmux/Codex/activity fields"

# 2b. cmux_surface: captured from $CMUX_SURFACE_ID, absent when unset
CMUX_SURFACE_ID="AAAAAAAA-TEST-UUID" "$E" --session s2b --role worker --state started
[ "$(jq -r '.cmux_surface' "$AGENT_DASHBOARD_STATE_DIR/s2b.json" 2>/dev/null)" = "AAAAAAAA-TEST-UUID" ] \
  && pass "cmux_surface captured from env" || fail "cmux_surface capture"
env -u CMUX_SURFACE_ID "$E" --session s2c --role worker --state started
jq -e 'has("cmux_surface") | not' "$AGENT_DASHBOARD_STATE_DIR/s2c.json" >/dev/null 2>&1 \
  && pass "cmux_surface omitted when env unset" || fail "cmux_surface omission"

# 3. jq-safety: a note with quotes/$/backticks round-trips verbatim
"$E" --session s3 --role worker --state started --note 'has "quotes" $VAR `bt`'
[ "$(jq -r '.note' "$AGENT_DASHBOARD_STATE_DIR/s3.json" 2>/dev/null)" = 'has "quotes" $VAR `bt`' ] \
  && pass "jq-safe note round-trips" || fail "jq-safe note"

# 4. best-effort: missing required arg -> exit 0, no file written
"$E" --session s4 --role worker; rc=$?
{ [ "$rc" -eq 0 ] && [ ! -f "$AGENT_DASHBOARD_STATE_DIR/s4.json" ]; } && pass "missing --state: exit 0, no write" || fail "missing-arg best-effort (rc=$rc)"

# 5. --remove deletes the snapshot, exit 0
"$E" --remove --session s1; rc=$?
{ [ "$rc" -eq 0 ] && [ ! -f "$AGENT_DASHBOARD_STATE_DIR/s1.json" ]; } && pass "--remove deletes, exit 0" || fail "--remove"

# 6. best-effort: unwritable state dir -> exit 0 (never fail the caller)
AGENT_DASHBOARD_STATE_DIR=/dev/null/cannot "$E" --session s5 --role worker --state started; rc=$?
[ "$rc" -eq 0 ] && pass "unwritable state dir: exit 0" || fail "unwritable best-effort (rc=$rc)"

# 7. session sanitized into a safe filename (path separators + spaces -> _)
"$E" --session 'weird/../id x' --role worker --state started
ls "$AGENT_DASHBOARD_STATE_DIR"/weird_.._id_x.json >/dev/null 2>&1 && pass "session sanitized to safe filename" || fail "filename sanitization"

echo
if [ "$fails" -eq 0 ]; then echo "all emit-status tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
