# claude-workbench

Personal, vendor-neutral versions of my Claude Code skills and agents. Lifted from a work-specific setup and stripped of work context so they're reusable in any project.

## Layout

```
commands/         # slash commands (skills) + references/ shared docs — see Skills below
agents/           # sub-agents — see Skills below
agent-dashboard/  # live terminal view of agent runs + the emit helper (see its README)
cmux/             # my cmux (Ghostty-based terminal) UI + settings (see its README)
wiki-site/        # Docusaurus shell that renders a personal Markdown wiki (see its README)
theme/            # reusable color palette (National Park Poster), the wiki UI look
```

## Skills

Slash commands live in `commands/`, sub-agents in `agents/`. The authoritative description for each is the `description:` in its own frontmatter; the tables below are a curated index for discovery.

### Plan & build

| Skill | What it does | Needs |
|-------|--------------|-------|
| [plan](commands/plan.md) | Deep-investigate an issue and produce an implementation plan | issue tracker (auto-detected) |
| [plan-review](commands/plan-review.md) | Staff-engineer review of the current/most recent implementation plan | — |
| [implement](commands/implement.md) | Create branch, execute the plan, run pre-commit checks | — |
| [ship](commands/ship.md) | Drive one issue through the whole chain (plan → review → implement → review → draft PR → babysit) with a stop-and-ask gate at every human decision | `gh` |
| [grill-me](commands/grill-me.md) | Stress-test a plan/design with pointed questions on assumptions and risks | — |

### Review & PR

| Skill | What it does | Needs |
|-------|--------------|-------|
| [code-review](commands/code-review.md) | Lightweight review: 2 parallel Sonnet agents (bug scan + convention check) | — |
| [verify](commands/verify.md) | Visual verification of UI changes via Playwright | Playwright MCP |
| [pr-prep](commands/pr-prep.md) | Run checks, generate a PR description, then commit + push + open the PR | `gh` |
| [babysit-pr](commands/babysit-pr.md) | Monitor a PR's CI and auto-triage bot review comments | `gh` |
| [pr-status](commands/pr-status.md) | Check open PRs and CI status for one or more repos | `gh` |

### Knowledge base & memory

| Skill | What it does | Needs |
|-------|--------------|-------|
| [learnings](commands/learnings.md) | Extract durable learnings from the session and update memory | `CLAUDE_MEMORY_DIR` |
| [meeting-summary](commands/meeting-summary.md) | Transcript -> verified, cited digest; optionally folds into a configured wiki brain | — |
| [wiki-store](commands/wiki-store.md) | Ingest a source (URL, file, paste) and fold it into wiki articles | `CLAUDE_WIKI_DIR` |
| [wiki-ask](commands/wiki-ask.md) | Cited Q&A over the personal wiki | `CLAUDE_WIKI_DIR` |
| [wiki-compile](commands/wiki-compile.md) | Audit and rebuild the wiki (fix orphans, normalize, refresh INDEX) | `CLAUDE_WIKI_DIR` |

### Misc

| Skill | What it does | Needs |
|-------|--------------|-------|
| [infographic](commands/infographic.md) | Turn dense research/data into a trustworthy single-file HTML infographic (fact-checked) | — |

### Sub-agents (`agents/`)

| Agent | What it does |
|-------|--------------|
| [staff-engineer](agents/staff-engineer.md) | Skeptical reviewer of implementation plans (read-only; verifies plan claims) |
| [issue-lookup](agents/issue-lookup.md) | Bulk issue-tracker fetcher; detects Jira/GitHub and fetches accordingly |

Shared reference docs used by several skills live in [`commands/references/`](commands/references/) (check-procedures, slack-formatting, wiki-conventions).

## Agent dashboard

[`agent-dashboard/`](agent-dashboard/) is a live terminal view of, and index into, your agent runs. Leave `agent-dashboard/run.sh` open in one pane and watch `implement` / `code-review` / `pr-prep` / `babysit-pr` (or a whole `/ship`) work in others — which issue, which role, what state, how long, and which ones need you. It observes and navigates (a row cursor + one-key jump to attach a live run or replay a finished one); you drive the runs. Pure stdlib Python 3, no daemon, no `pip install`. `dispatch.sh` is the optional spawn half: claim an issue, cut a `git worktree`, and launch a detached lane the dashboard then tracks.

The commands emit status best-effort at each phase (their "Dashboard status" sections). For them to find the emitter from inside your project, put it on `PATH` or set `AGENT_DASHBOARD_HOME`:

```bash
ln -s ~/Projects/claude-workbench/agent-dashboard/emit-status.sh ~/.local/bin/emit-status.sh
# or: export AGENT_DASHBOARD_HOME=~/Projects/claude-workbench/agent-dashboard
```

A teammate who hasn't set this up sees zero change — the emit calls no-op. See [`agent-dashboard/README.md`](agent-dashboard/README.md) for the full story (`overseer.py` GitHub reconciler, env vars, wiring table). `/ship` ties the whole chain together and lights up one journey row per issue.

## Install

Nothing fancy. Two options:

**Option 1: Cherry-pick into `~/.claude/`** — copy or symlink the files you want.

```bash
# Symlink individual skills
ln -s ~/Projects/claude-workbench/commands/plan.md ~/.claude/commands/plan.md
ln -s ~/Projects/claude-workbench/commands/learnings.md ~/.claude/commands/learnings.md
# ... etc
```

**Option 2: Symlink whole folders** — if you don't have conflicting files in `~/.claude/commands/` or `~/.claude/agents/`:

```bash
ln -s ~/Projects/claude-workbench/commands ~/.claude/commands
ln -s ~/Projects/claude-workbench/agents ~/.claude/agents
```

`wiki-site/` is a standalone Docusaurus app, not a skill — `cd wiki-site && npm install && npm start`. See `wiki-site/README.md`. It renders whatever Markdown wiki `CLAUDE_WIKI_DIR` points at (the same var the `wiki-*` skills use); ships with bundled example content so it boots before you wire up your own.

`cmux/` is terminal config, not a skill — my [cmux](https://cmux.com) UI and settings (theme, font, sidebar, dark chrome). Symlink the files into `~/.config/` and run `cmux reload-config`. See `cmux/README.md`.

`theme/` is just CSS — the color palette behind the wiki UI, pulled out so you can reuse it in any project (Docusaurus or otherwise). See `theme/README.md`.

## Conventions

- Skills that touch PRs/CI use `gh` (GitHub) as the source-control host. If it's absent, they fall back to read-only mode or ask.
- Issue IDs are accepted as free-form strings. Skills don't assume Jira — they detect the shape (`PROJ-123`, GitHub `#123`, URL) and dispatch accordingly.
- Branch naming: `{ISSUE}-{slug}` where `ISSUE` is the issue key if present, otherwise a user-supplied slug. No forced prefixes.
- Memory path override: skills that read/write memory assume `~/.claude/projects/<project-slug>/memory/`. Override with `CLAUDE_MEMORY_DIR`.
- Wiki path override: the `wiki-*` skills assume a personal knowledge base at `~/wiki/`. Override with `CLAUDE_WIKI_DIR`. They scaffold/read against `commands/references/wiki-conventions.md` (layout, frontmatter, rules) — drop a copy at the wiki root as `CLAUDE.md` to auto-load it.
- No Slack, no Jira MCP, no internal-tooling assumptions. If a skill benefits from external integrations, the skill body says "if you have X configured" - it does not assume.

## Originals

These are generalized copies of skills I use in my work repo. The originals live outside this repo and stay tuned for my work setup's specifics. Edits here should never import work-specific context back in.
