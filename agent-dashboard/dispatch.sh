#!/usr/bin/env bash
# dispatch.sh ISSUE PIPELINE_CMD
#
# Dispatch one issue into an isolated background lane: claim it in Jira, create a
# dedicated git worktree of the repo you run this from, and spawn a detached tmux session
# running PIPELINE_CMD in that worktree. Multiple issues can then run in parallel without
# sharing a checkout HEAD or double-claiming an issue. This is the spawn half; the
# dashboard (dashboard.py + handler.sh) is the viewing half.
#
# Run it from inside the repo you want to work in (cwd = that repo). The toolkit's own
# helpers (jira_claim.py, emit-status.sh) are resolved next to this script.
#
# Stages, each fail-closed, with a rollback that RESTORES prior state (never a blind
# teardown): preflight -> claim -> worktree -> seed reviewed artifacts -> tmux spawn.
# The claim runs before any spawn; a lost race leaves the winner's assignee untouched.
# A signal (INT/TERM) or any post-claim failure triggers the same rollback.
#
# Auth (required): JIRA_BASE_URL + JIRA_EMAIL + JIRA_API_TOKEN (see jira_claim.py).
# The Jira work is delegated to jira_claim.py (overridable via AGENT_DASHBOARD_CLAIM_CMD
# for testing).
#
# Usage:
#   dispatch.sh PROJ-75 'claude "/implement --auto PROJ-75"'
#
# Env:
#   AGENT_DASHBOARD_TMUX_SOCKET    private tmux socket for lanes (default: agent-lanes)
#   AGENT_DASHBOARD_TARGET_BRANCH  branch the worktree is cut from (default: origin/main)
#
# Exit codes:
#   0  dispatched (claimed + worktree + live tmux lane)
#   1  refused / failed (preflight refusal, or a post-claim failure rolled back cleanly)
#   2  usage / environment error (bad args, missing tool/token)
#   3  refused - issue assigned to someone else         (claim helper, no mutation)
#   4  no matching "In Progress" transition, fail closed (claim helper, no mutation)
#   5  lost claim race                                   (claim helper, no mutation)
#   6  Jira HTTP/transport error before any mutation     (claim helper, no mutation)
#   7  post-claim failure AND the Jira rollback could only partially restore
#      (assignee restored, status could not be) - the issue needs a human
# (3/4/5/6 propagate the claim helper's code; nothing was mutated on those paths.)
set -u

SOCKET="${AGENT_DASHBOARD_TMUX_SOCKET:-agent-lanes}"   # private tmux socket: tmux -L <socket>
TARGET_BRANCH="${AGENT_DASHBOARD_TARGET_BRANCH:-origin/main}"

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

die() { echo "dispatch: $*" >&2; }

usage() {
  echo "usage: dispatch.sh ISSUE PIPELINE_CMD" >&2
  echo "  e.g. dispatch.sh PROJ-75 'claude \"/implement --auto PROJ-75\"'" >&2
}

# --- args -------------------------------------------------------------------
TICKET="${1:-}"
PIPELINE_CMD="${2:-}"
if [ -z "$TICKET" ] || [ -z "$PIPELINE_CMD" ]; then usage; exit 2; fi
# Anchored: a Jira-style key (PROJ-<digits>) exactly. A loose glob would let `../` or `;`
# through into the worktree path and the REST key.
if [[ ! "$TICKET" =~ ^[A-Z][A-Z0-9]+-[0-9]+$ ]]; then
  die "ISSUE must be a Jira-style key, e.g. PROJ-75 (got: $TICKET)"; exit 2
fi

# --- required tooling -------------------------------------------------------
for tool in git tmux python3; do
  command -v "$tool" >/dev/null 2>&1 || { die "$tool not found on PATH"; exit 2; }
done
if [ -z "${JIRA_API_TOKEN:-}" ]; then
  die "JIRA_API_TOKEN is not set; the claim step needs it (see jira_claim.py). Refusing."
  exit 2
fi

# Anchor to the repo you invoked this from (cwd), never the toolkit's own checkout.
REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  die "not inside a git repository (run dispatch from the repo you want to work in)"; exit 2; }
WT="$(dirname "$REPO")/${TICKET}-wt"

# Overridable claim command (the test substitutes a stub). Word-split intentionally.
CLAIM_CMD="${AGENT_DASHBOARD_CLAIM_CMD:-python3 $SELF_DIR/jira_claim.py}"

