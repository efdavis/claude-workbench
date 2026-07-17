#!/usr/bin/env bash
# dash — the agent orchestra dashboard with live PR status. Runs the board plus the GitHub
# reconciler (overseer) together in one pane, so a row whose PR merged/closed shows the real
# status instead of a frozen pr-open. The overseer only UPDATES status; it never deletes. To
# clear a dead/merged row, hit `r` in the board (see the handler note in the README).
#
# The overseer's per-tick output is silenced so it never splatters the board's alt-screen
# render. Quitting the board (q / Ctrl-C) fires the trap, which tears the background overseer
# down with it — no orphaned reconciler, no standing daemon.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$here/overseer.py" >/dev/null 2>&1 &
ov=$!
trap 'kill "$ov" 2>/dev/null' EXIT

python3 "$here/dashboard.py"
