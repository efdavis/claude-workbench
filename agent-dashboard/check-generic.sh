#!/usr/bin/env bash
# check-generic.sh — fail if any project-specific / provider branding leaked into the repo.
#
# This toolkit is meant to be vendor-neutral and GitHub-native. This gate asserts that no
# work-specific names, no ex-employer branding, and no GitLab (`glab`) coupling survive. Run it
# after any edit; wire it into CI so genericness stays enforced.
#
#   ./check-generic.sh        # scan the repo, exit non-zero on any hit
set -u

root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
self="check-generic.sh"

# Denylist (case-insensitive, extended regex). These must NOT appear anywhere in the repo.
# Generic issue-tracker support (Jira/GitHub/free-form) is intentionally NOT denied — only
# GitLab-as-a-provider (`glab`) and work-specific branding are.
patterns='mcui|managed.custody|ripple|metaco|m3t4c0|rcus|devbox|harmonize|b2b-api|gas-station|\bglab\b|gitlab|MCUI_|\.mcui|MC_AUTH_GUARD|\bmc-[a-z]'

hits="$(grep -rniE "$patterns" "$root" \
  --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.docusaurus \
  --exclude-dir=build --exclude-dir=dist --exclude-dir=__pycache__ \
  --exclude="$self" 2>/dev/null)"

if [ -n "$hits" ]; then
  echo "FAIL: non-generic tokens found (this toolkit must stay vendor-neutral + GitHub-native):"
  echo
  echo "$hits"
  echo
  echo "Remove the branding / GitLab coupling above, or adjust the denylist in $self if a hit is a false positive."
  exit 1
fi

echo "ok: no project-specific branding or GitLab coupling found"
echo "     (scanned $root)"
exit 0
