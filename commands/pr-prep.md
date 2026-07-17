---
description: Run checks and generate a PR description, then commit + push + open the PR (GitHub)
---

# PR Prep

Run pre-PR checks and generate a terse PR description, then commit, push, and open the PR on GitHub (`gh`).

## Arguments

Parse `$ARGUMENTS` for flags:

| Flag | Effect |
|------|--------|
| `skip` | Skip all checks, jump straight to diff analysis + description |
| `submit` | Skip Steps 1-4, go straight to commit+push+PR |
| `draft` | Create the PR as a **draft** (`gh pr create --draft`) |

No flag = run all checks in parallel (default).

**Session awareness:** If checks were already run in this session, passed, and no code has changed since, skip Step 2 and go to Step 3. If any files were modified after the checks ran, re-run the affected checks.

## Step 1: Confirm Host

```bash
git remote get-url origin
```

Expect `github.com` (this command uses `gh`). If `gh` is not installed or the origin is not GitHub, stop after Step 4 (description only) and tell the user to open the PR manually.

## Step 2: Run Checks

**If `skip` flag is set → go directly to Step 3.**

Read `~/.claude/commands/references/check-procedures.md` (or this repo's `commands/references/check-procedures.md`) for auto-detected command patterns.

**CRITICAL: All checks MUST be submitted as parallel Bash tool calls in a single message.** Do not run one and wait before launching the others.

Default sequence (detect package manager + language):
- Format check (prettier/rustfmt/gofmt)
- Lint
- Test
- Typecheck

## Step 3: Analyze Changes

After checks pass (or if `skip`), analyze the diff:

```bash
# Detect base
git show-ref --verify --quiet refs/heads/main && BASE=main || BASE=master

# Current branch
git branch --show-current

# Diff vs base
git diff origin/$BASE...HEAD --stat
git diff origin/$BASE...HEAD

# Commits on this branch
git log origin/$BASE..HEAD --oneline
```

**If there are no commits ahead of base yet** (the work is still uncommitted — e.g. `pr-prep` was invoked right after `implement`/`code-review`, before any commit), analyze the working tree instead so the description reflects the real change:

```bash
git diff --stat HEAD
git diff HEAD
git ls-files --others --exclude-standard
```

The commit lands in Step 5; describe from whichever diff is non-empty.

## Step 4: Generate Description

Output a terse PR description in the chat window. **Do NOT commit or push anything.**

### Format

```
**Title:** <issue-id-if-any> <short description>

## Summary

<1-3 sentences: what this change does and why>

## Key Changes

- <behavioral change 1>
- <behavioral change 2>
- ...

## Deployment Notes

<Only if relevant: env vars, migrations, config changes. Omit entirely if none.>

## Follow-up

<Only if relevant: known follow-up work. Omit if none.>
```

### Rules

- **Title**: short, fits in one screen width. Start with issue ID if derivable from the branch name.
- **Summary**: Focus on **what** and **why**. Logically separate changes (different issues) get separate paragraphs.
- **Key Changes**: High-level behavioral changes only — what the user/system experiences differently.
  - NEVER mention specific file names, component names, or prop names — the reviewer reads the diff
  - NEVER mention translation key additions — noise
  - NEVER describe internal implementation details (e.g. "uses a Web Worker", "accepts an extraActions prop") — describe the *outcome*
  - 3-5 bullets max, one sentence each
- No checklists, no testing sections
- Fits in one screen

### Good vs Bad Examples

**Bad** (too much detail):
> `TransactionsList` now accepts an `extraActions` prop and forwards it to `FiltersBar`

**Good** (behavioral):
> Transaction pages can now render custom action buttons in the filter bar

**Bad** (noise):
> New translation keys: `transactions.exportSuccess`, `transactions.exportError`

**Good**: (just omit it entirely)

## Step 5: Commit

**If `submit` flag is set → use the description from the previous session (ask user to paste it) and start here.**

After the user reviews the description and says to proceed:

1. Show the list of changed files: `git diff --name-only` (include untracked from `git ls-files --others --exclude-standard`)
2. **Wait for user to confirm which files to stage.** Do not auto-stage everything.
3. **Re-verify the branch right before committing.** `git branch --show-current` must still equal the branch you started on. If it changed, a parallel process moved HEAD in this shared checkout — stop and surface it; never commit on a mismatched branch.
4. Format only the approved files: `prettier --write <files>` (or the relevant formatter)
5. **Stage explicit paths, never `git add -A`.** A primary checkout routinely carries *unrelated* uncommitted changes that `-A` would sweep into this commit (a fresh dedicated worktree is clean, so there the two are equivalent — but never rely on that). Stage the confirmed files by path, then confirm the staged set:
   ```bash
   git add <approved files>   # explicit paths, NOT -A
   git status                 # confirm: only the intended files are staged, nothing unrelated
   ```
6. Commit using the **Title** from Step 4 as the commit message

## Step 6: Push and Create PR

If the base moved while you worked, rebase before pushing so the PR is not immediately behind:

```bash
git fetch origin && git rebase origin/$BASE
```

```bash
git push -u origin <branch>
```

```bash
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary

<summary>

## Key Changes

<key-changes>

<optional sections>
EOF
)" --base <base-branch>
```

Add `--draft` when the `draft` flag was passed. Output the PR URL.

## Step 6.5: Embed Screenshots

Check if `/verify` saved screenshots for this branch:

```bash
ls "${SCREENSHOT_DIR:-$HOME/Projects/screenshots}/$(git branch --show-current)/" 2>/dev/null
```

**If screenshots exist:**

1. Tell the user: "Found N screenshot(s). Include in PR description?"
2. **Wait for confirmation.**
3. Upload:
   - Drag-and-drop is not available via CLI; use `gh api` to upload as attachments, or tell the user to drop them in the PR UI
4. Append a `<details><summary>Screenshots</summary>...</details>` section to the description and update.

**If no screenshots exist:** skip this step silently.

## Step 7: Review PR

Show the user the URL and confirm the description looks correct. **Wait for user confirmation.** The user may want to edit directly in the UI first.

## Step 8: Triage Bot Comments

After the PR is created (or after force push), check for automated bot comments and resolve ones that aren't real issues. See `/babysit-pr` for the full loop.

## User Checkpoints

There are 3 explicit checkpoints where you MUST stop and wait:

1. **After Step 4** — User reviews description, says "proceed" or requests edits
2. **After Step 5 file list** — User confirms which files to stage
3. **After Step 7** — User reviews the created PR

**Never skip a checkpoint.** Each one exists because the action that follows is visible to others or hard to reverse.

## Dashboard status (best-effort)

If the agent dashboard is installed, emit run status so it shows live. Resolve the emitter once; if absent, skip silently — an emit must never block or fail the run. Never hand-write JSON into the state dir; only the emitter writes snapshots.

```bash
EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "$EMIT" ] || EMIT="$(command -v emit-status.sh 2>/dev/null || true)"
```

With `TICKET` = the issue id/slug (from the branch, or the argument) and `SESSION="$TICKET-finisher"`, emit (skip all if `$EMIT` is empty):

- At Step 2 (checks running): `"$EMIT" --session "$SESSION" --role finisher --state reviewing --ticket "$TICKET" --note "pr-prep checks"`
- After Step 6 (PR created): `"$EMIT" --session "$SESSION" --role finisher --state pr-open --ticket "$TICKET" --pr "<N>" --note "PR #<N> open"`

The `finisher` row is continued by `/babysit-pr` (same `$TICKET-finisher` session) as it watches CI and flips to `merged`.

## Gotchas

- **Branch switch invalidates prior check results**: Re-run all checks if the branch changed.
- **format:write reformats unrelated files**: Use `prettier --write $(git diff --name-only --diff-filter=d)`.
- **No excuses in PR descriptions**: If something is missing (e.g., no screenshot), just omit it. Don't explain why.
- **Fix pre-existing errors proactively**: If checks reveal errors on the base branch, offer to fix them in this PR.
- **Suggest /babysit-pr after PR is created**: Bot review comments often appear within minutes.
