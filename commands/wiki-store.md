---
description: Add a source (URL, file, or paste) to a personal wiki and fold it into articles
argument-hint: "URL, file path, or pasted text"
---

# /wiki-store

Add a source to a personal LLM-maintained wiki and update the compiled articles.

**Input**: `$ARGUMENTS` — a URL, an absolute file path, or free text to ingest.

## Resolve the wiki directory (`$WIKI`)

- If `$CLAUDE_WIKI_DIR` is set, use it.
- Otherwise default to `~/wiki/`.
- If it doesn't exist, ask where the wiki lives, or offer to scaffold one (layout in `references/wiki-conventions.md`, or this repo's `commands/references/wiki-conventions.md`).

Read `$WIKI/CLAUDE.md` first if you haven't this session — it has the layout, frontmatter format, and hard rules. If there's no `CLAUDE.md`, fall back to `references/wiki-conventions.md`.

---

## Step 1: Identify and ingest the source

Parse `$ARGUMENTS`:

- **Looks like a URL** (starts with `http://` or `https://`) — use `WebFetch` to retrieve it. Save as `$WIKI/raw/web/<slug>.md` where `<slug>` is a kebab-case filename derived from the page title. If the page references images you'll cite, download them next to the markdown via `curl`.
- **Looks like an absolute file path** — copy (don't move) into the right `raw/` subdir: `.pdf` → `raw/papers/`, `.md`/`.txt` → `raw/web/` if it looks scraped, otherwise `raw/pasted/`. Keep the original filename (kebab-case it if needed).
- **Anything else** — treat as pasted text. Save as `$WIKI/raw/pasted/<YYYY-MM-DD>-<slug>.md` where slug is derived from the first line or content theme. If the user passed a multi-line argument, use it verbatim as the body.

Add frontmatter to the new raw file:

```yaml
---
source: <URL or "pasted" or original path>
fetched: <ISO date, today>
tags: []
---
```

Skip frontmatter for `.pdf` and binary files.

If a raw file with the same slug already exists, append a numeric suffix (`-2`, `-3`) rather than overwrite.

## Step 2: Read the new raw doc

Read the saved file (for PDFs, you can extract text via `pdftotext` if installed, otherwise note in the article that the source is a PDF and summarize from filename + user context). Identify:

- Main concepts the doc covers
- Key facts, definitions, claims worth capturing
- Any concepts the doc references that you don't recognize (candidates for new articles or web-search backfill — flag, don't fabricate)

## Step 3: Find relevant existing articles

```bash
cat "$WIKI/articles/INDEX.md"
ls "$WIKI/articles/"
```

Then grep article bodies for keyword overlap with the new doc:

```bash
grep -lirE "<keyword1|keyword2|...>" "$WIKI/articles/"
```

Read the candidates. Decide: which existing articles get updated, and what new articles (if any) are needed.

## Step 4: Update existing articles

For each existing article that gets new info from this source:

1. Add the new raw file path to the `sources:` list in frontmatter.
2. Update `updated:` to today's date.
3. Weave the new info into the body. Don't bolt it on as a "Update from <date>" section — integrate it as if you'd known it all along.
4. If the new source contradicts existing content, surface the conflict in the article (don't silently overwrite). Use a `> TODO:` line to flag for human review.

## Step 5: Create new articles

For each new concept worth its own article:

1. Pick a kebab-case filename. Check it doesn't collide.
2. Write the article per the style rules in `CLAUDE.md` (one-line definition, then context, then details).
3. Frontmatter: `title`, `tags`, `sources` (just the new raw file for now), `updated` (today).
4. Add `[[wiki-link]]` references to related existing articles. Visit those articles and add a backlink in the other direction so the linkage is bidirectional.

## Step 6: Update INDEX.md

If the article set changed (new articles, renamed articles, or significant theme shift):

1. Read the current `INDEX.md`.
2. Add new articles under the right theme heading. Create a new theme heading if none fits.
3. Update the `_Last updated:_` line.
4. Don't reorganize unrelated themes — that's `/wiki-compile`'s job.

## Step 7: Report

Print a short summary:

```
Stored: raw/<path>
Articles updated: <count> (<list>)
Articles created: <count> (<list>)
INDEX updated: yes/no
TODOs flagged: <count>
```

If you flagged any `TODO:` items, list them so the user can decide whether to act on them.
