---
description: Deep-investigate an issue and produce an implementation plan
argument-hint: "<issue-id | URL | free-form description>"
---

# Deep Plan

Investigate an issue thoroughly, gather all relevant context, then produce an implementation plan.

**Input**: `$ARGUMENTS` — one of:
- A provider URL (`https://github.com/org/repo/issues/123`, `https://company.atlassian.net/browse/PROJ-123`)
- An issue ID (`PROJ-123`, `#123`, `gh:123`)
- A free-form description (`"fix the flaky login test"`)

---

## Phase 1: Parse Input & Fetch Issue

Detect the shape of `$ARGUMENTS`:

| Pattern | Provider |
|---------|----------|
| `atlassian.net/browse/` in URL | Jira |
| `github.com/.../issues/` in URL | GitHub |
| `PROJ-123` pattern (project-number) | Jira (requires `cloudId` — ask user if unknown) |
| `#123` or `gh:123` | GitHub (use `gh` CLI against current repo) |
| Free-form text | No fetch; treat as the description directly |

**If Jira**: launch the `issue-lookup` agent (or call the Atlassian MCP if available). Fetch fields `summary,description,status,priority,issuetype,parent,issuelinks,labels`.

**If GitHub**: `gh issue view <number> --json title,body,state,labels,assignees,milestone,comments`

**If free-form**: skip Phase 1, use the text as the scope.

Extract and note:
- Summary, description, type (Bug / Feature / Task), status
- Acceptance criteria (if present)
- Parent / epic (fetch it if present — gives broader context)
- Linked issues

**Detect signals** in the text. Check each:

| Signal | How to detect | Flag |
|--------|--------------|------|
| Design link | URL containing `figma.com`, `excalidraw.com`, `whimsical.com` | → DESIGN |
| UI component | Keywords: "modal", "page", "tab", "drawer", "form", "table", "toast" | → UI_COMPONENT |
| Translation / i18n | Keywords: "text", "label", "message", "translation", "copy" | → I18N |
| Bug | Issue type = Bug, or keywords "broken", "error", "doesn't work" | → BUG |
| API / endpoint | Keywords: "endpoint", "API", "controller", "DTO", "request", "response" | → API |

---

## Phase 2: Gather Context

Based on signals from Phase 1, run the applicable steps **in parallel** (launch multiple tool calls in a single message).

### If DESIGN flagged
- Extract the URL
- If Figma MCP is available, call `get_design_context` with the fileKey and nodeId
- Otherwise, note the URL in the plan and ask the user for key specs

### If UI_COMPONENT flagged
- Grep for existing implementations of the component type in the repo
- Find the closest existing pattern — this is the **reference implementation** the plan should follow
- Note any design-system imports in use

### If I18N flagged
- Find the translations directory (grep for `i18n`, `translations`, `locales`, common filenames)
- Note the naming convention and relevant namespace

### If BUG flagged
- Grep for keywords from the bug description
- `git log --oneline -10 --grep="<relevant keyword>"` for recent related changes
- Trace the code path: component → hook → API call → error handling

### If API flagged
- Grep for the endpoint path or handler name
- Read the service/controller that owns it
- Check any OpenAPI/GraphQL schema files in the repo

### Always
- Search the repo for the issue ID (e.g. `PROJ-123`) — TODOs or references may exist
- `git log --all --oneline --grep="<issue-id>"` for prior branches
- Check `.claude/` for project-specific notes (todos, plans, ADRs)

---

## Phase 3: Identify Patterns & Gaps

From the context gathered:

1. **Reference pattern**: Identify the single best existing implementation to follow. Include the file path.

2. **Field coverage audit** (for UI display issues): If the issue describes rendering a data type, list every field the type exposes. For each field, mark whether it is: already displayed, being added by this issue, or missing. Flag missing fields — the issue description may be incomplete.

3. **Unknowns**: List anything the issue doesn't specify that you need to know to implement:
   - Missing acceptance criteria
   - Ambiguous behavior (e.g., "what happens when X fails?")
   - Design decisions not covered
   - API contracts not yet defined

4. **Dependencies**: Note if this task depends on other issues, PRs, or unreleased code.

---

## Phase 4: Ask Clarifying Questions

If Phase 3 found unknowns or ambiguities, ask the user **before** producing the plan. Frame questions as concrete choices, not open-ended.

Skip this phase if everything is clear.

---

## Phase 5: Produce Plan

Write the plan to `~/.claude/plans/<slug>.md` AND output a summary in chat:

```
## [ISSUE-ID or slug]: [Summary]

### Context
[1-2 sentences: what the change does and why it's needed]

### Design Reference
[Link + key specs, or "No design — logic-only change"]

### Approach
1. **[Step name]**
   - What: [description]
   - File(s): `path/to/file.ts`
   - Pattern: follows `path/to/reference.ts` [brief note on what to copy]

2. ...

### Files to Modify
| File | Change |
|------|--------|
| `path/to/file.ts` | [brief description] |

### Testing
- **Manual**: [how to verify in the browser/API/CLI]
- **Automated**: [what tests to write, which test file]

### Confidence: [High/Medium/Low] ([X%])
[1 sentence: what makes this confident or uncertain]
```

### Plan quality checklist (internal, don't output):
- [ ] Every step has a file path
- [ ] Reference pattern identified and cited
- [ ] No step is vague
- [ ] Testing section is actionable
- [ ] Confidence accounts for unknowns found in Phase 3

## Dashboard status (best-effort)

If the agent dashboard is installed, emit run status so it shows live. Resolve the emitter once; if absent, skip silently — an emit must never block or fail planning. Never hand-write JSON into the state dir; only the emitter writes snapshots.

```bash
EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "$EMIT" ] || EMIT="$(command -v emit-status.sh 2>/dev/null || true)"
```

With `TICKET` = the issue id/slug and `SESSION="$TICKET-planner"`, emit (skip all if `$EMIT` is empty):

- At Phase 1 (start): `"$EMIT" --session "$SESSION" --role planner --state started --ticket "$TICKET" --note "planning"`

## Gotchas

- **Scope Jira MCP fields**: Unscoped `getJiraIssue` returns 60-80KB. Pass a fields list.
- **Suggest /plan-review after outputting the plan**: Staff engineer review catches issues before implementation.
