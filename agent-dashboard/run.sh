#!/usr/bin/env bash
# Launch the agent dashboard. Ctrl-C to quit. Pure stdlib python3, no deps.
exec python3 "$(dirname "$0")/dashboard.py" "$@"
