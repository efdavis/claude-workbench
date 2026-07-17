#!/usr/bin/env bash
# Tests for dispatch.sh and jira_claim.py.
# Runs with ZERO Jira/tmux/git side effects: git + tmux are PATH-shimmed with fakes,
# and the Jira claim is stubbed via AGENT_DASHBOARD_CLAIM_CMD. Run: bash dispatch.test.sh
#
# The fakes are state-machine mocks so the same command (e.g. `tmux has-session`) can
# mean "no session yet" in preflight and "alive" post-spawn within one run.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
DISPATCH="$HERE/dispatch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()   { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  FAIL %s\n' "$1"; }
check_rc() { # desc expected actual
  if [ "$2" = "$3" ]; then ok "$1 (exit $3)"; else bad "$1: expected exit $2, got $3"; fi
}
grep_log() { grep -qF "$2" "$1" 2>/dev/null; }

# ---- shim + fake locations -------------------------------------------------
BIN="$TMP/bin"; mkdir -p "$BIN"
export FAKE_TOPLEVEL="$TMP/repo"
export FAKE_WT="$TMP/PROJ-75-wt"           # = $(dirname FAKE_TOPLEVEL)/PROJ-75-wt
export SESS_DIR="$TMP/tmux-sessions"
export GIT_LOG="$TMP/git.log"
export TMUX_LOG="$TMP/tmux.log"
export CLAIM_LOG="$TMP/claim.log"
export AGENT_DASHBOARD_STATE_DIR="$TMP/state"   # keep any emit off the real dashboard
mkdir -p "$FAKE_TOPLEVEL" "$SESS_DIR"

