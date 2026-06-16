---
description: Lightweight code review — 2 parallel Sonnet agents (bug scan + convention check)
---

# Code Review

Perform a code review using 2 parallel Sonnet agents — one for bugs, one for conventions.

## Step 1: Detect Provider and Gather Diff

```bash
git rev-parse --show-toplevel
git remote get-url origin
```

Detect provider from remote URL:
- Contains `github.com` → **GitHub** (use `gh`)
- Contains `gitlab.com` or self-hosted GitLab → **GitLab** (use `glab`)
- Other → local-only; skip remote PR lookups

Gather the diff based on the argument:

**No argument — uncommitted changes:**

```bash
git status
git diff --staged
git diff
git ls-files --others --exclude-standard
```

**`previous` — HEAD~1:**

```bash
git show HEAD~1 --stat
git show HEAD~1
```

**`<SHA>` — specific commit:**

```bash
git show <SHA> --stat
git show <SHA>
```

**`<GitHub PR URL>` — remote PR:**

Extract the PR number and `owner/repo` from the URL, then:

```bash
gh pr diff <PR>   --repo <owner/repo>
gh pr view <PR>   --repo <owner/repo>
```

**`<GitLab MR URL>` — remote MR:**

Extract the MR IID and namespace from the URL, then:

```bash
glab mr diff <IID> --repo <namespace/project>
glab mr view <IID> --repo <namespace/project>
```

Use the remote `diff` command as the **sole source of truth** for what changed — the local branch may not match the remote branch. Only read local files for surrounding context that isn't in the diff.

**`<branch>` — branch vs base:**

Detect the base branch (default: `origin/main` if it exists, else `origin/master`; ask once if unclear).

```bash
git log <base>..<branch> --oneline
git diff <base>..<branch>
```

Also run: `git log --oneline -5`

Note the base branch used and the full diff — you'll pass both to the agents.

## Step 2: Launch 2 Parallel Sonnet Agents

Launch both agents **in a single message** (parallel). Use `model: sonnet` for each. Pass the full diff as context in each agent's prompt.

### Agent 1 — Bug & Security Scan

Focus: logic errors, async mistakes, null/undefined, type misuse, security gaps, N+1 queries, DRY violations.

Rules:

- Only flag what changed (ignore pre-existing issues)
- Ignore what linters/typecheckers catch (formatting, import order, missing types flagged by tsc)
- No nitpicks — only bugs a senior engineer would raise in a real review
- Flag DRY violations: 3+ near-identical blocks that differ only in a hook call, property path, or string literal are candidates for a factory, map, or shared helper. Suggest the pattern (factory, lookup table, etc.)
- Return: file path, approx line, description, severity (HIGH/MED/LOW)

Severity definitions:
- **HIGH** — correctness bugs, security vulnerabilities, data integrity risks. Would block a PR.
- **MED** — observability gaps, pattern violations, maintainability issues. Blocks unless deferred.
- **LOW** — style, naming, minor improvements. Non-blocking.

### Agent 2 — Convention & Pattern Check

Detect the stack from `package.json`, `Cargo.toml`, `go.mod`, etc. Also look for a `CLAUDE.md` or `.claude/rules/` directory for project-specific conventions.

Generic checks that apply across most stacks:

- No hardcoded secrets, URLs, or user-visible strings where i18n is in use
- No `any` / `interface{}` / unchecked casts unless justified
- Errors surfaced, not silently swallowed
- Consistent use of the repo's existing patterns (import aliases, module structure, logging, error handling)
- New code follows the style of its immediate surroundings

Rules:

- Only flag what changed
- If `CLAUDE.md` or `.claude/rules/` exist, prioritize rules from there
- Return: file path, approx line, description, severity (HIGH/MED/LOW)

## Step 3: Synthesize

Combine both agents' findings. Deduplicate overlapping issues. Output:

```markdown
## Code Review Summary

**Target:** [commit/branch/uncommitted/PR-url]
**Base:** [origin/main | origin/master | custom]
**Files Changed:** X files
**Lines:** +additions -deletions
**Change Type:** [feature/bugfix/refactor/etc.]
**Overall:** [EXCELLENT/GOOD/NEEDS_IMPROVEMENT/CONCERNING]

## Strengths

- [positive aspects]

## Issues Found

| #   | Severity     | File:Line | Issue       | Suggestion         |
| --- | ------------ | --------- | ----------- | ------------------ |
| 1   | HIGH/MED/LOW | path:123  | Description | Fix recommendation |

## Recommendations

- [actionable improvements]

## Next Steps

- [immediate actions / follow-ups]
```

**Overall ratings:**

- **EXCELLENT** — no HIGH or MED findings
- **GOOD** — LOW findings only
- **NEEDS_IMPROVEMENT** — has MED findings
- **CONCERNING** — has HIGH findings

## Gotchas

- **Deduplicate across agents**: Both parallel agents may flag the same issue. Merge duplicates in the synthesis step.
- **Pass full diff to verification agents**: Don't let agents re-explore the codebase. Include the diff text in the prompt so they verify claims from it directly.
- **Never auto-post review comments** to a remote PR/MR. Always show the user the draft and wait for explicit "post it".
