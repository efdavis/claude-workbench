#!/usr/bin/env bash
# emit-status.sh — write one agent-run status snapshot for the agent dashboard.
#
# CONTRACT: best-effort, MUST NOT fail the caller. Any problem (no python3, unwritable
# state dir, bad args) -> warn to stderr only when AGENT_DASHBOARD_DEBUG is set, then
# exit 0. Observability must never break the run it observes.
# JSON is built with python3 (stdlib) - no jq or other dependency.
#
# Snapshots land in
#   ${AGENT_DASHBOARD_STATE_DIR:-${AGENT_DASHBOARD_HOME:-$HOME/Projects/claude-workbench/agent-dashboard}/state}/<session>.json
# written atomically (mktemp + mv). Schema: status.schema.json (sibling).
# Shared harness under ~/Projects so every project (Emberfall, etc.) sees one board.
#
# Usage:
#   emit-status.sh --session <id> --role <role> --state <state> \
#                  [--ticket PROJ-N] [--pr <number>] [--worktree <path>] [--model <name>] \
#                  [--claude-session-id <uuid>] [--note "..."]
#   emit-status.sh --remove --session <id>        # prune a run's snapshot
#
#   role  = planner | worker | reviewer | finisher | groomer | other
#   state = started | implementing | reviewing | waiting | pr-open | merged | escalated | done
#           (waiting = paused at a routine human gate; escalated = urgent, off-happy-path)
#   model = which Claude model drives the run (opus | sonnet | haiku | ...). If --model is omitted,
#           it is auto-detected: $AGENT_DASHBOARD_MODEL, else the `--model` token in cmux's launch
#           argv ($CMUX_AGENT_LAUNCH_ARGV_B64). Absent (default model, no cmux) -> shown as "-".
set -u

warn() { [ -n "${AGENT_DASHBOARD_DEBUG:-}" ] && printf 'emit-status: %s\n' "$*" >&2; return 0; }

session="" role="" state="" ticket="" pr="" note="" worktree="" model="" claude_session_id="" remove=""
while [ $# -gt 0 ]; do
  case "$1" in
    --session)  session="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
    --role)     role="${2:-}";    shift; [ $# -gt 0 ] && shift ;;
    --state)    state="${2:-}";   shift; [ $# -gt 0 ] && shift ;;
    --ticket)   ticket="${2:-}";  shift; [ $# -gt 0 ] && shift ;;
    --pr)       pr="${2:-}";      shift; [ $# -gt 0 ] && shift ;;
    --note)     note="${2:-}";    shift; [ $# -gt 0 ] && shift ;;
    --worktree) worktree="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
    --model)    model="${2:-}";   shift; [ $# -gt 0 ] && shift ;;
    --claude-session-id) claude_session_id="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
    --remove)   remove=1;         shift   ;;
    *)          warn "unknown arg: $1"; shift ;;
  esac
done

_ad_home="${AGENT_DASHBOARD_HOME:-$HOME/Projects/claude-workbench/agent-dashboard}"
state_dir="${AGENT_DASHBOARD_STATE_DIR:-$_ad_home/state}"
mkdir -p "$state_dir" 2>/dev/null || { warn "cannot create state dir: $state_dir"; exit 0; }

# sanitize session into a safe filename stem
safe_session="$(printf '%s' "$session" | tr -c 'A-Za-z0-9._-' '_')"

if [ -n "$remove" ]; then
  [ -n "$safe_session" ] && rm -f "$state_dir/${safe_session}.json" 2>/dev/null
  exit 0
fi

# required fields; missing -> best-effort no-op (never fail the caller)
if [ -z "$session" ] || [ -z "$role" ] || [ -z "$state" ]; then
  warn "missing required --session/--role/--state; no-op"; exit 0
fi
command -v python3 >/dev/null 2>&1 || { warn "python3 not found; no-op"; exit 0; }

epoch="$(date +%s 2>/dev/null)" || { warn "date failed"; exit 0; }
iso="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" || iso=""
tmp="$(mktemp "$state_dir/.${safe_session}.XXXXXX" 2>/dev/null)" || { warn "mktemp failed"; exit 0; }

python3 - "$session" "$role" "$state" "$ticket" "$pr" "$worktree" "$note" "$iso" "$epoch" "${CMUX_SURFACE_ID:-}" "$model" "$claude_session_id" \
  > "$tmp" 2>/dev/null <<'PY' || { warn "python3 write failed"; rm -f "$tmp" 2>/dev/null; exit 0; }
import base64, json, os, sys
session, role, state, ticket, pr, worktree, note, iso, epoch, cmux_surface, model, claude_session_id = sys.argv[1:13]
if not model:
    model = os.environ.get("AGENT_DASHBOARD_MODEL", "")
if not model:
    # cmux records the agent's launch argv; pull the model from `--model <name>` if present.
    b64 = os.environ.get("CMUX_AGENT_LAUNCH_ARGV_B64", "")
    if b64:
        try:
            parts = base64.b64decode(b64).decode("utf-8", "replace").split("\x00")
            if "--model" in parts:
                j = parts.index("--model")
                if j + 1 < len(parts):
                    model = parts[j + 1]
        except Exception:
            pass
doc = {"session": session, "role": role, "state": state, "iso_timestamp": iso, "epoch": int(epoch)}
if ticket:
    doc["ticket"] = ticket
if pr:
    doc["pr_number"] = pr
if worktree:
    doc["worktree_path"] = worktree
if model:
    doc["model"] = model
if note:
    doc["note"] = note
if cmux_surface:
    doc["cmux_surface"] = cmux_surface
if claude_session_id:
    doc["claude_session_id"] = claude_session_id
json.dump(doc, sys.stdout)
PY

mv -f "$tmp" "$state_dir/${safe_session}.json" 2>/dev/null || { warn "mv failed"; rm -f "$tmp" 2>/dev/null; exit 0; }
exit 0
