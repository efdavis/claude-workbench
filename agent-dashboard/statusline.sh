#!/bin/bash
# statusline.sh — Claude Code statusline: ticket/task | context % | cost
#
# This is the *producer* half of the dashboard's data contract. Claude Code pipes a JSON
# blob into this script on every render (a shell hook — no model call, so nothing here
# costs a token), and the mirrors written below are the only way dashboard.py can know a
# run's context fill, its spend, or the account's usage limit.
#
# Install: copy to ~/.claude/statusline.sh and point settings.json at it:
#   "statusLine": { "type": "command", "command": "bash ~/.claude/statusline.sh" }
#
# Everything is best-effort by contract, same as emit-status.sh: a failed write is
# silently skipped and the dashboard just renders "-" for that cell. It can never break
# the session it observes.
input=$(cat)

# Parse JSON fields in one jq call
eval "$(echo "$input" | jq -r '
  @sh "PCT=\(.context_window.used_percentage // 0 | floor)",
  @sh "COST_RAW=\(.cost.total_cost_usd // 0)",
  @sh "PROJECT_DIR=\(.workspace.project_dir // .cwd // "")",
  @sh "SESSION_NAME=\(.session_name // "")",
  @sh "SESSION_ID=\(.session_id // "")",
  @sh "RL5_PCT=\(.rate_limits.five_hour.used_percentage // -1 | floor)",
  @sh "RL5_RESET=\(.rate_limits.five_hour.resets_at // 0 | floor)"
' 2>/dev/null)" 2>/dev/null || {
  printf '\e[31m??\e[0m'
  exit 0
}

# --- Mirrors: the dashboard's data contract (see dashboard.py) ---
# Context % and cost, per session — so a session can self-check its own budget.
[ -n "$SESSION_ID" ] && echo "$PCT" > "/tmp/claude-context-pct-$SESSION_ID.txt" 2>/dev/null
[ -n "$SESSION_ID" ] && echo "$COST_RAW" > "/tmp/claude-cost-usd-$SESSION_ID.txt" 2>/dev/null
# The same two, keyed by cmux surface id. Dashboard snapshots record the run's
# $CMUX_SURFACE_ID but not its Claude session id, so this is the only join it has.
[ -n "${CMUX_SURFACE_ID:-}" ] && echo "$COST_RAW" > "/tmp/claude-cost-usd-surface-$CMUX_SURFACE_ID.txt" 2>/dev/null
[ -n "${CMUX_SURFACE_ID:-}" ] && echo "$PCT" > "/tmp/claude-context-pct-surface-$CMUX_SURFACE_ID.txt" 2>/dev/null
# The 5-hour plan usage limit ("Current session" in claude.ai settings) + its reset epoch.
# Account-wide, not per-session, so every session writes the same file and they agree;
# whichever renders last wins. Guarded: older clients omit rate_limits -> PCT is -1, skip.
[ "${RL5_PCT:--1}" -ge 0 ] 2>/dev/null && echo "$RL5_PCT $RL5_RESET" > /tmp/claude-rate-limit-5h.txt 2>/dev/null

# --- Ticket/task label ---
# Resolution order: the session name, then a Claude-written context file, then the branch.
TICKET=""
DESC=""

# A ticket id is any ABC-123 prefix; adjust the pattern if your tracker differs.
TICKET_RE='^([A-Z]+-[0-9]+)[[:space:]-]*(.*)'

if [ -n "$SESSION_NAME" ]; then
  if [[ "$SESSION_NAME" =~ $TICKET_RE ]]; then
    TICKET="${BASH_REMATCH[1]}"
    DESC="${BASH_REMATCH[2]}"
  else
    DESC="$SESSION_NAME"
  fi
else
  # Claude-written context file — per-session first, fall back to per-project
  SESSION_CTX_FILE=""
  [ -n "$SESSION_ID" ] && SESSION_CTX_FILE="/tmp/claude-statusline-ctx-$SESSION_ID.txt"
  PROJECT_CTX_FILE="/tmp/claude-statusline-ctx-$(echo -n "$PROJECT_DIR" | md5 -q 2>/dev/null || md5sum 2>/dev/null | cut -d' ' -f1)"
  CTX=""
  if [ -n "$SESSION_CTX_FILE" ] && [ -s "$SESSION_CTX_FILE" ]; then
    CTX=$(cat "$SESSION_CTX_FILE" 2>/dev/null)
  elif [ -f "$PROJECT_CTX_FILE" ]; then
    CTX=$(cat "$PROJECT_CTX_FILE" 2>/dev/null)
  fi

  if [ -n "$CTX" ]; then
    if [[ "$CTX" =~ $TICKET_RE ]]; then
      TICKET="${BASH_REMATCH[1]}"
      DESC="${BASH_REMATCH[2]}"
    else
      DESC="$CTX"
    fi
    [ ${#DESC} -gt 35 ] && DESC="${DESC:0:32}..."
  else
    BRANCH=""
    [ -n "$PROJECT_DIR" ] && BRANCH=$(git -C "$PROJECT_DIR" symbolic-ref --short HEAD 2>/dev/null) || true
    if [ -n "$BRANCH" ]; then
      if [[ "$BRANCH" =~ $TICKET_RE ]]; then
        TICKET="${BASH_REMATCH[1]}"
        DESC="${BASH_REMATCH[2]}"
        [ ${#DESC} -gt 25 ] && DESC="${DESC:0:22}..."
      else
        DESC="$BRANCH"
        [ ${#DESC} -gt 30 ] && DESC="${DESC:0:27}..."
      fi
    else
      DESC=$(basename "$PROJECT_DIR" 2>/dev/null)
    fi
  fi
fi

# --- Context % color: green -> yellow -> orange -> red as auto-compact approaches ---
if [ "$PCT" -le 30 ]; then CTX_COLOR='\e[32m'
elif [ "$PCT" -le 60 ]; then CTX_COLOR='\e[33m'
elif [ "$PCT" -le 80 ]; then CTX_COLOR='\e[38;5;208m'
else CTX_COLOR='\e[31m'; fi

COST_FMT=$(printf '$%.2f' "$COST_RAW" 2>/dev/null || echo '$0.00')

# --- Colors ---
C_TICKET='\e[38;5;75m'   # bright blue
C_DESC='\e[38;5;252m'    # light gray
C_SEP='\e[38;5;240m'     # dim gray separator
C_COST='\e[38;5;114m'    # soft green
RST='\e[0m'

# --- Build output ---
OUT=""
if [ -n "$TICKET" ] && [ -n "$DESC" ]; then
  OUT+="${C_TICKET}\e[1m${TICKET}${RST} ${C_DESC}${DESC}${RST}"
elif [ -n "$TICKET" ]; then
  OUT+="${C_TICKET}\e[1m${TICKET}${RST}"
elif [ -n "$DESC" ]; then
  OUT+="${C_DESC}\e[1m${DESC}${RST}"
else
  OUT+="${C_DESC}\e[1mclaude${RST}"
fi

OUT+="  ${C_SEP}|${RST}  ${CTX_COLOR}${PCT}%${RST}"

# Cost hidden when $0.00
if [ "$COST_FMT" != '$0.00' ]; then
  OUT+="  ${C_SEP}|${RST}  ${C_COST}${COST_FMT}${RST}"
fi

printf '%b' "$OUT"
