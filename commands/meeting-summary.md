---
description: Turn a meeting transcript or recording into a verified, decision-bearing digest (7-class extraction, every fact cited), gated and writing nothing until approved. Summarizes for action and audit, not recall. Optionally folds high-confidence entries into a wiki brain whose shape you configure once (any store, any format).
argument-hint: "[setup | meeting name/date | transcript path | paste] [--out <path>]"
---

# /meeting-summary

Turn a meeting transcript into a verified, decision-bearing **digest** - the 7-class extraction, every fact cited. Gated: shows the resolved transcript, then the digest, and writes nothing until you approve. The digest is the always-on deliverable. If you have set up a wiki brain (any store, any format), the skill also does the **skeleton work** to fold high-confidence entries into it.

This skill is repo-agnostic and store-agnostic. It does not assume a particular wiki technology, directory layout, or entry format - it learns your brain's shape from a one-time `setup`. It does not commit, push, or file issues. It stops at written files (plus an optional configured build); the human commits.

## Why it exists

Auto-summarizers optimize for recall ("what was discussed") and drop decision-bearing detail. Recurring failure modes they produce, all of which this skill is built to prevent:

* A speaker giving two different mechanism answers gets collapsed into one.
* Scope qualifiers get stripped, so a true-sounding sentence becomes misleading.
* Action items embedded mid-sentence get lost (only explicit "action:" lines survive).
* Precise values (TTLs, path strings, quorum policies, key grace periods, IDs) get rounded off or invented.

This skill summarizes for **action and audit** instead. It over-extracts, cites every fact to the transcript, keeps both sides of a fork, records unstated values as unstated, and never invents a number. The durable artifact is the digest; folding into a wiki brain is an optional, configured second step.

## Modes

* **`/meeting-summary setup`** - (re)configure the wiki brain this skill folds into. Interactive; writes `meeting-summary.config.md`.
* **`/meeting-summary [transcript]`** - the normal run. Resolves the transcript, builds the digest, gates it, optionally folds it.

If a run finds no config and the user did not ask for `setup`, it proceeds **digest-only** and notes once that `setup` can enable folding. Setup is never forced.

## Setup (one-time, per wiki brain)

Triggered by `/meeting-summary setup`, or offered the first time a run finds no config. Walk the user through the questions below, echo the resulting config back for confirmation, then write it to `<owning-root>/.claude/meeting-summary.config.md`. **Owning root** is the nearest ancestor of `store_dir` that is a project or vault root: the git root that contains the store, or for a non-repo vault (e.g. an Obsidian dir) the vault root itself - create `<vault>/.claude/` if absent. Write the config **inside the project or vault that owns the brain** - never into a global or shared `.claude/` you don't own. Never write the config without showing it first.

Capture these fields (a template is at the bottom of this file):

1. **Store + digest location**
   * `store_dir` - where compiled entries live (e.g. `docs/wiki/`, `wiki/articles/`, `notes/`). Relative to the owning root.
   * `digest_dir` - where the non-committed digest goes (e.g. `meetings/`, `notes/meetings/`). Create on first write.
2. **Entry format / frontmatter template**
   * `entry_template` - the exact skeleton a new entry must match: frontmatter fields and order, body shape. Pull this from an existing entry in `store_dir` if one exists, and confirm.
   * `slug_convention` - how an entry's filename is formed from its topic (e.g. kebab-case -> `<slug>.md` in `store_dir`). This is the single source for entry filenames and for the idempotency check; do not also bury a filename rule inside `entry_template`.
3. **Registration + build steps**
   * `register` - how a new entry gets registered, if at all (e.g. add the doc id to a sidebar config; add a link to an index). Unset or `none` -> the run lists registration as a manual TODO rather than editing anything.
   * `build_cmd` - a command to validate the store after writing (e.g. a docs-site build), or `none`. Set -> the run uses it as a gate; unset or `none` -> the run only lists manual TODOs.
   * `build_cwd` - the directory `build_cmd` runs in; omit for the repo root. Meaningless when `build_cmd` is unset/`none`.
4. **Canonical docs + names policy**
   * `canonical` - human-owned files the skill may *propose* edits to but must never auto-apply (e.g. a decision log, an ADR/ADD). Drift against these is presented at Gate 2 flagged distinctly.
   * `names_in_entries` - `yes` if the store is internal/access-gated and real names may appear in published entries; `no` to strip person names from entries (they stay in the digest only).

