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

## Step 2.5: Merge-readiness (behind-base drift)

A green pipeline and zero bot comments do NOT make a PR mergeable. If the base has moved and the repo requires branches to be up to date before merging, the PR is blocked until its branch is updated. Check the fine-grained state:

```bash
gh pr view <number> --json mergeStateStatus,mergeable,autoMergeRequest
```

**Read `mergeStateStatus`, not `mergeable`** — `mergeable` only reports conflicts (`MERGEABLE`/`CONFLICTING`), so it still says `MERGEABLE` when the branch is behind a protected base. `mergeStateStatus` carries the real reason:
- `BEHIND` — the branch is behind the base and "Require branches to be up to date before merging" is on. Needs an update before it can merge. Do NOT dismiss "only one commit behind" — a protected base blocks on it.
- `DIRTY` — merge conflicts with the base.
- `BLOCKED` — a required review/check/gate is unmet (not a branch-update problem; leave it, that's a human/CI gate).
- `UNSTABLE` — a non-required check is failing/pending.
- `CLEAN` — mergeable.

If `BEHIND` (no conflicts): update the branch with GitHub's server-side merge of the base — no local git, no force-push:
```bash
gh api --method PUT "repos/<owner>/<repo>/pulls/<number>/update-branch"
```
Wait for the new head, then re-check CI (the update retriggers it).

If `DIRTY` (conflicts): rebase locally in a **dedicated worktree** (`git worktree add <scratch-path> -B <branch> origin/<branch>`), never the main worktree — a shared HEAD races concurrent agents. Resolve docs/wording conflicts yourself; if a conflict touches code semantics or security-sensitive paths, STOP and flag for the user instead. After resolving, push with `--force-with-lease` (the one sanctioned force-push, see Safety Rules).

After any update or force-push, re-check auto-merge state (`autoMergeRequest`) — a new push can silently drop it. Report if it was armed before and is no longer; do NOT re-arm it yourself (merging needs the user's say-so).

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

**A PR that's behind its base is NOT done.** A green pipeline and zero bot comments do not make it mergeable if `mergeStateStatus == BEHIND` (protected base, out of date). Before you stop, re-run the Step 2.5 check: the base can drift again while CI runs, so if the branch is behind again, update it and re-check. Fast-moving bases can take several update rounds — keep going until it's not behind, subject to the iteration cap.

**Stop** (done) — ALL of these must hold:
- Branch is not behind the base (`mergeStateStatus != BEHIND`)
- CI is passing
- Bot comments are all triaged

...plus the hard exits (stop even if the above don't hold, but report exactly what's left unresolved):
- Iteration count >= 3 → max window reached
- CI canceled/skipped → nothing to wait for

**Re-check** (schedule another run in 5 min) if any of:
- Branch is behind → update now per Step 2.5, then re-check (an update retriggers CI)
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

## Gotchas

- **Stale bot comments after an update/rebase**: if a bot comment references a file/line that no longer exists post-update, it auto-resolves. Don't waste time investigating dead references.
- **Stale bot verification counters**: a review bot's non-resolvable "Rechecked N issues - N still open" notes can track already-resolved false positives and repost identically on every commit. Cross-check its issue-ids against actual thread resolution before treating the count as real; if the threads are resolved it's noise — report, don't chase.
- **`mergeStateStatus` is the merge-readiness field, not `mergeable`.** `mergeable` is coarse (`CONFLICTING`/`MERGEABLE` = conflicts only). `mergeStateStatus` carries the real reason (`BEHIND`, `BLOCKED`, `UNSTABLE`, `DIRTY`, `CLEAN`). Gate "done" on it.
- **The PR/discussions JSON can contain raw control characters** (from the body — embedded videos, newlines). A naive `json.load(...)` throws `Invalid control character`. Parse with `json.loads(text, strict=False)`.

---

## Safety Rules

- Never force-push, with one exception: `--force-with-lease` of the PR branch after a conflict-resolving rebase (Step 2.5)
- Never modify test assertions to make tests pass — fix the underlying code
- Max 3 auto-fix commits per invocation
- If the same comment reappears after a fix attempt, stop and flag it
- Max 3 total iterations of the monitoring loop
- Always show what was resolved/fixed (transparency)
- Never merge — surfacing "ready to merge" is the ceiling; the merge is the human's
