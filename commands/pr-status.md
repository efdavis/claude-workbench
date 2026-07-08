---
description: Check open PRs and CI status for the current repo (or a list of repos)
model: sonnet
---

# PR Status

Query open PRs and CI status on GitHub (`gh`).

## Step 1: Scope

```bash
git remote get-url origin   # expect github.com
```

Optionally, the user may pass a list of repos via `$ARGUMENTS`:
- `ownerA/repoA,ownerB/repoB` — query each one
- Empty → current repo only

## Step 2: Fetch Open PRs

```bash
gh pr list --state open --json number,title,author,headRefName,isDraft,reviewDecision,mergeable,url
```

With repos: `gh pr list --repo <owner/repo> --state open ...`

## Step 3: Check CI

```bash
gh pr checks <number> --json name,state,conclusion  # per PR
```

Or a broader view:
```bash
gh run list --limit 10 --json name,status,conclusion,headBranch
```

## Step 4: Summarize

Output a table per repo:

```
## <owner/repo>

| #   | Title                       | Author | Status                      |
| --- | --------------------------- | ------ | --------------------------- |
| 42  | Add dark mode toggle        | you    | ✅ reviewed, ready to merge |
| 41  | Refactor auth middleware    | ali    | ⏳ awaiting review           |
| 40  | Fix flaky test              | you    | ❌ CI failed                  |
```

Traffic-light states:
- ✅ Ready to merge (approved + CI green)
- 👀 In review
- ⏳ Awaiting review
- ❌ CI failed
- 🔄 CI running
- 📝 Draft

## Gotchas

- **`gh pr list` without `--state` defaults to open** — explicit is better for clarity.
- **Scope `--json` fields**: unscoped output is noisy; request only the fields you render.