Config resolution: a run operates on one target store. Resolve its config in order: (1) `--config <path>` if the user passes one; (2) `<owning-root>/.claude/meeting-summary.config.md` for the store the run targets - when the run is for a specific repo/vault, *that* repo/vault's owning root is the target, not the raw working dir; (3) the nearest `.claude/meeting-summary.config.md` found walking up from the working dir. A repo/vault-level config always shadows any higher one. None found -> digest-only.

## Inputs and transcript resolution

Argument is optional. Resolve the transcript in this order:

1. **Meeting-recording MCP**, if a name/date was given and you have one configured (e.g. a Zoom MCP - tools typically namespaced with `Zoom` / `meeting`). Resolve by meeting name or date.
   * A numeric arg may be a meeting ID - try ID lookup first when the arg is all digits.
   * **Name/date collision**: list the candidate meetings and ask which one. Never guess.
   * **MCP absent / headless / auth miss**: do not hard-fail. Fall through to step 2.
2. **Explicit path** to a `.txt`, `.vtt`, or `.md` transcript.
3. **Pasted transcript** in the prompt.

If the source is `.vtt`, parse to `speaker + timestamp + text` lines before extraction. If `.txt`/`.md`, use as-is - **but first detect and set aside any non-dialogue preamble.** Some files are hybrids: a hand-written summary (frontmatter, or a "what was decided" header) sitting above the actual dialogue. Extract from the **dialogue only**; excluding a preamble summary stops a human's conclusions getting laundered back in as if the skill found them. Note at Gate 1 if the file carried a pre-baked summary.

**Always confirm the resolved transcript at Gate 1** (name, date, length, speaker list) before extraction, so an MCP mis-resolution is caught before any work is shown as fact.

## Pipeline

1. Resolve the transcript (above). Normalize VTT to speaker + timestamp lines. Set aside any preamble.
2. **Extract** against the 7 classes (below), then **verify every citation** (see "Citation verification") so nothing reaches Gate 1 unverified.
3. **Segment into topics.** A meeting is rarely one topic. Produce one candidate per *durable* topic, not one per meeting.
4. **(Config only) Classify each candidate against the store.** Scan `store_dir` and the configured `canonical` docs; tag each candidate `new entry`, `drift edit` (store already covers it and the transcript supersedes/sharpens), or `corroboration` (store agrees; no write). No config -> skip this step.
5. **Gate 1**: show the resolved-transcript header, the verified digest, the topic segmentation, and (if configured) each candidate's classification with the store doc it overlaps. Human corrects and confirms.
6. **Render** the digest. If configured, render the approved entries in `entry_template` format and draft any drift edits as diffs.
7. **Gate 2**: show the digest, plus (if configured) each rendered entry and every drift edit as a diff (canonical-file edits flagged distinctly). Human approves per item.
8. **Write**: digest to its destination (per the Destination order below: `--out`, else `digest_dir`, else paste); then, if configured, write approved entries to `store_dir`, apply approved drift edits, do `register`, run `build_cmd` as a gate (or list manual TODOs). Stop. Do not commit.

## The 7 extraction classes

Extract every instance. When in doubt, over-extract - Gate 1 is where the human prunes.

| # | Class | What to capture | Failure it prevents |
|---|---|---|---|
| 1 | **Decisions + why** | The decision *and* the stated rationale. Name the decider when stated. | "What was decided" without "why" rots into an un-revisitable mystery. |
| 2 | **Open questions + owner** | The unresolved question, who owns resolving it, any deadline. | Open items read as settled. |
| 3 | **Actions + owner** | Action item, owner, due/trigger. Catch ones embedded mid-sentence, not just explicit "action:" lines. | Embedded asks (e.g. "include X in the review") get dropped by recall summaries. |
| 4 | **Scope / conditions** | Qualifiers that bound a statement ("for internal users only", "in MVP", "doesn't have to be X specifically"). | A stripped qualifier turns a true statement into a misleading one. |
| 5 | **Forks (two-answer cases)** | When a speaker (or two) give two different mechanism answers, keep **both**, attributed, marked unresolved if unresolved. | Collapsing a fork into one answer hides the real ambiguity. |
| 6 | **Precise values** | TTLs, path strings, quorum policies (M-of-N), key grace periods, role codes, dates, IDs - exactly as stated. | Rounding or inventing a number. **If a value is referenced but not stated (e.g. "a certain TTL"), record it verbatim as unstated - never supply a number.** |
| 7 | **Verbatim + timestamp** | For anything touching **auth, money movement, or a third party**, capture the exact quote + timestamp. | Paraphrase drift on the highest-stakes statements. |

