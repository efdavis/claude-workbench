# Wiki Maintainer Conventions

Operating manual for a personal LLM-maintained knowledge base. The `/wiki-store`, `/wiki-ask`, and `/wiki-compile` skills read this (or a `CLAUDE.md` copy of it placed at the wiki root). **You are the maintainer** — the user almost never edits articles by hand.

Drop a copy of this file at your wiki root as `CLAUDE.md` if you want it to auto-load when you work in that directory. Resolve the wiki path from `$CLAUDE_WIKI_DIR` (default `~/wiki/`).

## Layout

```
<wiki>/
├── raw/                 # source-of-truth documents — read-only for you
│   ├── web/             # web pages, articles (.md, often paired with images/)
│   ├── papers/          # PDFs and academic sources
│   ├── pasted/          # ad-hoc text/code pastes via /wiki-store
│   └── images/          # standalone images not tied to a single source
├── articles/            # the compiled wiki — yours to write and rewrite
│   ├── INDEX.md         # auto-maintained map of articles, grouped by theme
│   └── *.md             # one concept per file, kebab-case, flat
└── output/              # one-off derived artifacts (slides, plots, query answers)
```

## Hard rules

- **Never modify files in `raw/`.** They are the source of truth. If a raw doc is wrong, mention it in the relevant article rather than editing the source.
- **Articles are flat in `articles/`** with kebab-case filenames (e.g. `spaced-repetition.md`). Only nest into subdirs once a theme grows past ~20 articles.
- **Cross-link with `[[article-name]]`** (no `.md` suffix, no path). This stays Obsidian-compatible if Obsidian is added later, and is harmless plain text otherwise.
- **Cite your sources.** Every claim in an article should be traceable to a `raw/` file via the article's `sources` frontmatter list.
- **Link out to authoritative external sources** when they exist — official docs, papers, tickets, repos, vendor sites. Articles are navigational hubs; they should let the reader jump to the source of truth, not replace it.
- **When you don't know, say so.** Leave a `> TODO: ...` line in the article rather than fabricate. The user trusts the wiki because it's honest about gaps.

## What belongs in the wiki

**Promote to an article (evergreen):** concepts, architecture, domain knowledge, conventions that change rarely, terminology, comparisons. If it would still be true and useful in 12 months, it's evergreen.

**Keep in `raw/` only (transient or tooling-scoped):** slash commands, agent definitions, skills, in-flight project plans, ticket-specific handoff docs, sprint-scope todos, anything that names a specific PR/branch/sprint. These are real and useful but they churn — wiki articles for them go stale before they pay back the maintenance cost.

When in doubt, ask: would someone joining the project a year from now want this as a wiki article? Yes → write it. No → leave it in `raw/` where `/wiki-ask` can still pull it on demand.

## Frontmatter

### Articles (`articles/*.md`)

```yaml
---
title: Spaced Repetition
tags: [learning, memory]
sources:
  - raw/web/spaced-repetition-wikipedia.md
  - raw/papers/ebbinghaus-1885.pdf
updated: 2026-04-30
---
```

Optional `canonical: <path>` field — see "Mirroring an external rules/spec store" below.

### Raw docs (`raw/**/*.md`)

```yaml
---
source: https://en.wikipedia.org/wiki/Spaced_repetition
fetched: 2026-04-30
tags: [learning]
---
```

PDFs and images don't get frontmatter; the article that cites them carries the metadata.

## INDEX.md

`articles/INDEX.md` is your working map. Group articles by theme (your judgment) with a short one-line gloss for each. Refresh it whenever you add or rename an article. Full rebuild happens on `/wiki-compile`.

Format:

```markdown
# Wiki Index

_Last updated: 2026-04-30_

## Learning

- [[spaced-repetition]] — interval-based review schedules to combat forgetting
- [[active-recall]] — testing oneself rather than re-reading

## Memory

- [[ebbinghaus-forgetting-curve]] — exponential decay of retention over time
```

## Workflows

| Command | Purpose | What it touches |
|---------|---------|-----------------|
| `/wiki-store` | Add a source (URL, file, or paste); fold into articles | `raw/` (creates), `articles/` (creates/updates), `INDEX.md` |
| `/wiki-ask` | Q&A over the wiki with citations | reads only; offers (does not auto-write) to file the answer |
| `/wiki-compile` | Audit + rebuild indexes, fix orphans, normalize | `articles/` (rewrites), `INDEX.md` (rebuilds) |

## Style

- **One concept per article.** If an article keeps growing in unrelated directions, split it.
- **Lead with a one-line definition**, then context, then details. Skim-friendly.
- **Prefer prose to bullet salad** for the body, but use lists for enumerable things (steps, examples, sources).
- **Length**: typical article 200–800 words. If you're going past 1500, consider splitting.
- **Tone**: neutral, declarative, encyclopedic. No "we", no "let's".

## Mirroring an external rules/spec store (optional)

Only relevant if you also maintain a behavioral-rules or spec store *outside* the wiki (agent rule files, a coding-standards doc, a conventions file). In that case the wiki stays canonical for **domain knowledge** (what is true); the external store stays canonical for **behavioral specs** (what to do). Point across stores, never copy the body.

When an article mirrors an external rule, stamp `canonical: <path-to-source>` in its frontmatter and keep only the navigable layer here. `/wiki-compile`'s Step 5b re-syncs such articles one-directionally (source → article) and never edits the source.