# ---- fake git --------------------------------------------------------------
cat > "$BIN/git" <<'FAKEGIT'
#!/usr/bin/env bash
# minimal fake git driven by GIT_* env; records mutating calls to $GIT_LOG
while [ $# -gt 0 ]; do case "$1" in -C) shift 2 ;; *) break ;; esac; done
sub="${1:-}"; shift || true
case "$sub" in
  rev-parse) echo "$FAKE_TOPLEVEL" ;;
  fetch)     exit 0 ;;
  worktree)
    wsub="${1:-}"; shift || true
    case "$wsub" in
      list)
        [ -n "${GIT_WT_REGISTERED:-}" ] && echo "worktree $FAKE_WT"
        exit 0 ;;
      add)
        echo "worktree-add $*" >> "$GIT_LOG"
        [ -n "${GIT_WT_ADD_FAIL:-}" ] && exit 1
        for a in "$@"; do
          case "$a" in --detach|-*|origin/*) ;; *) mkdir -p "$a"; break ;; esac
        done
        exit 0 ;;
      remove) echo "worktree-remove $*" >> "$GIT_LOG"; exit 0 ;;
      prune)  exit 0 ;;
      *)      exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
FAKEGIT
chmod +x "$BIN/git"

# ---- fake tmux -------------------------------------------------------------
cat > "$BIN/tmux" <<'FAKETMUX'
#!/usr/bin/env bash
# state-machine fake tmux; sessions are marker files under $SESS_DIR
while [ $# -gt 0 ]; do case "$1" in -L) shift 2 ;; *) break ;; esac; done
sub="${1:-}"; shift || true
name=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) name="$2"; shift 2 ;;
    -s) name="$2"; shift 2 ;;
    -c) shift 2 ;;
    -d) shift ;;
    *)  cmd="$1"; shift ;;
  esac
done
case "$sub" in
  has-session) [ -f "$SESS_DIR/$name" ] ;;
  new-session)
    echo "new-session name=$name cmd=$cmd" >> "$TMUX_LOG"
    [ -n "${TMUX_NEWSESSION_FAIL:-}" ] && exit 1
    [ -n "${TMUX_PANE_DIES:-}" ] && exit 0     # "spawned" but pane died: no marker
    : > "$SESS_DIR/$name"; exit 0 ;;
  kill-session)
    echo "kill-session name=$name" >> "$TMUX_LOG"; rm -f "$SESS_DIR/$name"; exit 0 ;;
  *) exit 0 ;;
esac
FAKETMUX
chmod +x "$BIN/tmux"

# ---- claim stub ------------------------------------------------------------
cat > "$BIN/claim-stub" <<'STUB'
#!/usr/bin/env bash
action=""; pa=""; ps=""
while [ $# -gt 0 ]; do
  case "$1" in
    --action) action="$2"; shift 2 ;;
    --prior-assignee) pa="$2"; shift 2 ;;
    --prior-status)   ps="$2"; shift 2 ;;
    --ticket) shift 2 ;;
    *) shift ;;
  esac
done
if [ "$action" = "rollback" ]; then
  echo "rollback pa=$pa ps=$ps" >> "$CLAIM_LOG"
  echo "stub: rolled back $pa / $ps"
  exit "${STUB_ROLLBACK_RC:-0}"
fi
echo "claim outcome=${STUB_OUTCOME:-claimed}" >> "$CLAIM_LOG"
if [ -z "${STUB_NO_PRIOR:-}" ]; then
  echo "PRIOR_ASSIGNEE=${STUB_PRIOR_ASSIGNEE:-NONE}"
  echo "PRIOR_STATUS=${STUB_PRIOR_STATUS:-To Do}"
fi
echo "OUTCOME=${STUB_OUTCOME:-claimed}"
exit "${STUB_CLAIM_RC:-0}"
STUB
chmod +x "$BIN/claim-stub"

export PATH="$BIN:$PATH"
export AGENT_DASHBOARD_CLAIM_CMD="$BIN/claim-stub"
export JIRA_API_TOKEN="fake-token"           # presence check only; claim is stubbed
AUTO_CMD='claude "/implement --auto PROJ-75"'
PLAIN_CMD='echo hello'

# reset per-case disk state + case env
reset_state() {
  rm -rf "$SESS_DIR" "$FAKE_WT" "$FAKE_TOPLEVEL/.plans"
  mkdir -p "$SESS_DIR" "$FAKE_TOPLEVEL/.plans"
  : > "$GIT_LOG"; : > "$TMUX_LOG"; : > "$CLAIM_LOG"
  unset GIT_WT_REGISTERED GIT_WT_ADD_FAIL TMUX_NEWSESSION_FAIL TMUX_PANE_DIES
  unset STUB_OUTCOME STUB_CLAIM_RC STUB_PRIOR_ASSIGNEE STUB_PRIOR_STATUS STUB_ROLLBACK_RC STUB_NO_PRIOR
}
seed_review() { printf '{}' > "$FAKE_TOPLEVEL/.plans/PROJ-75.review.json"; }
seed_plan()   { printf 'plan\n' > "$FAKE_TOPLEVEL/.plans/PROJ-75-x.md"; }

echo "== dispatch.sh =="

# 1. happy path (--auto, review present): claim -> worktree -> seed -> tmux, exit 0
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "happy --auto dispatch" 0 $?
grep_log "$TMUX_LOG" "new-session name=PROJ-75" && ok "tmux session spawned" || bad "tmux session spawned"
[ -f "$FAKE_WT/.plans/PROJ-75.review.json" ] && ok "review.json seeded into worktree" || bad "review seeded"
[ -f "$FAKE_WT/.plans/PROJ-75-x.md" ] && ok "plan seeded into worktree" || bad "plan seeded"
grep_log "$CLAIM_LOG" "rollback" && bad "happy: no rollback expected" || ok "happy: no rollback"

# 2. assigned to someone else -> refuse (exit 3), no worktree, no rollback
reset_state; seed_plan; seed_review
export STUB_OUTCOME=refused-other STUB_CLAIM_RC=3
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "refused-other" 3 $?
grep_log "$GIT_LOG" "worktree-add" && bad "refused: no worktree add" || ok "refused: no worktree add"
grep_log "$CLAIM_LOG" "rollback" && bad "refused: no rollback (winner untouched)" || ok "refused: no rollback"

# 3. lost race -> exit 5, and CRUCIALLY no rollback (never unassign the winner)
reset_state; seed_plan; seed_review
export STUB_OUTCOME=lost-race STUB_CLAIM_RC=5
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "lost-race" 5 $?
grep_log "$CLAIM_LOG" "rollback" && bad "lost-race: assignee NOT unassigned" || ok "lost-race: assignee NOT unassigned"

# 4. worktree path already exists -> refuse before claim
reset_state; mkdir -p "$FAKE_WT"
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "worktree-path-exists refuse" 1 $?
grep_log "$CLAIM_LOG" "claim outcome" && bad "worktree-exists: claim not reached" || ok "worktree-exists: claim not reached"

# 5. worktree already registered -> refuse before claim
reset_state
export GIT_WT_REGISTERED=1 STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "worktree-registered refuse" 1 $?
grep_log "$CLAIM_LOG" "claim outcome" && bad "registered: claim not reached" || ok "registered: claim not reached"

# 6. tmux session already exists -> refuse before claim
reset_state; : > "$SESS_DIR/PROJ-75"
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "tmux-session-exists refuse" 1 $?
grep_log "$CLAIM_LOG" "claim outcome" && bad "tmux-exists: claim not reached" || ok "tmux-exists: claim not reached"

# 7. dead pane -> kill session, remove worktree, rollback the claim (restore prior)
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0 STUB_PRIOR_ASSIGNEE=NONE STUB_PRIOR_STATUS="To Do" TMUX_PANE_DIES=1
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "dead-pane fail" 1 $?
grep_log "$TMUX_LOG" "kill-session name=PROJ-75" && ok "dead-pane: session killed" || bad "dead-pane: session killed"
grep_log "$GIT_LOG" "worktree-remove" && ok "dead-pane: worktree removed" || bad "dead-pane: worktree removed"
grep_log "$CLAIM_LOG" "rollback pa=NONE ps=To Do" && ok "dead-pane: rollback restored prior" || bad "dead-pane: rollback restored prior"

# 8. post-claim failure (worktree add fails) -> rollback with captured prior, no tmux
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0 STUB_PRIOR_ASSIGNEE="acc-123" STUB_PRIOR_STATUS="To Do" GIT_WT_ADD_FAIL=1
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "worktree-add-fail" 1 $?
grep_log "$CLAIM_LOG" "rollback pa=acc-123 ps=To Do" && ok "wt-add-fail: rollback with prior state" || bad "wt-add-fail: rollback with prior state"
grep_log "$TMUX_LOG" "new-session" && bad "wt-add-fail: tmux not spawned" || ok "wt-add-fail: tmux not spawned"

# 9. --auto with NO review.json to seed -> loud dispatch-time failure + rollback
reset_state; seed_plan     # deliberately no seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0 STUB_PRIOR_ASSIGNEE=NONE STUB_PRIOR_STATUS="To Do"
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "--auto missing review.json" 1 $?
grep_log "$CLAIM_LOG" "rollback" && ok "missing-review: rolled back the claim" || bad "missing-review: rolled back"
grep_log "$TMUX_LOG" "new-session" && bad "missing-review: no lane spawned" || ok "missing-review: no lane spawned"

# 9b. NON-auto with no review.json -> seed is best-effort, dispatch still succeeds
reset_state; seed_plan
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$PLAIN_CMD" >/dev/null 2>&1; check_rc "non-auto no-review ok" 0 $?

# 10. no In-Progress transition -> fail closed (exit 4), no worktree, no rollback
reset_state; seed_plan; seed_review
export STUB_OUTCOME=no-transition STUB_CLAIM_RC=4
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "no-transition fail-closed" 4 $?
grep_log "$GIT_LOG" "worktree-add" && bad "no-transition: no worktree" || ok "no-transition: no worktree"

# 11. missing JIRA_API_TOKEN -> fail closed before claim
reset_state; seed_plan; seed_review
( unset JIRA_API_TOKEN; export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
  "$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1 ); check_rc "missing token fail-closed" 2 $?
grep_log "$CLAIM_LOG" "claim outcome" && bad "no-token: claim not reached" || ok "no-token: claim not reached"

# 12. assign-only (already In Progress, unassigned) treated as owned -> dispatch ok
reset_state
export STUB_OUTCOME=assign-only STUB_CLAIM_RC=0
"$DISPATCH" PROJ-75 "$PLAIN_CMD" >/dev/null 2>&1; check_rc "assign-only dispatch" 0 $?

# 13. bad issue id -> usage error
reset_state
"$DISPATCH" not-a-ticket "$PLAIN_CMD" >/dev/null 2>&1; check_rc "bad issue id" 2 $?

# 14. missing PIPELINE_CMD -> usage error
reset_state
"$DISPATCH" PROJ-75 >/dev/null 2>&1; check_rc "missing PIPELINE_CMD" 2 $?

# 15. pre-mutation Jira error (rc=6): helper committed nothing -> exit 6, NO rollback, no worktree
reset_state; seed_plan; seed_review
export STUB_OUTCOME=error STUB_CLAIM_RC=6
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "pre-mutation error rc=6" 6 $?
grep_log "$CLAIM_LOG" "rollback" && bad "rc=6: no rollback (no mutation)" || ok "rc=6: no rollback (no mutation)"
grep_log "$GIT_LOG" "worktree-add" && bad "rc=6: no worktree" || ok "rc=6: no worktree"

# 16. claim-uncertain (rc=8): assignee written, later step failed -> CLAIMED, rollback with prior, exit 1
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claim-uncertain STUB_CLAIM_RC=8 STUB_PRIOR_ASSIGNEE=NONE STUB_PRIOR_STATUS="To Do"
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "claim-uncertain rc=8 rolled back" 1 $?
grep_log "$CLAIM_LOG" "rollback pa=NONE ps=To Do" && ok "rc=8: rollback restored prior" || bad "rc=8: rollback restored prior"
grep_log "$GIT_LOG" "worktree-add" && bad "rc=8: no worktree (failed during claim)" || ok "rc=8: no worktree"

# 17. post-claim failure + PARTIAL rollback (helper rollback returns 7) -> exit 7 propagated
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0 STUB_PRIOR_ASSIGNEE=NONE STUB_PRIOR_STATUS="To Do" TMUX_PANE_DIES=1 STUB_ROLLBACK_RC=7
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "partial-rollback code propagated" 7 $?

# 18. ISSUE path-traversal attempt -> rejected by the anchored regex, claim never reached
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claimed STUB_CLAIM_RC=0
"$DISPATCH" "PROJ-75/../evil" "$AUTO_CMD" >/dev/null 2>&1; check_rc "issue traversal rejected" 2 $?
grep_log "$CLAIM_LOG" "claim outcome" && bad "traversal: claim not reached" || ok "traversal: claim not reached"

# 19. helper exits with an UNKNOWN code (e.g. 143 signal-killed) -> treat as uncertain, roll back
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claim-uncertain STUB_CLAIM_RC=143 STUB_PRIOR_ASSIGNEE=NONE STUB_PRIOR_STATUS="To Do"
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "unexpected helper exit -> rollback" 1 $?
grep_log "$CLAIM_LOG" "rollback pa=NONE ps=To Do" && ok "rc=143: treated as uncertain, rolled back" || bad "rc=143: rolled back"

# 20. helper dies BEFORE reporting prior state -> rollback REFUSES auto-restore (no force-unassign), exit 7
reset_state; seed_plan; seed_review
export STUB_OUTCOME=claim-uncertain STUB_CLAIM_RC=143 STUB_NO_PRIOR=1
"$DISPATCH" PROJ-75 "$AUTO_CMD" >/dev/null 2>&1; check_rc "no prior captured -> needs-human (exit 7)" 7 $?
grep_log "$CLAIM_LOG" "rollback" && bad "no-prior: must NOT force-unassign" || ok "no-prior: refused auto-restore (no force-unassign)"

echo
echo "== jira_claim.py (no-network unit assertions) =="
py_assert() { # desc  python-expr-that-prints-OK-or-not
  out="$(cd "$HERE" && python3 - <<PY 2>&1
import sys
sys.path.insert(0, "$HERE")
import jira_claim as m
$1
PY
)"
  [ "$out" = "OK" ] && ok "$2" || bad "$2 ($out)"
}
py_assert 'print("OK" if m._parse_body(b"") is None else "not-none")' "_parse_body empty -> None"
py_assert 'print("OK" if m._parse_body(b"{\"a\": 1}") == {"a": 1} else "bad")' "_parse_body non-empty -> parsed"
py_assert 'print("OK" if m.decide("me", "In Progress", "me") == "already-mine" else m.decide("me","In Progress","me"))' "decide already-mine"
py_assert 'print("OK" if m.decide("other", "To Do", "me") == "refused-other" else "bad")' "decide refused-other"
py_assert 'print("OK" if m.decide(None, "In Progress", "me") == "assign-only" else "bad")' "decide assign-only"
py_assert 'print("OK" if m.decide(None, "To Do", "me") == "claim" else "bad")' "decide claim"

# main() guards fail closed BEFORE any network call (validation + auth-env check):
( cd "$HERE" && python3 jira_claim.py --ticket 'bad-key' --action claim >/dev/null 2>&1 )
check_rc "helper rejects bad issue key (EX_USAGE)" 2 $?
( cd "$HERE"; export JIRA_BASE_URL=https://x.example JIRA_EMAIL=a@b.c; unset JIRA_API_TOKEN
  python3 jira_claim.py --ticket PROJ-75 --action claim >/dev/null 2>&1 )
check_rc "helper fails closed on missing token (EX_USAGE)" 2 $?
py_assert 'import os, signal
m._install_signal_handlers()
try:
    os.kill(os.getpid(), signal.SIGTERM)
    print("not-raised")
except m._Interrupted:
    print("OK")' "SIGTERM handler raises _Interrupted (no default terminate)"
py_assert 'import io, contextlib
m._call = lambda *a, **k: None
with contextlib.redirect_stdout(io.StringIO()):
    rc = m.do_rollback("PROJ-75", "NONE", "")
print("OK" if rc == m.EX_PARTIAL_ROLLBACK else f"bad:{rc}")' "do_rollback: empty prior_status -> partial (not false success)"

echo
if [ "$fail" -eq 0 ]; then echo "all dispatch tests passed ($pass)"; exit 0; else echo "$fail test(s) failed ($pass passed)"; exit 1; fi
