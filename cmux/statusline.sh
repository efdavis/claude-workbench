#!/bin/bash
# Claude Code statusline — minimal: ticket/task | model·effort | context % | cost
# Each element gets a distinct color for quick scanning.
#
# Wire it up in ~/.claude/settings.json:
#   "statusLine": { "type": "command", "command": "bash ~/.claude/statusline.sh" }
#
# Label precedence: session name → Claude-written context file → git branch → dir name.
# Optionally restrict ticket detection to specific project keys, e.g.:
#   export STATUSLINE_TICKET_KEYS="ABC|XYZ"
# Empty (default) matches any uppercase Jira-style key (ABC-123).
#
# Requires: jq.
input=$(cat)

# Restrict ticket detection to these project keys (pipe-separated), or match any key.
TICKET_KEYS="${STATUSLINE_TICKET_KEYS:-}"

# Parse JSON fields in one jq call
eval "$(echo "$input" | jq -r '
  @sh "PCT=\(.context_window.used_percentage // 0 | floor)",
  @sh "COST_RAW=\(.cost.total_cost_usd // 0)",
  @sh "PROJECT_DIR=\(.workspace.project_dir // .cwd // "")",
  @sh "SESSION_NAME=\(.session_name // "")",
  @sh "SESSION_ID=\(.session_id // "")",
  @sh "MODEL_ID=\(.model.id // "")",
  @sh "MODEL_NAME=\(.model.display_name // "")",
  @sh "EFFORT=\(.effort.level // "")"
' 2>/dev/null)" 2>/dev/null || {
  printf '\e[31m??\e[0m'
  exit 0
}

# Regex for a ticket key: either the configured keys or any uppercase prefix.
if [ -n "$TICKET_KEYS" ]; then
  KEY_RE="($TICKET_KEYS)-([0-9]+)"
else
  KEY_RE="([A-Z]+)-([0-9]+)"
fi

# --- Ticket/task label ---
TICKET=""
DESC=""
LABEL=""

if [ -n "$SESSION_NAME" ]; then
  # Try to split session name into ticket + description
  if [[ "$SESSION_NAME" =~ ^([A-Z]+-[0-9]+)[[:space:]]*(.*) ]]; then
    TICKET="${BASH_REMATCH[1]}"
    DESC="${BASH_REMATCH[2]}"
  else
    DESC="$SESSION_NAME"
  fi
else
  # Check for Claude-written context file — per-session first, fall back to per-project.
  # An optional UserPromptSubmit hook can write a "TICKET short-description" label to
  # /tmp/claude-statusline-ctx-<session_id>.txt so the bar tracks the live topic.
  SESSION_CTX_FILE=""
  [ -n "$SESSION_ID" ] && SESSION_CTX_FILE="/tmp/claude-statusline-ctx-$SESSION_ID.txt"
  PROJECT_CTX_FILE="/tmp/claude-statusline-ctx-$(echo -n "$PROJECT_DIR" | md5 -q 2>/dev/null || md5sum 2>/dev/null | cut -d' ' -f1)"
  CTX=""
  if [ -n "$SESSION_CTX_FILE" ] && [ -f "$SESSION_CTX_FILE" ] && [ -s "$SESSION_CTX_FILE" ]; then
    CTX=$(cat "$SESSION_CTX_FILE" 2>/dev/null)
  elif [ -f "$PROJECT_CTX_FILE" ]; then
    CTX=$(cat "$PROJECT_CTX_FILE" 2>/dev/null)
  fi

  if [ -n "$CTX" ]; then
    # Context file format: optional "TICKET desc" or just "desc"
    if [[ "$CTX" =~ ^([A-Z]+-[0-9]+)[[:space:]]*(.*) ]]; then
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
      if [[ "$BRANCH" =~ $KEY_RE ]]; then
        TICKET="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
        DESC="${BRANCH##*${TICKET}}"
        DESC="${DESC#-}"
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

# --- Context % color ---
if [ "$PCT" -le 30 ]; then CTX_COLOR='\e[32m'         # green
elif [ "$PCT" -le 60 ]; then CTX_COLOR='\e[33m'        # yellow
elif [ "$PCT" -le 80 ]; then CTX_COLOR='\e[38;5;208m'  # orange
else CTX_COLOR='\e[31m'; fi                             # red

# --- Cost ---
COST_FMT=$(printf '$%.2f' "$COST_RAW" 2>/dev/null || echo '$0.00')

# --- Colors ---
C_TICKET='\e[38;5;75m'   # bright blue
C_DESC='\e[38;5;252m'    # light gray
C_SEP='\e[38;5;240m'     # dim gray separator
C_COST='\e[38;5;114m'    # soft green
RST='\e[0m'

# --- Model + effort chip ---
C_EFF='\e[38;5;245m'                       # mid gray (effort)
MODEL_SHORT=""
MODEL_COLOR='\e[38;5;245m'                 # neutral fallback
case "$MODEL_ID" in
  *opus*)   MODEL_SHORT="opus";   MODEL_COLOR='\e[38;2;74;163;255m'  ;;  # blue
  *sonnet*) MODEL_SHORT="sonnet"; MODEL_COLOR='\e[38;2;45;212;191m'  ;;  # teal
  *fable*)  MODEL_SHORT="fable";  MODEL_COLOR='\e[38;2;157;92;255m'  ;;  # deep purple
  *haiku*)  MODEL_SHORT="haiku";  MODEL_COLOR='\e[38;2;74;222;128m'  ;;  # green
  *)        MODEL_SHORT=$(printf '%s' "$MODEL_NAME" | awk '{print tolower($1)}') ;;
esac

CHIP=""
if [ -n "$MODEL_SHORT" ]; then
  CHIP="${MODEL_COLOR}\e[1m${MODEL_SHORT}${RST}"
  [ -n "$EFFORT" ] && CHIP+="${C_SEP}·${RST}${C_EFF}${EFFORT}${RST}"
fi

# --- Build output ---
OUT=""

# Ticket (bright blue, bold) + description (light gray)
if [ -n "$TICKET" ] && [ -n "$DESC" ]; then
  OUT+="${C_TICKET}\e[1m${TICKET}${RST} ${C_DESC}${DESC}${RST}"
elif [ -n "$TICKET" ]; then
  OUT+="${C_TICKET}\e[1m${TICKET}${RST}"
elif [ -n "$DESC" ]; then
  OUT+="${C_DESC}\e[1m${DESC}${RST}"
else
  OUT+="${C_DESC}\e[1mclaude${RST}"
fi

# Separator + model·effort chip (between summary and context %)
if [ -n "$CHIP" ]; then
  OUT+="  ${C_SEP}|${RST}  ${CHIP}"
fi

# Separator + context %
OUT+="  ${C_SEP}|${RST}  ${CTX_COLOR}${PCT}%${RST}"

# Separator + cost (hidden when $0.00)
if [ "$COST_FMT" != '$0.00' ]; then
  OUT+="  ${C_SEP}|${RST}  ${C_COST}${COST_FMT}${RST}"
fi

printf '%b' "$OUT"
