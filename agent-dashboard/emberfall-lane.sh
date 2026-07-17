#!/usr/bin/env bash
# emberfall-lane.sh — give ad-hoc (non-skill) Emberfall chat sessions a row on the agent dashboard.
#
# The dashboard's rows are just JSON snapshots written by emit-status.sh, so a plain chat session
# can claim one the same way a wired skill does. This hook does that automatically.
#
# Wire into ~/.claude/settings.json on three events (the event name is read from stdin):
#   UserPromptSubmit -> claims/refreshes the lane, state=implementing (yellow, "running")
#   Stop             -> state=started (cyan, "idle — your turn"), keeps the row fresh
#   SessionEnd       -> removes the row
#
# A session claims a lane when it is cwd'd into emberfall OR any prompt mentions emberfall; the
# claim is then sticky for the rest of the session via a marker in /tmp, so follow-up prompts that
# don't say "emberfall" keep the lane alive. Row lifetime = the session.
#
# Two cleanup modes, neither of which ever touches a skill-driven row:
#   --gc      drop ad-hoc lanes whose cmux surface is gone (a hard-killed tab never fires SessionEnd).
#             Safe to run on every coordinator pass; no-ops entirely when cmux can't be queried,
#             so a missing cmux never mass-deletes live lanes.
#   --prune   drop ALL ad-hoc lanes — the "I reset the coordinator" button.
#
# CONTRACT: best-effort like the emitter it wraps — always exits 0, never blocks a prompt.
set -u

EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "${EMIT:-}" ] || EMIT="$(command -v emit-status.sh 2>/dev/null)"
[ -x "${EMIT:-}" ] || exit 0

_AD_HOME="${AGENT_DASHBOARD_HOME:-$HOME/Projects/claude-workbench/agent-dashboard}"
STATE_DIR="${AGENT_DASHBOARD_STATE_DIR:-$_AD_HOME/state}"
PREFIX="ember-chat"

if [ "${1:-}" = "--prune" ]; then
  rm -f "$STATE_DIR/$PREFIX-"*.json /tmp/claude-ember-lane-* 2>/dev/null
  exit 0
fi

if [ "${1:-}" = "--gc" ]; then
  command -v python3 >/dev/null 2>&1 || exit 0
  STATE_DIR="$STATE_DIR" PREFIX="$PREFIX" python3 - <<'PY' 2>/dev/null
import glob, json, os, re, subprocess
state_dir, prefix = os.environ["STATE_DIR"], os.environ["PREFIX"]
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
try:  # same query + anchored UUID match the dashboard uses for its pane column
    out = subprocess.run(["cmux", "tree", "--all", "--id-format", "uuids"],
                         capture_output=True, text=True, timeout=2)
    live = {m.group(0).upper() for m in UUID.finditer(out.stdout)} if out.returncode == 0 else set()
except (OSError, subprocess.SubprocessError):
    live = set()
if not live:
    raise SystemExit(0)  # cmux unavailable -> can't prove anything is dead; touch nothing
for path in glob.glob(os.path.join(state_dir, f"{prefix}-*.json")):
    try:
        surface = str(json.load(open(path)).get("cmux_surface", "")).upper()
    except (OSError, ValueError):
        continue
    if surface and surface not in live:  # no surface recorded -> unprovable, leave it
        os.remove(path)
PY
  exit 0
fi

command -v jq >/dev/null 2>&1 || exit 0
payload="$(cat)"
sid="$(printf '%s' "$payload"   | jq -r '.session_id // empty')"
event="$(printf '%s' "$payload" | jq -r '.hook_event_name // empty')"
cwd="$(printf '%s' "$payload"   | jq -r '.cwd // empty')"
prompt="$(printf '%s' "$payload"| jq -r '.prompt // empty')"
[ -n "$sid" ] || exit 0

marker="/tmp/claude-ember-lane-$sid"
run="$PREFIX-$(printf '%s' "$sid" | cut -c1-6)"

if [ "$event" = "SessionEnd" ]; then
  [ -e "$marker" ] && "$EMIT" --remove --session "$run"
  rm -f "$marker" "/tmp/claude-ember-skill-$sid" 2>/dev/null
  exit 0
fi

# NEVER double-claim a session a wired skill already owns — it emits its own row, and a duplicate
# ad-hoc row would both double-count on the board and (carrying a worktree_path) show up as a
# phantom live editor at /emberfall-coordinate's liveness gate. Two independent detectors, because
# each alone has a hole: the prompt test fires on turn 1 before any skill row exists, and the
# surface test catches sessions whose skill row predates this hook or was launched some other way.
skill_marker="/tmp/claude-ember-skill-$sid"
if [ ! -e "$skill_marker" ]; then
  owned=""
  case "$prompt" in
    /emberfall*|/ship*|/implement*|/plan*|/code-review*|/pr-prep*|/babysit-pr*) owned=1 ;;
  esac
  if [ -z "$owned" ] && [ -n "${CMUX_SURFACE_ID:-}" ]; then
    # another non-terminal snapshot in this same cmux pane == a skill row for this session
    STATE_DIR="$STATE_DIR" PREFIX="$PREFIX" SURFACE="$CMUX_SURFACE_ID" python3 - <<'PY' 2>/dev/null && owned=1
import glob, json, os, sys
state_dir, prefix, surface = os.environ["STATE_DIR"], os.environ["PREFIX"], os.environ["SURFACE"].upper()
for path in glob.glob(os.path.join(state_dir, "*.json")):
    if os.path.basename(path).startswith(prefix + "-"):
        continue
    try:
        d = json.load(open(path))
    except (OSError, ValueError):
        continue
    if (str(d.get("cmux_surface", "")).upper() == surface
            and d.get("state") not in ("merged", "done")):
        sys.exit(0)   # found a live skill row owning this pane
sys.exit(1)
PY
  fi
  if [ -n "$owned" ]; then
    : > "$skill_marker"
    [ -e "$marker" ] && { "$EMIT" --remove --session "$run"; rm -f "$marker" 2>/dev/null; }
  fi
fi
[ -e "$skill_marker" ] && exit 0

# Claim the lane: already claimed, or cwd'd into emberfall, or the prompt names it.
if [ ! -e "$marker" ]; then
  case "$cwd" in
    */emberfall|*/emberfall/*) : ;;
    *) printf '%s' "$prompt" | grep -qi 'emberfall' || exit 0 ;;
  esac
  : > "$marker"
fi

# Note: prefer the topic I keep in the statusline context file; else the prompt's first line.
note="$(head -c 200 "/tmp/claude-statusline-ctx-$sid.txt" 2>/dev/null | tr -d '\n')"
[ -n "$note" ] || note="$(printf '%s' "$prompt" | head -n1 | cut -c1-70)"

if [ "$event" = "Stop" ]; then
  state="started"; note="idle · ${note:-awaiting input}"
else
  state="implementing"
fi

# Only a session actually sitting IN the tree can be editing it. Record the worktree in that case
# and leave it empty otherwise, so /emberfall-coordinate can tell a lane that owns files apart from
# a chat that merely talks about Emberfall from elsewhere — and only gate integration on the former.
worktree=""
case "$cwd" in */emberfall|*/emberfall/*) worktree="$cwd" ;; esac

"$EMIT" --session "$run" --role other --state "$state" --ticket adhoc \
        ${worktree:+--worktree "$worktree"} --note "$note"
exit 0
