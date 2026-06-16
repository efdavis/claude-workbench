---
description: Check open PRs/MRs and pipeline status for the current repo (or a list of repos)
model: sonnet
---

# PR/MR Status

Query open PRs/MRs and pipeline status.

## Step 1: Detect Provider(s)

```bash
git remote get-url origin
```

- `github.com` → use `gh`
- `gitlab.com` or GitLab-like → use `glab`

Optionally, the user may pass a list of repos via `$ARGUMENTS`:
- `ownerA/repoA,ownerB/repoB` — query each one
- Empty → current repo only

## Step 2: Fetch Open PRs/MRs

### GitHub
```bash
gh pr list --state open --json number,title,author,headRefName,isDraft,reviewDecision,mergeable,url
```

With repos: `gh pr list --repo <owner/repo> --state open ...`

### GitLab
```bash
glab mr list -s opened
```

With repos: `glab mr list --repo <namespace/project> -s opened`

## Step 3: Check Pipelines

### GitHub
```bash
gh pr checks <number> --json name,state,conclusion  # per PR
```

Or a broader view:
```bash
gh run list --limit 10 --json name,status,conclusion,headBranch
```

### GitLab
```bash
glab pipeline list -s running
glab pipeline list -s failed
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

- **Never use `glab -F json`**: not a valid flag. Default text output + parse, or `glab api` with query params.
- **`--per-page` is not a flag for `glab api`**: use query-string syntax (`?per_page=20`).
- **`gh pr list` without `--state` defaults to open** — explicit is better for clarity.
