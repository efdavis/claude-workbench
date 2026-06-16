---
description: Monitor PR/MR pipeline and auto-triage bot review comments
model: sonnet
argument-hint: "[PR/MR number or URL, or empty for current branch] [--iteration N]"
---

# Babysit PR/MR

Check a PR/MR's pipeline status and bot review comments. Auto-resolve noise, auto-fix mechanical issues, flag real concerns. Re-checks up to 3 times at 5-minute intervals until the pipeline passes and bot comments are triaged.

**Input**: `$ARGUMENTS` — PR/MR number, URL, or empty (detect from current branch). Optional `--iteration N` for internal re-check tracking.

---

## Step 1: Identify the PR/MR

Detect provider:
```bash
git remote get-url origin
```
- `github.com` → GitHub (`gh`)
- `gitlab.com` or self-hosted GitLab → GitLab (`glab`)

Parse `$ARGUMENTS`:
- Empty or `--iteration` only → detect from current branch
- Numeric → use as PR/MR number
- URL → extract number

### GitHub
```bash
BRANCH=$(git branch --show-current)
gh pr list --head "$BRANCH" --state open --json number,url,headRefName
```

### GitLab
```bash
BRANCH=$(git branch --show-current)
REMOTE_URL=$(git remote get-url origin)
PROJECT_PATH=$(echo "$REMOTE_URL" | sed 's|.*gitlab\.com[:/]\(.*\)\.git|\1|')
PROJECT_ENCODED=$(echo "$PROJECT_PATH" | sed 's|/|%2F|g')
glab api "projects/$PROJECT_ENCODED/merge_requests?source_branch=$BRANCH&state=opened"
```

Extract number and web URL. If no open PR/MR, tell the user and stop.

Parse iteration count from `--iteration N` (default: 1).

---

## Step 2: Check pipeline status

### GitHub
```bash
gh pr checks <number> --json name,state,link
```

### GitLab
```bash
glab api "projects/$PROJECT_ENCODED/merge_requests/<iid>/pipelines"
```

Get the latest pipeline. Note status: `success`/`failed`/`running`/`pending`/`canceled`.

---

## Step 3: Fetch bot discussions

### GitHub
```bash
gh pr view <number> --json reviews,comments
gh api "repos/<owner>/<repo>/pulls/<number>/comments"
```

### GitLab
```bash
glab api "projects/$PROJECT_ENCODED/merge_requests/<iid>/discussions"
```

Filter for bot comments:
- Author username contains `bot` OR starts with `group_` OR is a known reviewer bot (`dependabot`, `renovate`, `coderabbitai`, etc.)
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
   - GitHub: `gh api --method PATCH "repos/<owner>/<repo>/pulls/comments/<id>"` (or thread resolution via GraphQL)
   - GitLab: `glab api --method PUT "projects/$PROJECT_ENCODED/merge_requests/<iid>/discussions/<id>" -f resolved=true`

### 4d. For the summary note

Skip it entirely — informational, not actionable.

---

## Step 5: Report

```
## PR #<n> Babysit Report (check <N>/3)

Pipeline: <status>

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
- Bot comments exist AND pipeline is passing → triage is complete
- Iteration count >= 3 → max window reached
- Pipeline canceled/skipped → nothing to wait for

**Re-check** (schedule another run in 5 min):
- Pipeline still running/pending
- No bot comments yet

If re-checking:
```
Pipeline still running, no bot comments yet. Check <N>/3 — re-checking in 5 minutes.
```
Then invoke: `/loop 5m /babysit-pr --iteration <N+1>`

---

## Safety Rules

- Never force-push
- Never modify test assertions to make tests pass — fix the underlying code
- Max 3 auto-fix commits per invocation
- If the same comment reappears after a fix attempt, stop and flag it
- Max 3 total iterations of the monitoring loop
- Always show what was resolved/fixed (transparency)
