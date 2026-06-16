---
description: Extract durable learnings from the current session and update memory
---

# Session Learnings

Review this conversation for durable learnings worth persisting to memory. Be ruthlessly selective. Most sessions produce 0-3 genuine learnings. Noise in memory causes future sessions to miss important signals.

## Prerequisites (read once)

This skill assumes a file-based memory model:

- a memory directory with **one markdown file per fact**, each with frontmatter (`name`, `description`, `type`)
- a `MEMORY.md` **index** at the root of that directory (one line per memory) that gets loaded into context each session
- `type` is one of `feedback` (how to work), `project` (ongoing work), `reference` (external pointers), `user` (who you are)

If you don't run this model yet, Step 1 will offer to create it. The `~200`-line index budget and the `type` vocabulary are sensible defaults, not law — tune them to your setup.

## Step 1: Load + Scan

Read the memory index and scan the conversation.

1. Resolve the memory directory:
   - If `$CLAUDE_MEMORY_DIR` is set, use it.
   - Otherwise, look for `~/.claude/projects/*/memory/MEMORY.md` matching the current working directory slug (encoded as `-path-to-dir`).
   - If none is found, ask the user where memory lives (or offer to create it).
2. Read `MEMORY.md` from that directory. Count the lines. Note if at or over 200.
3. Build a mental map of what topics are already covered.

Then scan the conversation for these categories:

| Category | Look for |
|----------|----------|
| **Corrections** | User said "no", "don't", "stop", "wrong", or rejected a tool call |
| **Tool/API gotchas** | Something failed in a surprising way (CLI flag that doesn't exist, MCP behavior, quirks of an SDK) |
| **Process validations** | User confirmed a non-obvious approach worked ("yes exactly", "perfect", accepted without pushback) |
| **External system details** | Field IDs, URLs, auth patterns, board IDs not derivable from the codebase |
| **Skill/workflow gaps** | A skill template produced wrong behavior, or a workflow step is missing |

## Step 2: Filter

For each candidate, ask these questions. If ANY answer is "yes" to the first four, drop it:

1. **Derivable from code, git log, or CLAUDE.md?** Skip it.
2. **Already in memory and still accurate?** Skip it.
3. **Ephemeral?** One-time debugging steps, task-specific details, transient state. Skip it.
4. **Too vague to act on?** "Be more careful" is noise. A specific rule with conditions is signal. Skip vague ones.

Then the key question:

**Would a fresh session make the same mistake without this?** If no, skip it.

For survivors:
- If it overlaps with an existing memory but adds info, mark as **UPDATE**
- If an existing memory is stale or wrong, mark as **DELETE** or **UPDATE**
- Otherwise continue to Step 2.5

## Step 2.5: Absorb Check

Before marking a survivor as NEW, check whether it belongs inside an existing aggregate doc instead of becoming its own memory file. Folding a fact into a doc that already collects that category adds 0 lines to `MEMORY.md`; a standalone memory adds 1. Aggregates also keep related facts in the one place a future session will actually look.

An **aggregate doc** is anything that already accumulates many facts of one kind. Scan this environment for these (and keep your own known list below):

- A curated index or pointer-doc that owns a category (a people directory, a team -> ownership map, a glossary, a decisions/ADR log).
- An internal or team wiki / knowledge base (Confluence, Notion, a `wiki/` tree in the repo).
- A project's markdown doc tree (a README, a per-project notes folder, a domain-notes directory).
- A personal append-style wiki or running knowledge log (Karpathy-style: one growing file, or a folder you append durable notes to).

> Known aggregates in my setup (edit this list for your own):
> - `path/to/people-directory.md` — facts about a person (role, handle, areas, focus)
> - `path/to/teams-doc.md` — team -> repo / service / project ownership
> - `path/to/wiki/` — durable cross-project knowledge

If a candidate fits an aggregate, mark it **ABSORB** with the target path + the section to add it to. The aggregate's own `MEMORY.md` pointer line (if it has one) stays as-is unless its description no longer covers the new scope.

Use **NEW** instead of ABSORB when:
- The fact is instructional/behavioral (a correction, preference, or workflow rule) — those belong as standalone `feedback_*` memories, not buried in a reference doc.
- It fits no aggregate cleanly (don't force it).
- It spans several aggregates (rare; pick the dominant one or split).

## Step 3: Present for Approval

Show a numbered list. Max 5 items. If you found more, rank by impact and cut the rest.

Format each item:

```
N. [NEW|UPDATE|DELETE|ABSORB] category: one-line summary
   Confidence: X% | Type: feedback|project|reference
   File: filename.md (for UPDATE/DELETE: current content summary + proposed change)
                     (for ABSORB: target aggregate path + section to add to)
   Why: what happened in this conversation that surfaced this
   How to apply: when and how future sessions should use this
```

**Confidence rubric:**

| Score | Use when |
|-------|----------|
| 90-100% | User explicitly corrected you, or same mistake happened twice |
| 70-89% | Clear preference demonstrated, non-obvious environment fact |
| 50-69% | Inferred from one instance, might be situational |
| Below 50% | Don't suggest it at all |

After the list, show:

```
MEMORY.md: {current_lines}/~200 lines
```

If over 195 lines, add: "WARNING: MEMORY.md is near capacity. Consider consolidating entries."

Also flag any skills (in `~/.claude/commands/` or wherever yours load from) that reference concepts touched by these learnings.

Then: "Approve by number (e.g. `1, 3`), `all`, or `none`."

**Wait for the user's response. Do not proceed until they approve.**

If user says "looks good" or similar, confirm: "All of them?"

## Step 4: Execute Approved Items

For each approved item:

**NEW:**
1. Create `$MEMORY_DIR/{type}_{snake_case_name}.md` with frontmatter (name, description, type) and body (fact/rule, **Why:**, **How to apply:**)
2. Add one-line entry to `MEMORY.md` under the appropriate `##` section

**UPDATE:**
1. Read the existing memory file
2. Edit the body (preserve frontmatter structure, update description if needed)
3. Update `MEMORY.md` entry text if the summary changed

**DELETE:**
1. Remove the memory file
2. Remove the `MEMORY.md` entry

**ABSORB:**
1. Read the target aggregate doc
2. Edit it to add the new fact in the appropriate section (match existing structure/voice)
3. If the aggregate has a `MEMORY.md` pointer line and its description no longer covers the new scope, lightly update it (don't expand into a multi-line entry)
4. Do NOT create a new memory file. Do NOT add a new `MEMORY.md` line.

**SKILL:** If flagged, edit the skill file in `~/.claude/commands/` (or wherever the skill is loaded from).

Report final `MEMORY.md` line count after changes.

## Rules

- **Never auto-save.** Every write requires explicit user approval.
- **Max 5 items per session.** Rank by impact, cut the rest.
- **Never save**: file paths, code patterns, architecture, git history, task progress, debugging steps.
- **Absorb > Update > Create.** Folding into an aggregate doc costs 0 index lines; updating an existing memory costs 0; creating costs 1.
- **Empty result is fine.** "Nothing from this session rises above the noise threshold." Don't invent learnings.
