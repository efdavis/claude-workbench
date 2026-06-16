---
description: Audit and rebuild the wiki — fix orphans, normalize, refresh INDEX
argument-hint: "(optional) topic or path to scope the sweep"
---

# /wiki-compile

Sweep a personal LLM-maintained wiki for drift and rebuild the index. This is the heavier counterpart to `/wiki-store`'s incremental updates.

**Input**: `$ARGUMENTS` — optional topic name or path. If omitted, full sweep.

## Resolve the wiki directory (`$WIKI`)

- If `$CLAUDE_WIKI_DIR` is set, use it.
- Otherwise default to `~/wiki/`.
- If it doesn't exist, ask where the wiki lives, or offer to scaffold one (layout in `references/wiki-conventions.md`, or this repo's `commands/references/wiki-conventions.md`).

Read `$WIKI/CLAUDE.md` first if you haven't this session. If there's no `CLAUDE.md`, fall back to `references/wiki-conventions.md`.

---

## Step 1: Inventory

```bash
ls "$WIKI"/raw/web/ "$WIKI"/raw/papers/ "$WIKI"/raw/pasted/ 2>/dev/null
ls "$WIKI/articles/"
```

Get a full picture of what's in `raw/` and what's in `articles/`. If `$ARGUMENTS` is set, limit the sweep to articles and sources that match it.

## Step 2: Audit raw → articles coverage

For each file in `raw/`, check whether it's listed in any article's `sources:` frontmatter:

```bash
grep -rE "raw/<subdir>/<filename>" "$WIKI/articles/"
```

Flag (don't auto-fix) raw files that aren't cited anywhere — they're either:

- Sources the user dropped in but `/wiki-store` was never run for, or
- Sources whose articles got deleted

List them in the report; ask the user whether to fold them in.

## Step 3: Audit `[[wiki-link]]` references

For each article, extract `[[link]]` references and verify each resolves to a real file in `articles/`:

```bash
grep -oE "\[\[[a-z0-9-]+\]\]" "$WIKI"/articles/*.md
```

For each link target:

- If the file exists, check the reverse direction — does the linked article link back? If not and a backlink makes sense, add one.
- If the file doesn't exist, flag it. Don't auto-create — ask the user whether to create a stub or remove the dangling link.

## Step 4: Fix orphans

Identify articles with **no inbound links** (nothing links to them) and **no outbound links** (they link to nothing). These are isolated.

For each orphan:

1. Read it.
2. Look for natural connection points to existing articles (shared concepts, references, themes).
3. Add `[[links]]` in both directions where appropriate.

If an orphan genuinely doesn't connect to anything else in the wiki, leave it — note it in the report. Some topics start isolated and grow connections later.

## Step 5: Normalize

Spot-check articles for convention drift:

- Filenames: kebab-case, no spaces, no underscores
- Frontmatter present and complete (title, tags, sources, updated)
- Style: leads with a one-line definition, no rambling intros
- Length: flag articles past 1500 words as split candidates

Fix the mechanical issues (filename casing, missing frontmatter fields with reasonable defaults). Flag the judgment calls (split candidates, style issues) for the user.

## Step 5b: Re-sync `canonical:`-stamped articles (optional)

Only relevant if you also keep a separate behavioral-rules or spec store *outside* the wiki — e.g. agent rule files, a coding-standards doc, a conventions file. In that setup, an article can mirror one of those external files and carry a `canonical: <path-to-source>` field in its frontmatter. The external file is the behavioral source of truth; the article is the navigable, self-contained view (so an Obsidian vault, which can't reach files outside itself, still renders the content).

If you don't keep such a store, skip this step entirely.

For each article with a `canonical:` field:

1. Read the referenced source file. If it's missing, flag the broken `canonical:` pointer — don't delete the article.
2. Compare the source's behavioral content (command tables, conventions, IDs) against the matching section of the article.
3. If they diverge, OR the source file's mtime is newer than the article's `updated:` date, re-fold the source's current content into the article's mirrored section. **Preserve the wiki-owned prose** — the why, the evolution, cross-links, and any section the article marks as wiki-owned. Never blind-overwrite the whole file.
4. Bump the article's `updated:` to today and list it in the report.

Sync is one-directional: source → article. **Never edit the source file from here.** If the article looks more correct than the source (the source may be stale), flag it for the user rather than propagate the stale content into the article.

## Step 6: Rebuild INDEX.md

Read every article's frontmatter and body. Group articles by theme — use existing themes from the current `INDEX.md` as a baseline, but feel free to reorganize if the article set has shifted.

Write the new `INDEX.md`:

```markdown
# Wiki Index

_Last updated: <today>_

## <Theme>

- [[article-slug]] — one-line gloss

## <Theme>

- ...
```

Glosses come from the article's lead sentence — not the title, not a TLDR you invent.

## Step 7: Suggest

Don't auto-execute these — surface them at the end of the report:

- **Consolidations**: two articles covering nearly the same concept (could be merged)
- **Splits**: articles past 1500 words covering multiple concepts
- **New article candidates**: concepts that come up across multiple articles but don't have their own page yet
- **Backfill targets**: `> TODO:` lines that could be resolved with a web search

## Step 8: Report

```
Articles audited: <count>
Raw files audited: <count>
Uncited raw files: <list>
Broken [[links]]: <list>
Articles re-synced from canonical source: <list>
Broken canonical: pointers: <list>
Backlinks added: <count>
Orphans fixed: <count>
Orphans remaining: <count>
INDEX rebuilt: yes
Suggestions for human review:
  - <consolidation/split/new article/backfill suggestions>
```
