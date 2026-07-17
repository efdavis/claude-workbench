#!/usr/bin/env bash
# handler.sh — the ONE external action handler for the orchestra dashboard.
#
# dashboard.py mutates nothing: every "open something" keypress shells out here.
# CONTRACT: fire-and-forget — always exit 0, and always print exactly one status line to
# stdout (the dashboard surfaces the last line as a transient message).
#
# Usage: handler.sh <coord> <key> <issue> <state> <live> <pr> <worktree_path>
#   <coord> : which list/pane the row lives in (currently always "runs"; kept for
#             contract-shape parity with the dashboard→handler seam, not yet branched on).
#   <key>   : enter | p | t
#   <live>  : the row's pane liveness from the collector (live | ghost | stale | -)
#
# cmux-required: if cmux is absent, print the install hint and no-op — no copy-paste
# or take-over-terminal fallback.
#
# Sessions: dispatch names the tmux lane <issue> on the private lane socket; the status
# row's own session id is <issue>-worker. So an attach targets <issue>.
#
# Browser opens are URL-base-configured (vendor-neutral, no hardcoded host):
#   AGENT_DASHBOARD_PR_URL_BASE      e.g. https://github.com/OWNER/REPO/pull  -> open $BASE/<pr>
#   AGENT_DASHBOARD_ISSUE_URL_BASE   e.g. https://you.atlassian.net/browse    -> open $BASE/<issue>
# Unset -> the action prints a hint telling you which var to set, and no-ops.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"  # so replay can find transcript.py next to us

PR_URL_BASE="${AGENT_DASHBOARD_PR_URL_BASE:-}"
ISSUE_URL_BASE="${AGENT_DASHBOARD_ISSUE_URL_BASE:-}"

coord="${1:-}"; key="${2:-}"; ticket="${3:-}"; state="${4:-}"; live="${5:-}"; pr="${6:-}"; worktree="${7:-}"
: "${coord:=runs}"  # currently unused beyond shape parity; keep the positional slot
# <state> ($4) is likewise part of the contract shape but not branched on — routing is
# keyed off <live> ($5); the slot is kept so the argv matches the dashboard→handler seam.

# tmux socket the lanes live on. Honor the same override dashboard.py reads so a lane on
# a custom socket that renders `live` also attaches on that socket.
SOCKET="${AGENT_DASHBOARD_TMUX_SOCKET:-agent-lanes}"

# Row fields come from a best-effort on-disk snapshot; the issue key is interpolated into
# a command string cmux "types" into a live shell, so constrain it to the safe charset
# (mirrors emit-status.sh's session sanitize). A well-formed issue key is untouched.
ticket="$(printf '%s' "$ticket" | tr -c 'A-Za-z0-9._-' '_')"

say() { printf '%s\n' "$*"; }

need_cmux() {
  command -v cmux >/dev/null 2>&1 && return 0
  say "install cmux (brew install cmux)"
  return 1
}

# Open a cmux terminal TAB and run a command in it. Args: <tab-title> <command-string>.
# `new-surface` = a tab, never `new-pane` (a pane splits the dashboard's own layout).
# The trailing literal \n is how `cmux send` submits the line as Enter. Never
# `send`/`rename` against an empty --surface — a mis-routed send types into someone
# else's pane.
cmux_terminal() {
  local title="$1" cmd="$2" out tok surf=""
  out="$(cmux new-surface --type terminal --focus true 2>/dev/null)" || { say "cmux: new-surface failed"; return 1; }
  # new-surface prints e.g. "OK surface:3 pane:5 workspace:1" — grab the surface:N token.
  for tok in $out; do
    case "$tok" in surface:*) surf="$tok"; break ;; esac
  done
  [ -n "$surf" ] || { say "cmux: no surface returned"; return 1; }
  cmux send --surface "$surf" -- "${cmd}\n" || { say "cmux: send failed"; return 1; }
  cmux rename-tab --surface "$surf" "$title" 2>/dev/null || true
  return 0
}

attach_lane() {
  need_cmux || return 0
  [ -n "$ticket" ] || { say "no issue for this row"; return 0; }
  # Clear any stuck tmux copy/view-mode FIRST: attaching to a session the user scrolled
  # into otherwise hangs. Harmless no-op if not in a mode.
  tmux -L "$SOCKET" send-keys -t "$ticket" -X cancel 2>/dev/null || true
  cmux_terminal "🎻 ${ticket}" "tmux -L ${SOCKET} attach -t ${ticket}" && say "attach → ${ticket}"
}

replay_recording() {
  need_cmux || return 0
  [ -n "$worktree" ] || { say "no worktree recorded for ${ticket:-this row}"; return 0; }
  # Claude Code stores a run's transcript under ~/.claude/projects/<slug>, where <slug> is
  # the worktree path with every char outside [A-Za-z0-9-] turned into '-' (e.g.
  # /home/you/projects/acme -> -home-you-projects-acme).
  local slug projdir jsonl
  slug="$(printf '%s' "$worktree" | tr -c 'A-Za-z0-9-' '-')"
  projdir="${HOME}/.claude/projects/${slug}"
  jsonl="$(ls -t "${projdir}"/*.jsonl 2>/dev/null | head -1)"
  [ -n "$jsonl" ] || { say "no transcript under ${projdir}"; return 0; }
  # Render the raw .jsonl into readable turns (transcript.py) rather than paging the raw
  # JSON — the raw file is dominated by hook/metadata noise. less -R keeps the ANSI colors.
  cmux_terminal "📼 ${ticket:-run}" "python3 ${HERE}/transcript.py ${jsonl} | less -R" && say "replay ${jsonl}"
}

open_browser() {  # <label> <url>
  need_cmux || return 0
  if cmux new-surface --type browser --url "$2" --focus true >/dev/null 2>&1; then
    say "opened $1"
  else
    say "cmux: could not open browser tab"
  fi
}

case "$key" in
  enter)
    case "$live" in
      live|ghost)
        # A row renders `live` from EITHER source: a real lane (dispatch), OR just a live
        # cmux surface (a hands-on run). Only the former is an attachable tmux session;
        # attaching a cmux run by issue key fails "can't find session". has-session is the
        # authoritative gate — attach a real lane, else say so.
        if [ -n "$ticket" ] && tmux -L "$SOCKET" has-session -t "$ticket" 2>/dev/null; then
          attach_lane
        else
          say "${ticket:-this run} is live in cmux, not a ${SOCKET} lane — switch to its own cmux tab"
        fi
        ;;
      *)          replay_recording ;;   # terminal/stale/absent -> replay the transcript
    esac
    ;;
  p)
    if [ -z "$pr" ]; then
      say "no PR yet for ${ticket:-this row}"
    elif [ -z "$PR_URL_BASE" ]; then
      say "set AGENT_DASHBOARD_PR_URL_BASE to open PR #${pr} (e.g. https://github.com/OWNER/REPO/pull)"
    else
      open_browser "PR #${pr}" "${PR_URL_BASE%/}/${pr}"
    fi
    ;;
  t)
    if [ -z "$ticket" ]; then
      say "no issue for this row"
    elif [ -z "$ISSUE_URL_BASE" ]; then
      say "set AGENT_DASHBOARD_ISSUE_URL_BASE to open ${ticket} (e.g. https://you.atlassian.net/browse)"
    else
      open_browser "${ticket}" "${ISSUE_URL_BASE%/}/${ticket}"
    fi
    ;;
  *)
    say "no action for key '${key}'"
    ;;
esac
exit 0
