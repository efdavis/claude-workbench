---
description: Drive one issue through the full command chain (plan → plan-review → implement → code-review → pr-prep → draft PR → babysit handoff) with a stop-and-ask gate at every human decision. Hands-on, single issue, never merges. Emits live status to the agent dashboard.
argument-hint: "<issue-id | slug> [--investigate]"
disable-model-invocation: true
---

# Ship

Per-issue, human-gated pipeline: run the sibling commands in sequence for one issue, stop and ask at
every human-decision gate, and end at an open **draft** PR with babysit handed off. This command is
**thin glue** — every phase is a Skill-tool invocation of a sibling command; never inline, paraphrase,
or re-implement their steps (they are independently maintained). The agent dashboard
(`agent-dashboard/`, if installed) shows the run live: a routine gate-wait shows as yellow `waiting`,
and red `escalated` is reserved for off-happy-path stops (failure, hard stop) so red stays meaningful.

**Input**: `$ARGUMENTS` = an issue id or slug (e.g. `PROJ-12`, `#42`, `fix-login-redirect`) and an
optional `--investigate` flag.

`--auto` is NOT accepted — an unattended mode (soft gates flip to proceed-and-log, hard-stops survive)
is not built. If passed, refuse and say so.

## The chain

| # | Phase | Invokes | Gate |
|---|---|---|---|
| 0 | Preflight | — | refuse-to-run checks |
| 1 | Investigate | `investigate` (if present) | only with `--investigate` |
| 2 | Plan | `plan` | — |
| 3 | Plan review | `plan-review` | **always stops** — even on APPROVE |
| 4 | Implement | `implement` | its own checkpoints. Ends at green checks, **uncommitted** |
| 5 | Code review | `code-review` (no arg = working tree) | LOW-only proceeds; any HIGH / MED stops |
| 6 | Prep + open PR | `pr-prep draft` | its own checkpoints (description, file-stage, PR review) |
| 7 | Handoff | — | print `/babysit-pr <PR>`; session ends. Merge is the human's, always |

**Gate discipline:** a gate is an `AskUserQuestion` in this session. Before asking, emit `waiting`
with the pending question in `--note` (a routine gate is expected flow, not an alarm; `escalated` is
reserved for failures, hard stops, and a third consecutive REVISE). A gate NEVER defaults to
proceed — if the ask times out, re-ask; the stops are the whole point of hands-on mode. Log every
gate decision verbatim for the Step 7 report.

## Dashboard emits (best-effort)

Resolve the emitter once; if absent, skip every emit silently — an emit must never block or fail the
run. **Skip means skip:** never hand-write JSON into the state dir; an improvised write bypasses the
emitter's schema/sanitization/atomic-write contract and fakes a green wiring signal.

```bash
EMIT="${AGENT_DASHBOARD_HOME:+$AGENT_DASHBOARD_HOME/emit-status.sh}"
[ -x "$EMIT" ] || EMIT="$(command -v emit-status.sh 2>/dev/null || true)"
```

**Every `Emit` below is shorthand** for `"$EMIT" --session <ISSUE>-pipeline --ticket <ISSUE> <shown args>`
— the emitter silently no-ops if `--session`, `--role`, or `--state` is missing, so never drop them.
Where the shorthand omits `--role`, use the current phase's role. One journey row for the whole run;
`role` tracks the phase (`planner` → `worker` → `reviewer` → `finisher`), `--note` carries the fine
phase. Sub-commands emit their own detail rows (`<ISSUE>-worker`, `<ISSUE>-reviewer`, …) once wired —
that is by design: journey row + detail rows. Don't suppress them.

## Step 0: Preflight (refuse-to-run)

All must pass, else report which failed and stop:

1. **Issue arg.** `$ARGUMENTS` contains exactly one issue id or slug. `--auto` present → refuse (not built).
2. **Repo.** `git rev-parse --show-toplevel` succeeds. Run from the repo root or a worktree of it.
3. **Siblings present.** `plan`, `plan-review`, `implement`, `code-review`, `pr-prep` are invocable in
   this session (commands load at session start — if one exists on disk but is not invocable, stop and
   tell the user to open a fresh tab in the repo). `investigate` is optional (Step 1 skips if absent).
4. **Issue exists (best-effort).** If the arg is an issue id, try to confirm it (`gh issue view` for
   `#N`, the `issue-lookup` agent / Atlassian MCP for a `PROJ-N` id). Unverifiable (offline, no
   integration, free-form slug) → ask the user whether to proceed unverified (their call, logged);
   never hard-refuse on this one.
