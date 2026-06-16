---
description: Create branch, execute implementation plan, run pre-commit checks
argument-hint: "<issue-id | slug>"
---

# Implement

Create a branch, execute a plan, and run pre-commit checks.

**Input**: `$ARGUMENTS` — an issue ID (`PROJ-123`, `#42`) or a short slug (`fix-login-redirect`).

---

## Step 1: Branch Setup

### 1a. Parse input

Extract a **branch slug**:
- If input looks like an issue ID (matches `^[A-Z]+-\d+$`, `^#?\d+$`): use it as the prefix, then derive a short description from the issue title (fetched via Phase 1 of `/plan` or directly).
- Otherwise: use the input as-is, normalized to kebab-case.

Final branch name: `<prefix>-<short-description>` (3-5 words, lowercase, hyphens).

### 1b. Determine repo root

```bash
git rev-parse --show-toplevel
```

Work from that directory. If unsure, ask the user.

### 1c. Check for existing branches

```bash
git branch --list "*<issue-id-or-slug>*"
git worktree list   # also check for stale worktrees
```

**If a matching branch exists:** Tell the user the branch name and how many commits ahead of base. Ask: reuse it, delete and recreate, or abort. **Wait for answer.**

**If no match:** proceed to 1d.

### 1d. Determine base branch

Default: `main` if it exists, otherwise `master`. Check:
```bash
git show-ref --verify --quiet refs/heads/main && echo main || echo master
```

If the project uses a different default (feature branch flow, trunk-based with `develop`, etc.), ask the user once and remember for the session.

Before branching, ensure the base is up to date:
```bash
git fetch origin
git checkout <base-branch>
git pull --ff-only origin <base-branch>
```

### 1e. Create branch

```bash
git checkout -b <branch-name>
```

Announce the branch name and base branch to the user.

---

## Step 2: Execute Implementation

### 2a. Find the plan

Check for an implementation plan in this order:

1. **Conversation context**: If a `/plan` was run earlier in this session, the plan is in context.
2. **Plan files**: `ls -t ~/.claude/plans/*.md | head -5` — look for one referencing this issue or slug.
3. **Project-specific**: Check `.claude/` in the repo for plans referencing the issue.

**If no plan found:** Ask the user what to implement. Do not guess.

### 2b. Execute the plan

Work through the plan step by step. For each step:

1. Read the files the step references to confirm they exist and match the plan's assumptions
2. Make the changes
3. Briefly note what was done before moving to the next step

If a step's assumptions are wrong (file moved, pattern changed, API different), stop and tell the user. Propose an adjustment. **Wait for confirmation** before continuing.

---

## Step 3: Pre-commit Checks

Read `~/.claude/commands/references/check-procedures.md` (or this repo's `commands/references/check-procedures.md`) for auto-detected command patterns.

Default sequence:
1. Format changed files only (prettier/rustfmt/gofmt on diff'd files — not the whole repo)
2. Run lint / test / typecheck in a single message (parallel Bash calls)
3. Fix any failures, re-run until clean
4. After all checks pass: "All checks pass. Ready for `/pr-prep`."

If the repo has a `package.json`, detect the package manager from the lockfile (`pnpm-lock.yaml` → pnpm, `yarn.lock` → yarn, `bun.lockb` → bun, else npm). For other languages, use the standard toolchain (cargo, go, etc.).

---

## User Checkpoints

Only 2 explicit checkpoints where you MUST stop and wait:

1. **Step 1c** — If existing branches found, ask what to do
2. **Step 2b** — If plan assumptions are wrong, propose adjustment and wait

## Gotchas

- **Check existing branches first**: Run `git branch --list "*<issue>*"` before creating a new one.
- **Rebase before push**: `git fetch origin && git rebase origin/<base>` before pushing. The base moves.
- **format:write reformats unrelated files**: Use `prettier --write $(git diff --name-only --diff-filter=d)` instead of whole-repo format.
- **Branch switch invalidates prior check results**: If you changed branches, re-run all checks.
- **Suggest /code-review when done**: Before moving to /pr-prep.