# --- rollback state ---------------------------------------------------------
CLAIMED=""          # set once we may own / have mutated the issue
WT_CREATED=""       # set once the worktree exists
TMUX_CREATED=""     # set once the tmux session exists
PRIOR_ASSIGNEE=""
PRIOR_STATUS=""
PRIOR_CAPTURED=""   # set once the claim helper actually reported prior state
ROLLBACK_RC=0       # exit code of the Jira rollback (7 = partial / needs a human)

rollback() {
  # Tear down only what THIS run created, most-recent first, then restore Jira.
  # Teardown failures are surfaced loudly (not swallowed) - a stale worktree or a
  # still-live pane whose cwd we are about to remove must not pass silently.
  if [ -n "$TMUX_CREATED" ]; then
    tmux -L "$SOCKET" kill-session -t "$TICKET" 2>/dev/null
    if tmux -L "$SOCKET" has-session -t "$TICKET" 2>/dev/null; then
      die "rollback: tmux session $TICKET still alive after kill - check by hand"
    fi
  fi
  if [ -n "$WT_CREATED" ]; then
    git -C "$REPO" worktree remove --force "$WT" 2>/dev/null \
      || die "rollback: could not remove worktree $WT - remove it by hand"
    git -C "$REPO" worktree prune 2>/dev/null \
      || die "rollback: worktree prune failed - a stale registration may remain (git worktree list)"
  fi
  if [ -n "$CLAIMED" ]; then
    if [ -z "$PRIOR_CAPTURED" ]; then
      # The claim helper died before reporting prior state, so we don't know what to
      # restore. Auto-restoring would force-unassign against an unknown prior and then
      # falsely report a clean restore - the exact false-narrative failure this guard
      # closes. Refuse, flag for a human, and mark needs-attention (7).
      die "rollback: prior state was never captured for $TICKET (claim helper died before reporting it); NOT auto-restoring assignee/status - check $TICKET by hand"
      ROLLBACK_RC=7
    else
      # shellcheck disable=SC2086
      $CLAIM_CMD --ticket "$TICKET" --action rollback \
        --prior-assignee "${PRIOR_ASSIGNEE:-NONE}" --prior-status "$PRIOR_STATUS"
      ROLLBACK_RC=$?
      [ "$ROLLBACK_RC" -ne 0 ] && die "rollback of the Jira claim returned $ROLLBACK_RC - check $TICKET by hand"
    fi
  fi
}

fail() {
  die "$*"; rollback
  # Any nonzero rollback result (partial status-restore, or the rollback call itself
  # failing over HTTP) means the issue may need a human - surface that distinctly (7)
  # from a clean rollback (1).
  [ "$ROLLBACK_RC" -ne 0 ] && exit 7
  exit 1
}

# Re-entrancy guarded: disarm before running rollback so a second signal mid-rollback
# can't re-enter and overlap teardown.
on_signal() {
  trap '' INT TERM; echo >&2; die "interrupted - rolling back"; rollback
  # Honor the rollback result the same way fail() does: a nonzero ROLLBACK_RC means the
  # issue may need a human, distinct from a clean interrupt-rollback (130).
  [ "$ROLLBACK_RC" -ne 0 ] && exit 7
  exit 130
}
# Ignore signals through preflight + the claim capture: the claim helper installs its
# OWN SIGINT/SIGTERM handlers and self-reports claim-uncertain (exit 8) if interrupted
# mid-mutation, so the shell must not die mid-capture and lose the exit code that drives
# rollback. The rollback-capable trap is armed only once we own the issue (below).
trap '' INT TERM

# --- preflight refusals (before any state change) ---------------------------
if [ -e "$WT" ]; then die "worktree path already exists: $WT (refusing)"; exit 1; fi
if git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -qF "worktree $WT"; then
  die "a worktree is already registered at $WT (refusing)"; exit 1
fi
if tmux -L "$SOCKET" has-session -t "$TICKET" 2>/dev/null; then
  die "a tmux session named $TICKET already exists on socket $SOCKET (refusing)"; exit 1
fi