5. **Checkout not shared with another active run.** A checkout has ONE HEAD; a parallel run switching
   branches mid-ship is a wrong-branch-commit hazard. Best-effort: if the dashboard state dir
   (`${AGENT_DASHBOARD_STATE_DIR:-$HOME/.claude/agent-dashboard/state}`) holds a non-terminal snapshot
   whose `worktree_path` equals this checkout and whose session is not this run's, stop and recommend a
   dedicated worktree (`git worktree add ../<ISSUE>-wt <base>`). No state dir / no snapshots → passes silently.

Then Emit `--role planner --state started --note "preflight ok"`.

## Step 1: Investigate (only with `--investigate`)

If no `investigate` command is present, note it and skip. Otherwise Emit
`--role planner --state started --note "investigating"` and invoke `investigate` with the issue.

## Step 2: Plan

Emit `--role planner --state started --note "planning"`. Invoke `plan` with the issue. **Capture the
exact plan path it reports** (`~/.claude/plans/<slug>.md`) — that path is this step's completion
evidence and the argument for Step 3. "Some plan file exists" is NOT sufficient (a parallel pane's
plan or a stale one satisfies it).

## Step 3: Plan review — GATE (always)

Emit `--role planner --state reviewing --note "plan review running"`. Invoke `plan-review` **with the
plan path captured in Step 2** — not the bare issue; its no-arg fallback picks the most-recently-modified
plan, which in parallel-pane use can be a different issue's.

