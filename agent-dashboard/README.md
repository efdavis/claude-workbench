# Agent orchestra dashboard

A live terminal view of, and index into, your agent runs. Leave it open in one pane and watch `implement` / `code-review` / `pr-prep` / `babysit-pr` (and dispatch lanes) work in others: which issue, which role, what state, how long, and which ones need you. Then drive a row cursor and hit one key to jump to any of them.

Music-themed: the dashboard is the **orchestra**, each run is a **soloist**, and the `dispatch` lane spawner is the **cue**. Naming only; the mechanics are the same filesystem-snapshot convention.

It **observes and navigates**, it never mutates a live run. Every external side effect (opening a cmux tab, attaching a tmux lane) is shelled out to `handler.sh`; the renderer's only local write is the `r` key removing a finished/dead row's card. No daemon, no cron, no persistent agents.

## Keys

| Key | Action |
|---|---|
| `j` / `k` or `↓` / `↑` | move the row cursor (tracks a run by session id, so it stays put across refresh/re-sort) |
| `Enter` | open the focused run, three outcomes by liveness: a **live/ghost** row backed by a real dispatch lane opens a cmux tab that attaches it (close the tab to detach, the lane lives on); a row live only in its own cmux surface (a hands-on run, no lane) jumps straight to that cmux tab (focuses the surface, follows its workspace so the left-column selection tracks, then flashes it); anything else (finished, stale, or not yet matched) opens its newest transcript, **rendered as readable turns** (user/assistant text, tool calls, results, not the raw JSON), in a pager tab, if one exists |
| `p` | open the run's PR in a cmux browser tab (set `AGENT_DASHBOARD_PR_URL_BASE`) |
| `t` | open the run's issue in a cmux browser tab (set `AGENT_DASHBOARD_ISSUE_URL_BASE`) |
| `r` | reap the focused row - remove a **merged/done or stale (💀)** card from the board. Refuses on a live/ghost row (won't yank a card from under a running agent). Deletes only the on-disk snapshot, never the run. |
| `q` / `Ctrl-C` | quit (restores the terminal, even on SIGTERM/SIGHUP) |

Actions require **cmux** (`brew install cmux`); without it the action prints an install hint and no-ops. An unhandled key shows a transient status line, never a silent no-op.

## Quickstart

No install, no setup beyond `python3`. From this directory:

1. **Watch it:** `./run.sh` (Ctrl-C to quit). It renders an empty board until runs appear.
2. **Try it with demo data:** in another pane, `./seed-demo.sh` drops six fake runs (3 active, 1 waiting at a gate in yellow, 1 escalated in red, 1 merged). Remove them with `./seed-demo.sh --clear`.
3. **See real runs:** wire the emitter into your commands (see Wiring below), then open a fresh session with the wired commands loaded, run `./run.sh` in a pane, and drive a command in another. Its row advances live (started -> implementing -> pr-open, etc.). Or drive a whole issue with `/ship <issue>`: one `<issue>-pipeline` journey row plus each sub-command's detail rows.

## How it works

Four moving parts, joined by a filesystem convention:

- **`emit-status.sh`** - a tiny bash helper the commands call at each phase boundary. It writes one atomic JSON snapshot per run (built with python3 stdlib, no `jq` or other dependency) to `${AGENT_DASHBOARD_STATE_DIR:-~/Projects/claude-workbench/agent-dashboard/state}/<session>.json` (schema: [`status.schema.json`](./status.schema.json)). **Best-effort by contract:** if it's absent or the state dir is unwritable it silently no-ops, so it can never break the run it observes.
- **`dashboard.py`** - polls that state dir and renders a live cross-section, plus the row cursor + action keys. Pane liveness comes from two anchored, exact-match sources per refresh: cmux rows match the emitter's captured `$CMUX_SURFACE_ID` against `cmux tree` (for hands-on runs), and dispatch lanes match against one `tmux -L <socket> list-sessions` (a row's `<issue>` / `<issue>-worker` matched to a lane named `<issue>` **exactly**, never a prefix rule, which would cross-match sibling issue numbers like `PROJ-7` vs `PROJ-76`). A live match past the stale threshold shows as `ghost`; a terminal (merged/done) row is never live. Pure stdlib Python 3; ANSI rendering; honors `NO_COLOR`.
- **`statusline.sh`** - a Claude Code [statusline](https://docs.claude.com/en/docs/claude-code/statusline) script, and the source of the `cost` / `ctx` columns and the usage-limit readout. Claude Code pipes a JSON blob into it on every render — a **shell hook, not a model call, so it costs zero tokens** — and it mirrors context % and spend to `/tmp` (keyed by both session id and `$CMUX_SURFACE_ID`), and the account-wide 5-hour + 7-day usage limits to **both** the durable `quota/` dir under this harness **and** `/tmp` (dual-write). Copy it to `~/.claude/statusline.sh` and point `settings.json` at it. Skip it and the dashboard still works — those cells just render `-`.
- **`handler.sh`** - the one external action handler `dashboard.py` shells out to on an action key (`handler.sh <coord> <key> <issue> <state> <live> <pr> <worktree_path> <cmux_surface>`). It owns every side effect (cmux tabs, `tmux attach`, the transcript pager), keeping the renderer a pure viewer. **cmux-required, fire-and-forget:** always exits 0 and prints one status line the dashboard surfaces.
- **`transcript.py`** - renders a finished run's Claude Code session `.jsonl` into readable turns (user/assistant text, tool calls with a one-line arg hint, truncated tool results, a compact thinking marker) and drops the hook/mode/metadata noise the raw file is full of. `handler.sh`'s replay action pipes it into a pager (`transcript.py <file> | less -R`). Pure stdlib.

**Shared harness home (all projects, including Emberfall):**

```text
~/Projects/claude-workbench/agent-dashboard/
  state/     # live seat rows (gitignored)
  quota/     # durable claude/codex/grok % (gitignored) — survives reboot
  coord/     # coordinator cadence files (gitignored)
  *.py *.sh  # versioned code
```

Optional: `ln -sfn ~/Projects/claude-workbench/agent-dashboard ~/.claude/agent-dashboard` so anything still hard-coded to `~/.claude/agent-dashboard` follows along. Runtime dirs are gitignored, **never in a repo**: the code is versioned, live snapshots and quota are ephemeral and machine-local under Projects.

One optional part:

- **`overseer.py`** - reconciles rows against GitHub reality. A row with a `pr_number` can outlive its emitting session (the run ends, the PR lives on, the row shows `pr-open` forever); the overseer polls GitHub and flips rows whose PR **merged** (-> `merged`) or **closed unmerged** (-> `done`), writing through `emit-status.sh` so there is still a single state writer. Open PRs are left alone, babysit owns their live status. **No LLM, zero tokens:** one `gh pr view` call per reconcilable row per tick (default 60s; `--once` for a single pass). It only UPDATES status; it never deletes. `dash` runs it alongside the board for you (see Run), so you never invoke it directly and it dies with the board; run it standalone only if you want the reconciler without the board. To clear a dead/merged card off the board, hit `r` (removes the snapshot, not the run).

## Run

```bash
./dash.sh                # the one you want: board + live PR status (board + overseer in one pane)
./run.sh                 # board only, no PR-status updates (or: python3 dashboard.py)
python3 overseer.py      # the reconciler on its own — you normally never run this; dash bundles it
```

`q` or Ctrl-C to quit (it restores your terminal on exit). The dashboard shows:

- **title** - counts, total spend, and Claude's **5-hour usage limit** with its reset countdown (`5h ████████░░ 81% used · resets 25m`) — the limit that actually stops work, so it reddens at 90%. Needs `statusline.sh` installed; absent, the title reads as it always did.
- **weekly line** (directly under the title) - **7-day** plan-usage bars, one slot per vendor whose data is present: **claude · codex · grok** (`claude 7d ██░░░░░░░░ 18% · 4d12h   codex 7d █░░░░░░░░░ 10% · 6d22h`). Claude comes from `statusline.sh` (`rate_limits.seven_day` → dual-write `quota/claude-7d.txt` + `/tmp/claude-rate-limit-7d.txt`). Codex is refreshed by the dashboard from ChatGPT `wham/usage` (primary window ≈ weekly; falls back to the newest Codex rollout's `rate_limits.primary`) and dual-writes `quota/codex-7d.txt` — but only if `~/.codex/auth.json` exists, so an enterprise-Claude-only machine never fires that request and never shows a codex slot. Grok has no stable public weekly % API yet — leave a mirror at `quota/grok-7d.txt` or `/tmp/grok-rate-limit-7d.txt` (`pct reset_epoch`) if you have one; absent, the slot simply doesn't render. After reboot, durable `quota/*` still paints until a live session re-mirrors.
- **soloists** - every run (role · issue · state · model · ctx · cost · age · pane · note): the `state` cell is glyph-prefixed (🎬 started · 🎻 implementing · 👀 reviewing · 🙋 waiting · 📬 pr-open · 🚨 escalated · 👏 merged · ✅ done · 💀 stale); the `model` column shows which model drives the run (opus/sonnet/haiku color-coded, plus grok/codex peer seats); `ctx` is its context-window fill as a mini bar (red near auto-compact); `cost` is its spend — priced by walking the run's transcript **and every nested subagent/Workflow transcript** via `cost.py` (throttled ~30s), so a fan-out-heavy lane shows its true cost, not the statusline's subagent-blind `total_cost_usd`. Rows it can't bridge to a Claude session (grok/codex seats, panes with no statusline yet) fall back to the mirror value; the `pane` column is `live`/`ghost`/`stale`/`-`. Escalated first, then `waiting` (paused at a human gate, bold yellow), active, and terminal (merged/done) dimmed and kept until you clear them with `r`. Stale runs flagged (no update in 15m); rows capped at `AGENT_DASHBOARD_MAX_ROWS` with an overflow count. The `▸` cursor marks the focused row (reverse-video bar when color is on); `j`/`k`/arrows move it, and it tracks a run by session id so it holds position across a refresh.
- **pilot** (optional panel) - present only when `PILOT_LIGHT_DIR` points at a configured [pilot-light](../../emberfall/pilot-light) sidecar. Shows its armed/gated/running state, launch count, next fire, and last verdict. Unset (the default, and every enterprise machine) → no panel.
- **escalations** (red panel) - any run in `escalated` state (a hard stop, a failure). Routine gate-waits show as `waiting`, not here, red is earned.
- footer - load average, the key legend, the state dir, the quota dir, refresh interval.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `AGENT_DASHBOARD_HOME` | `~/Projects/claude-workbench/agent-dashboard` | shared harness root (state + quota + code) |
| `AGENT_DASHBOARD_STATE_DIR` | `$AGENT_DASHBOARD_HOME/state` | where snapshots are written/read (emitter + dashboard must agree) |
| `AGENT_DASHBOARD_QUOTA_DIR` | `$AGENT_DASHBOARD_HOME/quota` | durable claude/codex/grok % mirrors (+ `snapshot.json`) |
| `AGENT_DASHBOARD_MODEL` | unset | overrides the model shown for a run when `--model` isn't passed (else auto-detected from cmux's launch argv) |
| `AGENT_DASHBOARD_REFRESH` | `2` | dashboard refresh seconds |
| `AGENT_DASHBOARD_STALE_SECS` | `900` | a non-terminal run with no update past this is flagged stale |
| `AGENT_DASHBOARD_MAX_ROWS` | `30` | max table rows rendered; overflow shown as a "+N more" line |
| `AGENT_DASHBOARD_TMUX_SOCKET` | `agent-lanes` | the private `tmux -L <socket>` lane liveness reads **and the socket `handler.sh` attaches on** (the socket `dispatch` spawns on) |
| `AGENT_DASHBOARD_PR_URL_BASE` | unset | `p` opens `<base>/<pr>` (e.g. `https://github.com/OWNER/REPO/pull`); unset -> the key prints a hint |
| `AGENT_DASHBOARD_ISSUE_URL_BASE` | unset | `t` opens `<base>/<issue>` (e.g. `https://you.atlassian.net/browse`); unset -> the key prints a hint |
| `AGENT_DASHBOARD_DEBUG` | unset | when set, `emit-status.sh` prints why it no-op'd (else silent) |
| `NO_COLOR` | unset | standard, disables ANSI color |

## Wiring

The commands emit best-effort at their phase transitions (each has a "Dashboard status" section). For them to find the emitter from inside *your* project (a different repo than this one), either symlink `emit-status.sh` onto your `PATH` or export `AGENT_DASHBOARD_HOME=/path/to/agent-dashboard`.

| Command | Role | States it emits |
|---|---|---|
| `implement` | worker | started → implementing → pr-open (· escalated on a hard stop) |
| `babysit-pr` | finisher | pr-open → merged (· escalated on a required-gate stop) |
| `code-review` | reviewer | reviewing → done |
| `/ship` | planner → worker → reviewer → finisher (one `<issue>-pipeline` journey row) | started → waiting (each gate) → implementing → reviewing → pr-open → done (· escalated on a failure / moved HEAD) |

A teammate who hasn't set up the dashboard sees zero change, the emit calls no-op.

## Dispatch: the spawn half

`./dispatch.sh ISSUE PIPELINE_CMD` claims the issue in Jira, cuts an isolated `git worktree` of the repo you run it from, and spawns a detached `tmux -L <socket>` lane running the pipeline, emitting a `started` row here. Run it from inside the repo you want to work in.

```bash
cd ~/code/your-repo
/path/to/agent-dashboard/dispatch.sh PROJ-75 'claude "/implement --auto PROJ-75"'
```

Multiple issues can run in parallel this way without sharing a checkout HEAD or double-claiming. Every stage is fail-closed with a rollback that *restores* prior state (it never blind-unassigns): a lost claim race leaves the winner untouched, and a post-claim failure rolls the assignee + status back to what they were.

Unlike the viewer (which needs only `python3` + cmux for the action keys), dispatch additionally needs `tmux` and Jira token auth for the claim step:

| Var | Meaning |
|---|---|
| `JIRA_BASE_URL` | your Jira site, e.g. `https://you.atlassian.net` |
| `JIRA_EMAIL` | the Atlassian account the API token belongs to |
| `JIRA_API_TOKEN` | a personal token, mint at `https://id.atlassian.com/manage-profile/security/api-tokens` |
| `AGENT_DASHBOARD_TARGET_BRANCH` | branch the worktree is cut from (default `origin/main`) |

The claim is delegated to `jira_claim.py` (swap in another tracker with `AGENT_DASHBOARD_CLAIM_CMD`). For an `--auto` lane, dispatch seeds any `.plans/<issue>*` artifacts into the worktree and refuses if a required `<issue>.review.json` is missing, so the headless lane never runs without its reviewed plan.

## Tests

Pre-commit contract tests, pure stdlib (plus `jq` for the emit assertions only):

```bash
./emit-status.test.sh    # the emitter's atomic-write + best-effort contract
./handler.test.sh        # each action key -> recipe, and every fail-visible path (PATH-shims cmux/tmux)
./dashboard.test.sh      # emoji alignment, the row cursor, exact-match lane liveness, r-reap guard
./transcript.test.sh     # replay renderer keeps user/assistant turns, drops metadata noise
./dispatch.test.sh       # claim -> worktree -> lane, with fail-closed rollback (shims git/tmux/claim)
./check-generic.sh       # asserts no project-specific branding leaked in
```

## Ad-hoc lanes (sessions no skill launched)

A row is just a snapshot file, so a plain chat session can claim one too — useful when you're driving
a project by hand in a cmux tab and want it on the board next to the skill-driven runs.
`emberfall-lane.sh` does this as a Claude Code hook (zero tokens — a shell hook, not a model call).
Symlink it into `~/.claude/hooks/` and wire it on three events in `settings.json`:

| Event | Effect |
|---|---|
| `UserPromptSubmit` | claims the lane (cwd in the project tree, or the prompt names it — then sticky for the session), state `implementing`; refreshes the timestamp each turn so it never goes stale |
| `Stop` | state `started` — idle, your turn |
| `SessionEnd` | removes the row |

Rows land as `ember-chat-<sid>`, role `other`, ticket `adhoc`. The note comes from the session's
statusline topic file, so it says what the session is actually about. `worktree_path` is set **only**
when the session is cwd'd inside the project tree — that's the signal a coordinator uses to tell a
lane that might be editing files apart from a chat that merely talks about the project.

It will not double-claim a session a wired skill already owns (detected two ways: the prompt opens
with an emitting slash-command, or another non-terminal row shares its `$CMUX_SURFACE_ID`) — that
would double-count the run and plant a phantom "live editor" at a coordinator's liveness gate.

Cleanup: `emberfall-lane.sh --gc` drops lanes whose cmux tab is gone (a hard-killed tab never fires
`SessionEnd`); it no-ops entirely when cmux can't be queried, so it can never mass-delete live rows.
`--prune` drops every ad-hoc lane — the deliberate-reset button. Neither ever touches a skill row.

## Scope + what's next

v1 observes manual runs and navigates them (cursor + open-in-a-tab); it still drives nothing on its own. Deliberately deferred:

- a **cron + daemon** for autonomous overnight dispatch, only if a groomed backlog justifies the cost.
- a **`textual`** upgrade if you later want richer interactivity (scroll, mouse) instead of the refresh + raw-mode input loop.
