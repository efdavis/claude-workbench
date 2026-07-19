#!/usr/bin/env bash
set -u
here="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; pid=""
trap '[ -n "$pid" ] && kill "$pid" 2>/dev/null || true; rm -rf "$TMP"' EXIT
fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

printf '%s\n' \
  '{"type":"thread.started","thread_id":"abc"}' \
  '{"type":"item.completed","item":{"type":"agent_message","text":"progress now"}}' \
  '{"type":"item.started","item":{"type":"command_execution","command":"printf hello","status":"in_progress"}}' \
  '{"type":"item.completed","item":{"type":"command_execution","command":"printf hello","aggregated_output":"hello","exit_code":0,"status":"completed"}}' \
  '{"type":"item.completed","item":{"type":"command_execution","command":"curl -H Authorization:supersecret","aggregated_output":"data:image/png;base64,AAAA and token=abc123","exit_code":0,"status":"completed"}}' \
  '{"type":"item.completed","item":{"type":"file_change","changes":[{"path":"/tmp/a.ts","kind":"update"}],"status":"completed"}}' \
  '{"type":"response_item","payload":{"type":"message","role":"developer","content":"hidden"}}' \
  > "$TMP/run-1.jsonl"
ln -s run-1.jsonl "$TMP/current.jsonl"
out="$(NO_COLOR=1 python3 "$here/codex-stream.py" "$TMP/current.jsonl")"
case "$out" in *"progress now"*"printf hello"*"hello"*"/tmp/a.ts"*) pass "renders exec events" ;; *) fail "exec rendering" ;; esac
case "$out" in *hidden*) fail "hidden record leaked" ;; *) pass "drops unknown/hidden records" ;; esac
case "$out" in *supersecret*|*abc123*|*base64,AAAA*) fail "sensitive payload leaked" ;; *) pass "redacts auth and image payloads" ;; esac
case "$out" in *$'\033['*) fail "NO_COLOR emitted ANSI" ;; *) pass "honors NO_COLOR" ;; esac

NO_COLOR=1 python3 "$here/codex-stream.py" --follow "$TMP/current.jsonl" > "$TMP/follow.out" & pid=$!
sleep 0.25
printf '%s' '{"type":"item.completed","item":{"type":"agent_message","text":"partial' >> "$TMP/run-1.jsonl"
sleep 0.2
printf '%s\n' ' becomes whole"}}' >> "$TMP/run-1.jsonl"
sleep 0.25
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"after rotation"}}' > "$TMP/run-2.jsonl"
ln -sfn run-2.jsonl "$TMP/.current.tmp"; mv -f "$TMP/.current.tmp" "$TMP/current.jsonl"
sleep 0.35
kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; pid=""
{ grep -q "partial becomes whole" "$TMP/follow.out" && grep -q "after rotation" "$TMP/follow.out"; } \
  && pass "follows partial completion and symlink rotation" || fail "follow/rotation"

echo
if [ "$fails" -eq 0 ]; then echo "all Codex stream tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