**Pre-gate digest (render, don't re-analyze).** Before the gate ask, print a short digest so the
plan's substance is in front of the human at decision time — both verdicts, APPROVE included. This is
gate presentation, not fresh analysis: render what the plan file + review already state, with a fixed,
small set of licensed judgements — an `Impact` high/med/low call per file, made from the plan's
blast-radius / risk notes (plans rarely grade files, so this judgement is expected; don't hunt for
grading text that isn't there).

```
Plan digest: <ISSUE> (<plan path>)
Verdict: <APPROVE|REVISE> · Confidence: <NN%>
Approach: <first line of the plan's Approach>

| File | Change | Impact |    ← one row per Files-to-Modify entry; Impact = the licensed high/med/low judgement
```

Confidence always renders as a percentage: use the plan's number where it gives one; where it grades
in letters, map coarsely — H → 85%, M → 60%, L → 35% — keeping the letter visible (`85% (H)`) so the
mapping doesn't fake precision the plan lacks. On REVISE, the reviewer's findings follow the digest
as-is, with their own confidence scores.

Then stop — this gate fires **even on APPROVE** (hands-on mode is where trust gets built):

* **APPROVE** → Emit `waiting --note "plan-review APPROVE — awaiting go"`; ask: **Proceed** / **Revise anyway** (treat as REVISE) / **Abort**.
* **REVISE** → Emit `waiting --note "plan-review REVISE — awaiting call"`; present the findings as-is; ask: **Update plan + re-review** (apply accepted suggestions via the review command's own apply step, then re-invoke `plan-review` with the same plan path) / **Proceed anyway** (logged) / **Abort**.
* Loop bound: a third consecutive REVISE → Emit `escalated --note "plan-review REVISE x3"`; recommend abort.

On proceed, Emit `--role worker --state implementing --note "implementing"`.

## Step 4: Implement

Invoke `implement` with the issue. Its own checkpoints (existing-branch choice, plan-assumption drift)
surface to the user as designed; relay them, don't answer them yourself and don't suppress them.

* **Repo hard-stop guard.** If the repo defines a PreToolUse guard (e.g. an auth/secret-path guard in
  its `.claude/settings.json`) and it trips, that stop is mandatory and never skippable: Emit
  `escalated --note "repo guard tripped — hands-on only"`, hand it to the user, resume or abort on
  their call. Never work around a repo guard. (No guard configured → nothing special here.)
* **Unfixable pre-commit failure**: Emit `escalated`, ask: user guidance + retry / abort.

`implement` ends at green pre-commit checks and **stops before any commit** ("Ready for `/pr-prep`").
So at this step's end the work sits **uncommitted in the working tree**; Step 6 is the gated glue that
commits and ships it.

## Step 5: Code review — GATE

Emit `--role reviewer --state reviewing --note "code review"`. Invoke `code-review` **with no argument**
— its no-arg form reviews the uncommitted working tree, which is exactly where the implementation sits.

Read the synthesized findings:

* **LOW only (or none)** → log them in the report and proceed. No ask.
* **Any HIGH or MED** → Emit `waiting --note "code review: <N> HIGH / <M> MED"`; ask: **Fix now**
  (apply the fixes, re-run the pre-commit checks, re-invoke `code-review`; max two fix cycles, then
  re-ask with abort recommended) / **Defer** (logged with the finding list — they ride to the PR body
  in Step 6 for the reviewer to see) / **Abort**.

## Step 6: Prep + open the draft PR

Emit `--role finisher --state reviewing --note "pr-prep"`. Invoke `pr-prep` with the `draft` flag.
`pr-prep` owns the finish and its own gates — do not re-implement them here:

* it runs the pre-commit checks (session-aware: skips if Step 4 already ran them clean),
* generates the description from the working-tree diff and **stops for the user to review it**,
* shows the changed files and **waits for the user to confirm which to stage**,
* commits (using the description Title), pushes, and creates the PR **as a draft**,
* then stops for the user to review the created PR.

**Deferred findings ride in the PR body.** If the Step 5 gate deferred any findings, ensure the
description `pr-prep` presents for review includes a `## Deferred review findings` section — one line
per finding (`severity · finding · one-line disposition`), closing with: *"Each of these that survives
review should get a follow-up issue before merge."* None deferred → no section.

Capture the PR number + URL from `pr-prep`'s output. Emit `--role finisher --state pr-open --note "draft PR #<N> open"`.

## Step 7: Handoff + report — session ends

Emit `--role finisher --state done --note "PR #<N> open; babysit handed off — merge is the human's"`.
Then sweep this run's detail rows to terminal so they don't zombie once their sessions end:
`"$EMIT" --session <ISSUE>-worker --role worker --state done --ticket <ISSUE> --pr <N> --note "handed off; PR #<N>"`,
and the same for `<ISSUE>-reviewer` — but never `<ISSUE>-finisher`, which is the spawned babysit tab's live row.

**Auto-spawn babysit (best-effort).** If `cmux` is on PATH, launch the babysit tab yourself:

```bash
if command -v cmux >/dev/null 2>&1 \
   && SURF=$(cmux new-surface --type terminal --focus false 2>/dev/null | awk '{print $2}') \
   && [ -n "$SURF" ]; then   # new-surface output: "OK surface:<n> pane:<m> workspace:<k>"
  cmux rename-tab --surface "$SURF" "<ISSUE> babysit #<N>"
  cmux send --surface "$SURF" -- 'cd <this-checkout-toplevel> && claude --model sonnet "/babysit-pr <N>"\n'
else
  echo "cmux spawn unavailable — hand off manually: fresh tab in this repo, then /babysit-pr <N>"
fi
```

The guard IS the fallback — never run `rename-tab`/`send` against an empty `--surface` (a mis-routed
`send` types into someone else's pane). A failed spawn is not an error: the else-branch reports it, and
the Next list carries the manual line. Babysit never runs inline — the CI-polling tail does not belong
in this session's context.

Print the final report and stop:

```markdown
## /ship <ISSUE> — ready

**PR**: #<N> <url> (Draft)
**Phases**: investigate? · plan · plan-review (<verdict> → <decision>) · implement · code-review (<counts> → <decision>) · pr-prep · draft PR
**Gate decisions**: <each gate + the user's verbatim answer>
**Deferred findings**: <list, or none>

**Next**:
1. <"babysit auto-spawned: cmux tab '<ISSUE> babysit #<N>'" | or: "in a separate tab (fresh session in the repo): `/babysit-pr <N>`">
2. When green + triaged: review and merge on GitHub — the merge is yours; /ship never merges.
3. Deferred findings (if any): file a follow-up issue for each one that survives review.
```

## Abort (available at every gate)

Emit `--role <current phase's role> --state done --note "aborted at <phase> by user"`. Leave every
artifact in place (plan, working tree or branch, draft PR if it exists) and report what exists + which
sibling command resumes the work manually.

## Gotchas

* **Cache**: this command + its siblings load at session start with cwd in the repo. A freshly edited
  `/ship` needs a new tab.
* **Never merge. Never touch a repo's PreToolUse guard.**
* **Multiple dashboard rows per issue is correct** — the journey row plus each sub-command's detail row.
* **`waiting` vs `escalated`: red is earned.** A routine gate emits `waiting` (yellow); `escalated`
  (red) is only for failures, a tripped repo guard, or a third REVISE. Diluting red trains the operator to ignore it.
* A gate ask that times out gets re-asked. A gate never falls through to proceed.