## Citation verification

Before anything is shown at Gate 1, confirm each extracted item's citation actually supports it. The source is prose, often auto-transcribed, so:

* Match **fuzzy / substring**, not exact string. Line numbers drift and quotes are lightly cleaned; the test is "does the cited line or its neighbors carry this content," not byte-equality.
* Class 7 (verbatim) items must appear at the cited line with only transcription-noise differences. If not, fix the citation or drop the item.
* Any item whose citation cannot be confirmed is **dropped or flagged `[unverified]` at Gate 1**, never shown as established fact.

A hallucinated citation in an audit artifact is worse than a missing one. This check is cheap insurance.

## The digest (always-on artifact)

The digest is the deliverable every run produces, with or without a configured store.

* **One digest per meeting.** Header: meeting name, date, source (meeting ID / path), speakers, link to the transcript. Body: the 7-class extraction with per-fact citations and a HIGH/MED/LOW confidence stamp on each decision.
* A short **action-led summary** at the top (decisions, open questions, actions - what a reader acts on), then the full classed extraction below it.
* **Destination**: `--out <path>` if given; else `digest_dir` from config; else paste it back in chat and offer to write it. The digest is working/audit material - it carries names, verbatim quotes, and lower-confidence items, so it is **not committed** by the skill even when a store is configured.

## Folding into a wiki brain (only when configured)

When a config resolves, the skill does the skeleton work to add the meeting's durable, high-confidence topics to the store. It uses the config; it assumes nothing about the store's technology.

**Coverage / drift (generalized).** For each candidate topic, scan `store_dir` and `canonical` before drafting:

* **Already covered** -> a **drift edit** (transcript supersedes/sharpens existing content; render as a diff) or **pure corroboration** (transcript agrees; no write, noted at Gate 1). Default here when unsure - a new entry must clear "nothing in the store already states this."
* **Net-new and durable** -> a **new entry** in `entry_template` format.
* A hedge is a stop signal, not a publishable caveat. If a draft wants to say "this likely lives elsewhere, verify there," do the lookup now; if it is there, it is a cross-reference or drift, not a new entry.

**Publish bar.** A candidate becomes a written entry only if all three hold; fail any and it stays in the digest, surfaced at Gate 1 as `NEEDS RESEARCH before publish: <what to verify, and where>`:

1. **Clean and fact-led** - leads with the fact, no meeting/date/attendee preamble, no hedge words ("likely", "probably", "seems", "appears", "TBD").
2. **High confidence (>= 90%)** on the central claim. A localized sub-point may carry its own lower stamp only if the core is HIGH.
3. **Not already covered** in the store / canonical.

**Entry shape.** Match `entry_template` exactly; filename per `slug_convention`. Lead with the fact, not the meeting. Add a `Confidence:` line after the lead. Link to canonical, never restate it. Keep session-ledger cruft (dates, attendee lists, "transcript-sourced") out of the body - that lives in the digest. Apply the `names_in_entries` policy: when it is `no`, strip person names (full names, @handles, initials standing in for a person) from the rendered entries - they survive only in the digest - and **at Gate 2 confirm no person name leaked into any entry before writing.** The digest always keeps names regardless.

**Canonical guard.** Files listed in `canonical` are human-owned. The skill may *draft* edits to them but must (a) present them at Gate 2 flagged distinctly, and (b) never apply one the human did not approve item-by-item. When unsure on a canonical file, propose an open-question note instead of an edit. Two edits to the same line (one fact, two files) are presented as one merged proposal, never applied blindly.

## Write, build, rollback

Order in step 8, and the build gate:

1. Write the digest (per "Destination" above).
2. (Config only) Write approved entries to `store_dir`; apply approved drift edits.
3. (Config only) Do `register` if specified and mechanical (e.g. insert the doc id into a sidebar config; the id must equal the store-relative path with no extension and not duplicate an existing one). If `register` is unset/`none`, list it as a manual TODO.
4. (Config only) If `build_cmd` is set, **run it as a gate** (in `build_cwd`, else the repo root). On failure, the working tree is dirty and the "stops at green build" guarantee is broken: either fix the cause (bad markup, broken cross-link, registration mismatch) and rebuild, or stop and enumerate the files written this run, **split into two lists**: (a) store-side files the human can safely revert - new/edited entries and any `register` edit (sidebar config and friends, the most likely build-break cause); (b) the digest, which lives outside the store and is the run's keepable artifact - flag it as "keep, do not revert." If `build_cmd` is unset/`none`, list the manual validate/commit steps instead.
5. Stop at written files (plus green build if configured). The human commits and opens the PR/MR.

