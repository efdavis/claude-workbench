---
description: Monitor a GitHub PR's pipeline and auto-triage bot review comments
model: sonnet
argument-hint: "[PR number or URL, or empty for current branch] [--iteration N]"
---

# Babysit PR

Check a PR's CI status and bot review comments. Auto-resolve noise, auto-fix mechanical issues, flag real concerns. Re-checks up to 3 times at 5-minute intervals until CI passes and bot comments are triaged. GitHub (`gh`).

**Input**: `$ARGUMENTS` — PR number, URL, or empty (detect from current branch). Optional `--iteration N` for internal re-check tracking.

---

## Step 1: Identify the PR

```bash
git remote get-url origin   # expect github.com
```

Parse `$ARGUMENTS`:
- Empty or `--iteration` only → detect from current branch
- Numeric → use as PR number
- URL → extract number

```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --state open --json number,url,headRefName
```

Extract the number and web URL. If no open PR, tell the user and stop.

Parse iteration count from `--iteration N` (default: 1).

---

## Step 2: Check CI status

```bash
gh pr checks <number> --json name,state,link
```

Note the overall status: `success`/`failed`/`running`/`pending`/`canceled`.

---

## Step 3: Fetch bot discussions

```bash
gh pr view <number> --json reviews,comments
gh api "repos/<owner>/<repo>/pulls/<number>/comments"
```

Filter for bot comments:
- Author username contains `bot` OR is a known reviewer bot (`dependabot`, `renovate`, `coderabbitai`, etc.)
- Not resolved
- Resolvable

Separate into:
- **Inline comments**: tied to a file path and line
- **Summary comment**: informational-only (often contains a fingerprint marker)

---

## Step 4: Evaluate and act on each comment

For each unresolved bot comment:

### 4a. Read context
- The comment body and any `suggestion` block
- The file + line it targets
- Read the actual code at that location on the current branch

### 4b. Classify and act

| Category | How to detect | Action |
|----------|--------------|--------|
| **Stale** | File/line no longer exists on branch (after force push or rebase) | Auto-resolve |
| **Summary note** | Informational, no actionable suggestion | Skip |
| **Noise** | Restates what code does, flags existing patterns used elsewhere, generic advice. >90% confidence not a real issue | Auto-resolve |
| **Valid + auto-fixable** | Lint fix, missing field, typo, lockfile regen. Bot often provides a `suggestion` block. >95% confidence AND fix is mechanical | Apply fix, run typecheck, commit, push, resolve |
| **Valid but complex** | Security concern, design question, logic issue. >90% real but non-trivial to fix | Flag for user with assessment |
| **Uncertain** | <90% confidence either way | Flag for user with the comment + analysis |

### 4c. For auto-fixes

1. Apply the suggested change (or derive the fix from the comment)
2. Run typecheck on the affected project
3. If types pass:
   ```bash
   prettier --write <file>   # or the relevant formatter
   git add <file>
   git commit -m "fix: resolve review comment in <filename>"
   git push
   ```
4. Resolve the discussion:
   ```bash
   gh api --method PATCH "repos/<owner>/<repo>/pulls/comments/<id>"   # or thread resolution via GraphQL
   ```

### 4d. For the summary note

Skip it entirely — informational, not actionable.

---

## Step 5: Report

```
## PR #<n> Babysit Report (check <N>/3)

CI: <status>

Bot comments:
- <N> auto-resolved (noise/stale):
  - "<1-line summary of each resolved comment>"
- <N> auto-fixed and pushed:
- <N> flagged for your review:
  - "<comment summary>" in <file>:<line>
    Assessment: <analysis>
    Confidence: <X>% real issue
```

---

## Step 6: Decide whether to re-check or stop

**Stop** (done):
- Bot comments exist AND CI is passing → triage is complete
- Iteration count >= 3 → max window reached
- CI canceled/skipped → nothing to wait for

**Re-check** (schedule another run in 5 min):
- CI still running/pending
- No bot comments yet

If re-checking:
```
CI still running, no bot comments yet. Check <N>/3 — re-checking in 5 minutes.
```
Then invoke: `/loop 5m /babysit-pr --iteration <N+1>`

---

## Dashboard status (best-effort)

If the agent dashboard is installed, emit run status so it shows live. Resolve the emitter once; if absent, skip silently — an emit must never block or fail the run. Never hand-write JSON into the state dir; only the emitter writes snapshots.

```bash
EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "$EMIT" ] || EMIT="$(command -v emit-status.sh 2>/dev/null || true)"
```

With `TICKET` = the issue id/slug (from the branch) and `SESSION="$TICKET-finisher"` (the same row `/pr-prep` opened), emit (skip all if `$EMIT` is empty):

- At Step 1 (PR identified): `"$EMIT" --session "$SESSION" --role finisher --state pr-open --ticket "$TICKET" --pr "<n>" --note "babysit check <N>/3"`
- When CI is green and comments are triaged (Step 6 stop): `"$EMIT" --session "$SESSION" --role finisher --state merged --ticket "$TICKET" --pr "<n>" --note "CI green — ready to merge"` (the human still does the merge)
- On a required gate / hard stop: `"$EMIT" --session "$SESSION" --role finisher --state escalated --ticket "$TICKET" --pr "<n>" --note "<reason>"`

---

## Safety Rules

- Never force-push
- Never modify test assertions to make tests pass — fix the underlying code
- Max 3 auto-fix commits per invocation
- If the same comment reappears after a fix attempt, stop and flag it
- Max 3 total iterations of the monitoring loop
- Always show what was resolved/fixed (transparency)
- Never merge — surfacing "ready to merge" is the ceiling; the merge is the human's
