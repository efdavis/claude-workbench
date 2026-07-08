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

### 2c. Diff self-audit (before pre-commit)

Audit the diff against the mechanism the plan committed to — a cheap safety net before the checks run:

```bash
git diff --stat
git diff
```

Check:
1. **Mechanism alignment** — compare the diff's shape to the plan's approach. If the delivered change is **broader** than planned (more files, more coupling, more surface than the plan described), stop and tell the user. Scope creep caught here is cheap; caught in review it isn't.
2. **Loose-type / unchecked-cast sweep** — grep changed files for the language's escape hatches (`: any` / `as any` / `as unknown as` in TS, `interface{}` in Go, `# type: ignore`, etc.). Replace with concrete types, or note why it's unavoidable.
3. **Dead-branch scan** — for each new conditional or fallback, is a branch unreachable given an earlier guard? (e.g. `if (x != null) { use(x ?? default) }` — the `?? default` is dead.)
4. **No hardcoded user-facing strings** where the repo uses i18n — use the existing translation mechanism.

Fix anything surfaced before Step 3.

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

Explicit checkpoints where you MUST stop and wait:

1. **Step 1c** — If existing branches found, ask what to do
2. **Step 2b** — If plan assumptions are wrong, propose adjustment and wait
3. **Step 2c** — If the diff is broader than the plan's mechanism, stop and surface it

## Dashboard status (best-effort)

If the agent dashboard is installed, emit run status at each phase so it shows live. Resolve the emitter once; if absent, skip silently — an emit must never block or fail the run. Never hand-write JSON into the state dir; only the emitter writes snapshots.

```bash
EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "$EMIT" ] || EMIT="$(command -v emit-status.sh 2>/dev/null || true)"
```

With `TICKET` = the issue id/slug (from `$ARGUMENTS` or the branch) and `SESSION="$TICKET-worker"`, emit (skip all if `$EMIT` is empty):

- Step 1 (branch created): `"$EMIT" --session "$SESSION" --role worker --state started --ticket "$TICKET" --worktree "$(git rev-parse --show-toplevel)" --note "branch <name>"`
- Step 2 (executing the plan): `"$EMIT" --session "$SESSION" --role worker --state implementing --ticket "$TICKET" --note "implementing"`
- Step 3 (all checks pass): `"$EMIT" --session "$SESSION" --role worker --state implementing --ticket "$TICKET" --note "checks green — ready for pr-prep"`
- On an unrecoverable stop: `"$EMIT" --session "$SESSION" --role worker --state escalated --ticket "$TICKET" --note "<reason>"`

## Gotchas

- **Check existing branches first**: Run `git branch --list "*<issue>*"` before creating a new one.
- **Rebase before push**: `git fetch origin && git rebase origin/<base>` before pushing. The base moves.
- **format:write reformats unrelated files**: Use `prettier --write $(git diff --name-only --diff-filter=d)` instead of whole-repo format.
- **Branch switch invalidates prior check results**: If you changed branches, re-run all checks.
- **Suggest /code-review when done**: Before moving to /pr-prep.
