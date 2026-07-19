#!/usr/bin/env bash
# handler.sh — the ONE external action handler for the orchestra dashboard.
#
# dashboard.py mutates nothing: every "open something" keypress shells out here.
# CONTRACT: fire-and-forget — always exit 0, and always print exactly one status line to
# stdout (the dashboard surfaces the last line as a transient message).
#
# Usage: handler.sh <coord> <key> <issue> <state> <live> <pr> <worktree_path> <cmux_surface>
#   <coord> : which list/pane the row lives in (currently always "runs"; kept for
#             contract-shape parity with the dashboard→handler seam, not yet branched on).
#   <key>   : enter | p | t | r
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

coord="${1:-}"; key="${2:-}"; ticket="${3:-}"; state="${4:-}"; live="${5:-}"; pr="${6:-}"; worktree="${7:-}"; surface="${8:-}"
: "${coord:=runs}"  # currently unused beyond shape parity; keep the positional slot
# <state> ($4) is likewise part of the contract shape but not branched on — routing is
# keyed off <live> ($5); the slot is kept so the argv matches the dashboard→handler seam.

# tmux socket the lanes live on. Honor the same override dashboard.py reads so a lane on
# a custom socket that renders `live` also attaches on that socket.
SOCKET="${AGENT_DASHBOARD_TMUX_SOCKET:-agent-lanes}"

# Row fields come from a best-effort on-disk snapshot; the issue key is interpolated into
# a command string cmux "types" into a live shell, so constrain it to the safe charset
# (mirrors emit-status.sh's session sanitize). A well-formed issue key is untouched.
# Strip a leading '#' first (some snapshots carry "#39" rather than "39") — otherwise the
# charset filter below turns it into "_39", which breaks both tmux session lookup and the
# GitHub issue URL built from ISSUE_URL_BASE.
ticket="$(printf '%s' "$ticket" | sed 's/^#//' | tr -c 'A-Za-z0-9._-' '_')"

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

# Jump the cmux view to a hands-on run's own tab: focus its surface (the horizontal
# tab), follow with its workspace (cmux's left-column list, parsed from the focus
# response so the whole view lands on the tab), then flash it. Best-effort;
# cmux-required like the other openers. surface.focus alone already brings the tab
# forward; workspace.select makes the left-column selection follow too.
focus_cmux_tab() {  # $1 = cmux surface uuid
  need_cmux || return 0
  local surf="$1" out ws
  [ -n "$surf" ] || { say "no cmux surface recorded for ${ticket:-this run}"; return 0; }
  out="$(cmux rpc surface.focus "{\"surface_id\":\"${surf}\"}" 2>/dev/null)" \
    || { say "cmux: focus failed for ${ticket:-this run}"; return 0; }
  ws="$(printf '%s\n' "$out" | grep '"workspace_id"' \
        | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}' | head -1)"
  [ -n "$ws" ] && cmux rpc workspace.select "{\"workspace_id\":\"${ws}\"}" >/dev/null 2>&1
  cmux trigger-flash --surface "$surf" >/dev/null 2>&1
  say "jumped to ${ticket:-run}'s cmux tab"
}

# End a still-running row for the dashboard `r` key: open/focus its pane, then kill it.
# Only called when the dashboard reports live|ghost — never on stale/tombstone rows, whose
# cmux_surface is often a shared/stale UUID from an earlier process (closing it would kill
# someone else's live tab). Best-effort; always prints exactly one status line; never exits
# non-zero. Uses a silent cmux presence check (not need_cmux) so an install-hint can't
# leak an extra stdout line before the final `say`.
end_live_run() {
  local did=0 closed=0 killed=0 no_cmux=0 bad_surf=0 kill_fail=0
  case "$live" in
    live|ghost) ;;
    *)
      say "nothing live to end for ${ticket:-this row}"
      return 0
      ;;
  esac

  # 1. Open the pane first (hands-on cmux surface): focus + flash so you see what you're
  # ending, then close the surface (kills the agent process in that tab).
  # Surface must look like a UUID (or surface:N ref) — never interpolate raw snapshot
  # garbage into the focus JSON / close-surface argv.
  if [ -n "$surface" ]; then
    if printf '%s' "$surface" | grep -Eq '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$|^surface:[0-9]+$'; then
      if command -v cmux >/dev/null 2>&1; then
        local out ws
        # Quiet focus (no early `say` — would break the one-line status contract).
        out="$(cmux rpc surface.focus "{\"surface_id\":\"${surface}\"}" 2>/dev/null)" || true
        ws="$(printf '%s\n' "$out" | grep '"workspace_id"' \
              | grep -oE '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}' \
              | head -1)"
        [ -n "${ws:-}" ] && cmux rpc workspace.select "{\"workspace_id\":\"${ws}\"}" >/dev/null 2>&1 || true
        cmux trigger-flash --surface "$surface" >/dev/null 2>&1 || true
        if cmux close-surface --surface "$surface" >/dev/null 2>&1; then
          closed=1
          did=1
        fi
      else
        no_cmux=1
      fi
    else
      bad_surf=1
    fi
  fi

  # 2. Dispatch lane (tmux session named <issue>): kill it so the lane doesn't keep
  # running after the card is reaped. No attach tab — we want it gone, not opened.
  # Same ticket-sanitize as attach; has-session is the gate so a hands-on row whose
  # ticket happens not to be a lane is a no-op here.
  if [ -n "$ticket" ] && tmux -L "$SOCKET" has-session -t "$ticket" 2>/dev/null; then
    if tmux -L "$SOCKET" kill-session -t "$ticket" 2>/dev/null; then
      killed=1
      did=1
    else
      kill_fail=1
    fi
  fi

  if [ "$did" -eq 1 ]; then
    local bits=""
    [ "$closed" -eq 1 ] && bits="closed cmux tab"
    if [ "$killed" -eq 1 ]; then
      [ -n "$bits" ] && bits="${bits} + "
      bits="${bits}killed lane ${ticket}"
    fi
    say "ended ${ticket:-run}: ${bits}"
  elif [ "$no_cmux" -eq 1 ] && [ -n "$surface" ] && [ -z "$ticket" ]; then
    say "install cmux (brew install cmux)"
  elif [ "$bad_surf" -eq 1 ] && [ "$kill_fail" -eq 0 ]; then
    say "end: bad cmux_surface id for ${ticket:-this row}"
  elif [ "$kill_fail" -eq 1 ]; then
    say "end: kill-session failed for lane ${ticket}"
  else
    say "end: nothing to kill for ${ticket:-this row} (no live surface/lane)"
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
          # not a lane -> a hands-on run in its own cmux tab: jump to it
          focus_cmux_tab "$surface"
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
  r)
    end_live_run
    ;;
  *)
    say "no action for key '${key}'"
    ;;
esac
exit 0