# --- claim (before any spawn) -----------------------------------------------
# shellcheck disable=SC2086
claim_out="$($CLAIM_CMD --ticket "$TICKET" --action claim 2>&1)"; claim_rc=$?
printf '%s\n' "$claim_out"
PRIOR_ASSIGNEE="$(printf '%s\n' "$claim_out" | sed -n 's/^PRIOR_ASSIGNEE=//p' | head -1)"
PRIOR_STATUS="$(printf '%s\n' "$claim_out" | sed -n 's/^PRIOR_STATUS=//p' | head -1)"
# Did the helper actually report prior state? If not, rollback must NOT auto-restore.
printf '%s\n' "$claim_out" | grep -q '^PRIOR_ASSIGNEE=' && PRIOR_CAPTURED=1
case "$claim_rc" in
  0) CLAIMED=1 ;;   # owned
  8) # claim-uncertain: assignee write may have committed; drive rollback to restore prior.
     CLAIMED=1
     fail "claim of $TICKET is uncertain (assignee written, a later step failed); rolling back" ;;
  2|3|4|5|6)
     # refused-other(3)/no-transition(4)/lost-race(5)/pre-mutation error(6)/usage(2): the
     # helper committed NO mutation and left the assignee correct (winner/prior untouched).
     die "claim did not succeed for $TICKET (code $claim_rc); no mutation, not dispatching."
     exit "$claim_rc" ;;
  *) # Any OTHER code (signal death 130/143, uncaught exit 1): the helper may have mutated
     # before dying. Safe default - assume uncertain and roll back rather than orphan a claim.
     CLAIMED=1
     fail "claim helper exited unexpectedly ($claim_rc) for $TICKET; treating as uncertain, rolling back" ;;
esac
# We own the issue. Arm the rollback-capable trap for the worktree/seed/tmux window.
trap on_signal INT TERM

# --- worktree ---------------------------------------------------------------
err="$(git -C "$REPO" fetch origin 2>&1)" || fail "git fetch origin failed: $err"
err="$(git -C "$REPO" worktree add --detach "$WT" "$TARGET_BRANCH" 2>&1)" \
  || fail "git worktree add $WT from $TARGET_BRANCH failed: $err"
WT_CREATED=1

# --- seed reviewed artifacts (.plans/ is typically gitignored, absent from the worktree) ---
mkdir -p "$WT/.plans"
seed_failed=""
for f in "$REPO"/.plans/"$TICKET"-* "$REPO"/.plans/"$TICKET".*; do
  [ -e "$f" ] || continue
  cp "$f" "$WT/.plans/" 2>/dev/null || { seed_failed=1; die "warning: could not seed $(basename "$f") into the worktree"; }
done
# For an --auto lane the seeded plan + review are load-bearing: without them the lane's
# gate refuses (or it runs with no plan) inside the detached pane where nobody sees it.
# Fail loudly at dispatch on a missing review OR any failed artifact copy.
case "$PIPELINE_CMD" in
  *--auto*)
    if [ ! -f "$WT/.plans/${TICKET}.review.json" ]; then
      fail "PIPELINE_CMD is --auto but no ${TICKET}.review.json was available to seed; the lane would fail its plan-review gate. Run /plan-review first."
    fi
    if [ -n "$seed_failed" ]; then
      fail "PIPELINE_CMD is --auto but a plan artifact failed to copy into the worktree; the lane would run without its full plan. Aborting."
    fi ;;
esac

# --- detached lane ----------------------------------------------------------
tmux -L "$SOCKET" new-session -d -s "$TICKET" -c "$WT" "$PIPELINE_CMD" \
  || fail "tmux new-session failed"
TMUX_CREATED=1
sleep 1
if ! tmux -L "$SOCKET" has-session -t "$TICKET" 2>/dev/null; then
  fail "lane pane died within ~1s of spawn (check PIPELINE_CMD)"
fi

# Past the last rollback-worthy step: the lane is live and owns the worktree now.
trap - INT TERM

# --- dashboard row (best-effort) --------------------------------------------
EMIT="$SELF_DIR/emit-status.sh"
if [ -x "$EMIT" ]; then
  emit_err="$("$EMIT" --session "${TICKET}-worker" --role worker --state started \
    --ticket "$TICKET" --worktree "$WT" --note "dispatched lane" 2>&1)" \
    || die "warning: dashboard emit failed (non-fatal): $emit_err"
fi

echo "dispatch: dispatched $TICKET"
echo "  worktree: $WT"
echo "  lane:     tmux -L $SOCKET attach -t $TICKET"
exit 0
