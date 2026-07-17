# Agent orchestra dashboard

A live terminal view of, and index into, your agent runs. Leave it open in one pane and watch `implement` / `code-review` / `pr-prep` / `babysit-pr` (and dispatch lanes) work in others: which issue, which role, what state, how long, and which ones need you. Then drive a row cursor and hit one key to jump to any of them.

Music-themed: the dashboard is the **orchestra**, each run is a **soloist**, and the `dispatch` lane spawner is the **cue**. Naming only; the mechanics are the same filesystem-snapshot convention.

It **observes and navigates**, it never mutates a run or holds a key. Every side effect (opening a cmux tab, attaching a tmux lane) is shelled out to `handler.sh`; the renderer itself stays dumb. No daemon, no cron, no persistent agents.

## Keys

| Key | Action |
|---|---|
| `j` / `k` or `↓` / `↑` | move the row cursor (tracks a run by session id, so it stays put across refresh/re-sort) |
| `Enter` | open the focused run, three outcomes by liveness: a **live/ghost** row backed by a real dispatch lane opens a cmux tab that attaches it (close the tab to detach, the lane lives on); a row live only in its own cmux surface (a hands-on run, no lane) gets a message pointing at that cmux tab; anything else (finished, stale, or not yet matched) opens its newest transcript, **rendered as readable turns** (user/assistant text, tool calls, results, not the raw JSON), in a pager tab, if one exists |
| `p` | open the run's PR in a cmux browser tab (set `AGENT_DASHBOARD_PR_URL_BASE`) |
| `t` | open the run's issue in a cmux browser tab (set `AGENT_DASHBOARD_ISSUE_URL_BASE`) |
| `q` / `Ctrl-C` | quit (restores the terminal, even on SIGTERM/SIGHUP) |

Actions require **cmux** (`brew install cmux`); without it the action prints an install hint and no-ops. An unhandled key shows a transient status line, never a silent no-op.

## Quickstart

No install, no setup beyond `python3`. From this directory:

1. **Watch it:** `./run.sh` (Ctrl-C to quit). It renders an empty board until runs appear.
2. **Try it with demo data:** in another pane, `./seed-demo.sh` drops six fake runs (3 active, 1 waiting at a gate in yellow, 1 escalated in red, 1 merged). Remove them with `./seed-demo.sh --clear`.
3. **See real runs:** wire the emitter into your commands (see Wiring below), then open a fresh session with the wired commands loaded, run `./run.sh` in a pane, and drive a command in another. Its row advances live (started -> implementing -> pr-open, etc.). Or drive a whole issue with `/ship <issue>`: one `<issue>-pipeline` journey row plus each sub-command's detail rows.

## How it works

Four moving parts, joined by a filesystem convention:

- **`emit-status.sh`** - a tiny bash helper the commands call at each phase boundary. It writes one atomic JSON snapshot per run (built with python3 stdlib, no `jq` or other dependency) to `${AGENT_DASHBOARD_STATE_DIR:-~/.claude/agent-dashboard/state}/<session>.json` (schema: [`status.schema.json`](./status.schema.json)). **Best-effort by contract:** if it's absent or the state dir is unwritable it silently no-ops, so it can never break the run it observes.
- **`dashboard.py`** - polls that state dir and renders a live cross-section, plus the row cursor + action keys. Pane liveness comes from two anchored, exact-match sources per refresh: cmux rows match the emitter's captured `$CMUX_SURFACE_ID` against `cmux tree` (for hands-on runs), and dispatch lanes match against one `tmux -L <socket> list-sessions` (a row's `<issue>` / `<issue>-worker` matched to a lane named `<issue>` **exactly**, never a prefix rule, which would cross-match sibling issue numbers like `PROJ-7` vs `PROJ-76`). A live match past the stale threshold shows as `ghost`; a terminal (merged/done) row is never live. Pure stdlib Python 3; ANSI rendering; honors `NO_COLOR`.
- **`handler.sh`** - the one external action handler `dashboard.py` shells out to on an action key (`handler.sh <coord> <key> <issue> <state> <live> <pr> <worktree_path>`). It owns every side effect (cmux tabs, `tmux attach`, the transcript pager), keeping the renderer a pure viewer. **cmux-required, fire-and-forget:** always exits 0 and prints one status line the dashboard surfaces.
- **`transcript.py`** - renders a finished run's Claude Code session `.jsonl` into readable turns (user/assistant text, tool calls with a one-line arg hint, truncated tool results, a compact thinking marker) and drops the hook/mode/metadata noise the raw file is full of. `handler.sh`'s replay action pipes it into a pager (`transcript.py <file> | less -R`). Pure stdlib.

