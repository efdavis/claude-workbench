---
description: Staff engineer review of the current or most recent implementation plan
argument-hint: "[optional: path to plan file]"
---

# Plan Review

Launch a staff engineer agent to review an implementation plan before you start building.

## Step 1: Find the plan

If `$ARGUMENTS` is provided and is a file path, use that as the plan file.

Otherwise, find the most recently modified `.md` file in `~/.claude/plans/`:

```bash
ls -t ~/.claude/plans/*.md | head -1
```

If no plan file is found, tell the user: "No plan file found. Enter plan mode first, or pass a path: `/plan-review /path/to/plan.md`"

## Step 2: Read the plan

Read the plan file contents.

## Step 3: Launch staff engineer review

Launch the `staff-engineer` agent with this prompt:

> Review the following implementation plan. Explore the codebase to verify its claims (file paths, patterns, existing code). Return a structured review with BLOCKER / SUGGESTION / NITPICK severity levels, each with a confidence score (0-100%), and a verdict (APPROVE or REVISE).
>
> **IMPORTANT: If the plan specifies a feature branch (e.g. "Branch: feature/xxx"), verify claims against that branch using `git show branch:path/to/file`, NOT against master/main.** Plans often describe work-in-progress on feature branches -- checking main will produce false negatives.
>
> Plan file: [path]
>
> ---
>
> [full plan contents]

## Step 4: Present the review

Show the agent's review to the user. Do not editorialize -- present the review as-is.

If the verdict is **REVISE**, ask the user if they want to update the plan now or proceed anyway.

If the verdict is **APPROVE**, let the user know they can proceed to implementation.

## Step 5: Offer to deepen research

After presenting the review, ask:

> Any suggestions you'd like me to research further to improve confidence?

If the user selects suggestions to research, launch targeted exploration agents (Explore or general-purpose, read-only) to gather evidence for those specific suggestions. Present the findings and updated confidence scores.

## Step 6: Apply suggestions

After research is complete (or if the user skips Step 5), ask:

> Want me to apply the accepted suggestions to the plan?

If yes, update the plan in the conversation (or plan file if one exists) with the accepted suggestions incorporated.

## Gotchas

- **Add confidence scores to suggestions**: After presenting the review, show High/Medium/Low (%) next to each SUGGESTION.
- **Use Sonnet agents for targeted verification, not staff-engineer**: staff-engineer defaults to exhaustive exploration. For checking specific claims, use a Sonnet agent with the diff in the prompt.
