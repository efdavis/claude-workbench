# Pre-commit Check Procedures

Shared by `/implement` and `/pr-prep`. Always run **all checks in a single message** (parallel Bash calls).

## Detect Package Manager / Toolchain

Inspect the repo root:

| File | Tool |
|------|------|
| `pnpm-lock.yaml` | pnpm |
| `yarn.lock` | yarn (classic or berry — check `package.json#packageManager`) |
| `bun.lockb` | bun |
| `package-lock.json` | npm |
| `Cargo.toml` | cargo |
| `go.mod` | go |
| `pyproject.toml` + `uv.lock` | uv |
| `pyproject.toml` + `poetry.lock` | poetry |
| `requirements.txt` | pip |

Also check for `CLAUDE.md` in the repo root for project-specific overrides.

## Formatting (always first)

Run `git diff --name-only --diff-filter=d` to get changed files, then format only those:

```bash
# Node/TS
npx prettier --write <file1> <file2> ...

# Rust
cargo fmt -- <file1> <file2> ...

# Go
gofmt -w <file1> <file2> ...

# Python
ruff format <file1> <file2> ...
```

Do NOT run whole-repo format commands — they reformat unrelated files due to tool-version drift.

## Node / TypeScript

Prefer whatever scripts the repo exposes. Common patterns:

```bash
<pm> run lint
<pm> run test
<pm> run typecheck    # or: npx tsc --noEmit
```

For monorepos (nx, turborepo, etc.), use their "affected" / "changed-only" variants. Examples:

### Nx
```bash
<pm> nx affected -t lint --uncommitted
<pm> nx affected -t test --uncommitted
<pm> nx affected -t check-types --uncommitted
```

Committed changes:
```bash
<pm> nx affected -t lint --base=origin/<base> --head=HEAD
```

### Turborepo
```bash
<pm> turbo run lint --filter=...[origin/<base>]
<pm> turbo run test --filter=...[origin/<base>]
<pm> turbo run typecheck --filter=...[origin/<base>]
```

## Rust

```bash
cargo check
cargo clippy -- -D warnings
cargo test
```

## Go

```bash
go vet ./...
go test ./...
```

## Python

```bash
ruff check .
pytest
mypy .   # if configured
```

## Fixing failures

- **Lint/format**: fix automatically, re-run
- **Type errors**: fix the code, re-run
- **Test failures**: investigate root cause, fix, re-run

## Gotchas

- **Install deps before checks** if the lockfile changed — otherwise you'll get confusing missing-module errors
- **Branch switch invalidates prior results** — re-run after a `git checkout`
- **Whole-repo `format:write` causes drift** — always format only changed files