Runtime state lives in `$HOME` (`~/.claude/agent-dashboard/state/`), **never in a repo**: the code is versioned, the per-run snapshots are ephemeral and machine-local.

One optional part:

- **`overseer.py`** - reconciles rows against GitHub reality. A row with a `pr_number` can outlive its emitting session (the run ends, the PR lives on, the row shows `pr-open` forever); the overseer polls GitHub and flips rows whose PR **merged** (-> `merged`) or **closed unmerged** (-> `done`), writing through `emit-status.sh` so there is still a single state writer. Open PRs are left alone, babysit owns their live status. **No LLM, zero tokens:** one `gh pr view` call per reconcilable row per tick (default 60s; `--once` for a single pass). Run it from inside the repo whose PRs you want reconciled.

## Run

```bash
./run.sh                 # or: python3 dashboard.py
python3 overseer.py      # optional, spare pane (run from your repo): flips rows whose PR merged/closed
```

`q` or Ctrl-C to quit (it restores your terminal on exit). The dashboard shows:

- **soloists** - every run (role · issue · state · model · age · pane · note): the `state` cell is glyph-prefixed (🎬 started · 🎻 implementing · 👀 reviewing · 🙋 waiting · 📬 pr-open · 🚨 escalated · 👏 merged · ✅ done · 💀 stale); the `model` column shows which Claude model drives the run (opus/sonnet/haiku, color-coded); the `pane` column is `live`/`ghost`/`stale`/`-`. Escalated first, then `waiting` (paused at a human gate, bold yellow), active, and terminal (merged/done) dimmed then aged out after 10m. Stale runs flagged (no update in 15m); rows capped at `AGENT_DASHBOARD_MAX_ROWS` with an overflow count. The `▸` cursor marks the focused row (reverse-video bar when color is on); `j`/`k`/arrows move it, and it tracks a run by session id so it holds position across a refresh.
- **escalations** (red panel) - any run in `escalated` state (a hard stop, a failure). Routine gate-waits show as `waiting`, not here, red is earned.
- footer - load average, the key legend, the state dir, refresh interval.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `AGENT_DASHBOARD_STATE_DIR` | `~/.claude/agent-dashboard/state` | where snapshots are written/read (emitter + dashboard must agree) |
| `AGENT_DASHBOARD_MODEL` | unset | overrides the model shown for a run when `--model` isn't passed (else auto-detected from cmux's launch argv) |
| `AGENT_DASHBOARD_REFRESH` | `2` | dashboard refresh seconds |
| `AGENT_DASHBOARD_STALE_SECS` | `900` | a non-terminal run with no update past this is flagged stale |
| `AGENT_DASHBOARD_AGEOUT_SECS` | `600` | merged/done runs drop from view after this |
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
./dashboard.test.sh      # emoji alignment, the row cursor, and exact-match lane liveness
./transcript.test.sh     # replay renderer keeps user/assistant turns, drops metadata noise
./dispatch.test.sh       # claim -> worktree -> lane, with fail-closed rollback (shims git/tmux/claim)
./check-generic.sh       # asserts no project-specific branding leaked in
```

## Scope + what's next

v1 observes manual runs and navigates them (cursor + open-in-a-tab); it still drives nothing on its own. Deliberately deferred:

- a **cron + daemon** for autonomous overnight dispatch, only if a groomed backlog justifies the cost.
- a **`textual`** upgrade if you later want richer interactivity (scroll, mouse) instead of the refresh + raw-mode input loop.
