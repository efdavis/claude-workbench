---
description: Q&A over a personal wiki with cited answers
argument-hint: "your question"
---

# /wiki-ask

Answer a question using a personal LLM-maintained wiki as the knowledge source.

**Input**: `$ARGUMENTS` — a free-form question.

## Resolve the wiki directory (`$WIKI`)

- If `$CLAUDE_WIKI_DIR` is set, use it.
- Otherwise default to `~/wiki/`.
- If it doesn't exist, say so and ask where the wiki lives, or offer to scaffold one (layout in `references/wiki-conventions.md`, or this repo's `commands/references/wiki-conventions.md`).

Read `$WIKI/CLAUDE.md` first if you haven't this session — it has the layout, frontmatter format, and hard rules. If there's no `CLAUDE.md`, fall back to `references/wiki-conventions.md`.

---

## Step 1: Find candidate articles

```bash
cat "$WIKI/articles/INDEX.md"
```

Identify candidate articles whose theme or title plausibly relates to the question.

Then grep for keyword overlap across articles and (if needed) raw sources:

```bash
grep -lirE "<keyword1|keyword2|...>" "$WIKI/articles/"
grep -lirE "<keyword1|keyword2|...>" "$WIKI/raw/"
```

Be generous with keywords — synonyms, related concepts, common phrasings.

## Step 2: Read

Read every candidate article in full. For articles that look central to the answer, also read the raw sources they cite (from the `sources:` frontmatter list). Stop pulling in raw sources once you have enough material — don't read everything indiscriminately.

If you find no relevant articles, say so plainly and ask whether the user wants to `/wiki-store` something on the topic first. Don't fabricate from general knowledge — the value of the wiki is that answers are traceable.

## Step 3: Answer

Write the answer in clear prose, with **inline citations**:

- Article citations: `[[article-name]]`
- Raw source citations: `(raw/web/foo.md)` or similar relative path

Example:

> Spaced repetition works because of the [[forgetting-curve]] — Ebbinghaus showed retention drops exponentially without review (raw/papers/ebbinghaus-1885.pdf). The optimal interval expands with each successful recall ([[expanding-rehearsal]]).

If the wiki only partially answers the question, say what's missing. Don't fill gaps with general knowledge silently — if you do supplement, mark it clearly: _(general knowledge, not in wiki)_.

## Step 4: Offer to file the answer

If the answer is non-trivial (more than a sentence or two of synthesis from the existing articles), end with:

> Want me to file this as `articles/<proposed-name>.md`?

**Do not auto-write.** Wait for the user to say yes.

If they say yes:

1. Create the article with frontmatter.
2. The `sources:` should include every raw file you cited.
3. Add `[[backlinks]]` to and from the existing articles you drew on.
4. Update `INDEX.md`.
5. Report what was filed.
