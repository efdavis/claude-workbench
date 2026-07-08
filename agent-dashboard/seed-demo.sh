#!/usr/bin/env bash
# Seed a handful of demo snapshots so you can watch the dashboard populate without
# real agent runs. Writes to ${AGENT_DASHBOARD_STATE_DIR:-$HOME/.claude/agent-dashboard/state}
# with demo-* session ids (easy to clear). Then run ./run.sh in another pane.
#
#   ./seed-demo.sh          # seed
#   ./seed-demo.sh --clear  # remove the demo snapshots
set -u
here="$(cd "$(dirname "$0")" && pwd)"
E="$here/emit-status.sh"
dir="${AGENT_DASHBOARD_STATE_DIR:-$HOME/.claude/agent-dashboard/state}"

if [ "${1:-}" = "--clear" ]; then
  rm -f "$dir"/demo-*.json 2>/dev/null
  echo "cleared demo snapshots in $dir"
  exit 0
fi

"$E" --session demo-worker-23   --role worker   --state implementing --ticket PROJ-23 --model opus   --note "wiring the read-console query"
"$E" --session demo-finisher-27 --role finisher --state pr-open      --ticket PROJ-27 --pr 51 --model sonnet --note "CI green, awaiting review"
"$E" --session demo-reviewer-8  --role reviewer --state reviewing    --ticket PROJ-8  --model sonnet --note "code-review pass 2"
"$E" --session demo-pipeline-12 --role planner  --state waiting      --ticket PROJ-12 --model opus   --note "plan-review APPROVE — awaiting go"
"$E" --session demo-worker-14   --role worker   --state escalated    --ticket PROJ-14 --model opus   --note "pre-commit failed twice — needs a human"
"$E" --session demo-finisher-1  --role finisher --state merged       --ticket PROJ-1  --pr 48 --model haiku  --note "merged to main"

echo "seeded 6 demo snapshots in $dir"
echo "watch:  ./run.sh   (in another pane)"
echo "clear:  ./seed-demo.sh --clear"