## Gates (never skip)

* **Gate 1 - facts.** Resolved-transcript header + verified digest + topic segmentation + (if configured) per-candidate classification and the store doc each overlaps. Nothing unverified is shown as fact. Human corrects, then confirms.
* **Gate 2 - writes.** The rendered digest, plus (if configured) each entry and every drift edit as a diff, canonical edits flagged distinctly. Human approves per item before any write.

## Idempotency (re-running the same meeting)

* **Digest**: if one already exists at the destination, **skip the write only when the new digest is byte-identical** to it; any difference means this is a corrected re-run and gets a new slug (`<date>-<slug>-digest-corrected.md`) rather than editing in place. Never silently drop a correction by treating "exists" as "done."
* **Entry**: if the target entry already exists, this is a **re-compile** - bump any `last_compiled`/date field, show the full old-to-new diff at Gate 2, never silently overwrite.

## Quality bar (self-test before trusting it on a new corpus)

Run two checks on a representative transcript before relying on the digest:

* **Invention test.** Find a place where a value is referenced but not stated (e.g. "usable for a certain TTL"). Pass = the digest records it verbatim as unstated and supplies no number. A digest that invents one fails - this is exactly what class 6 exists to prevent.
* **Fork test.** Find a place where two mechanism answers were given. Pass = both sides are kept and attributed, not collapsed.

If either fails, tune the extraction before trusting the skill - do not ask it to surface a fact whose source is not in the input (that trains hallucination).

## Defaults you can change

* `names_in_entries` defaults from setup; digest always keeps names.
* Every decision in the digest carries a HIGH/MED/LOW stamp + source line/timestamp.
* One entry per durable topic, not one entry per meeting.
* The skill never commits, pushes, or files issues. Action items live in the digest; route them to your tracker separately if you want them filed.

---

## Config template

`/meeting-summary setup` writes a filled-in copy of this to `<owning-root>/.claude/meeting-summary.config.md`.
Hand-edit any time, or re-run setup. Delete it (or never create it) to run digest-only.

### Docs-site example (sidebar + build)

```yaml
store_dir: docs/wiki/            # where compiled entries live
digest_dir: meetings/            # where the non-committed digest is written
slug_convention: kebab-case from the topic     # filename = <slug>.md in store_dir
entry_template: |
  ---
  title: <human title>
  description: <one to two sentences, the entry's thesis>
  tags: [<kebab tags from your vocabulary>]
  sources:
    - <canonical files this entry links>
    - 'meeting: <name> (<id>)'
  last_compiled: <YYYY-MM-DD>
  status: draft
  ---
  # <title>
  Confidence: <stamp on the core fact>
  <fact-led body, links to canonical, no meeting/date/attendee preamble>
register: <how a new entry's id is added to the site nav>   # or none
build_cmd: <docs-site build command>                        # or none
build_cwd: <dir build_cmd runs in>                          # omit for repo root
canonical:                       # human-owned; skill proposes edits, never auto-applies
  - docs/decisions.md
names_in_entries: yes            # internal/gated store; real names allowed in entries
```

### Obsidian / plain-folder example (no build, no sidebar)

A vault that is just markdown files - no registration step, no build command.

```yaml
store_dir: articles/             # vault-relative
digest_dir: meetings/
slug_convention: kebab-case from the topic
entry_template: |
  ---
  title: <human title>
  tags: [<your vocabulary>]
  source: 'meeting: <name> (<id>)'
  ---
  # <title>
  Confidence: <stamp>
  <fact-led body, [[wikilinks]] to related notes>
register: none                   # vault auto-discovers files; nothing to register
build_cmd: none                  # no build; entries are valid the moment they're written
canonical: []                    # nothing human-owned/auto-edit-protected here
names_in_entries: yes            # private vault
```

### Minimal config

Only `store_dir` and `digest_dir` are required to enable folding. Everything else falls back:
no `entry_template` -> infer from an existing entry or use plain frontmatter + H1; `register`/`build_cmd`
absent -> listed as manual TODOs; no `canonical` -> nothing is auto-edit-protected (all overlaps become
drift diffs); `names_in_entries` absent -> defaults to keeping names in the digest only.

```yaml
store_dir: wiki/articles/
digest_dir: meetings/
```
