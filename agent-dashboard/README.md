# Agent dashboard

A live, read-only terminal view of your agent runs. Leave it open in one pane and watch
`implement` / `code-review` / `pr-prep` / `babysit-pr` (or a whole `/ship` run) work in others —
which issue, which role, what state, how long, and which ones need you.

It **observes**: you drive the runs manually in cmux/tmux panes, the dashboard just watches. No
daemon, no cron, no persistent agents.

## Quickstart

No install beyond `python3`. From this directory:

1. **Watch it:** `./run.sh` (Ctrl-C to quit). It renders an empty board until runs appear.
2. **Try it with demo data:** in another pane, `./seed-demo.sh` drops six fake runs (3 active, 1
   waiting at a gate in yellow, 1 escalated in red, 1 merged). Remove them with `./seed-demo.sh --clear`.
3. **See real runs:** wire the emitter into your commands (see Wiring below). Claude Code caches
   commands at session start, so open a fresh session with the wired commands loaded, run `./run.sh`
   in a pane, and drive a command in another. Its row advances live. Or drive a whole issue with
   `/ship <issue>` — one `<issue>-pipeline` journey row plus each sub-command's detail rows.

## How it works

Two moving parts, joined by a filesystem convention:

- **`emit-status.sh`** — a tiny bash helper the commands call at each phase boundary. It writes one
  atomic JSON snapshot per run (built with python3 stdlib — no `jq` or other dependency) to
  `${AGENT_DASHBOARD_STATE_DIR:-~/.claude/agent-dashboard/state}/<session>.json` (schema:
  [`status.schema.json`](./status.schema.json)). **Best-effort by contract:** if it's absent or the
  state dir is unwritable it silently no-ops, so it can never break the run it observes.
- **`dashboard.py`** — polls that state dir (+ tmux/cmux for pane liveness: cmux rows match the
  emitter's captured `$CMUX_SURFACE_ID` against `cmux tree`, anchored, no title matching) and renders
  a live cross-section. Pure stdlib Python 3 (no `pip install`). ANSI rendering; honors `NO_COLOR`.

Runtime state lives in `$HOME` (`~/.claude/agent-dashboard/state/`), **never in a repo** — the code
is versioned, the per-run snapshots are ephemeral and machine-local.

One optional third part:

- **`overseer.py`** — reconciles rows against GitHub reality. A row with a `pr_number` can outlive its
  emitting session (the run ends, the PR lives on, the row shows `pr-open` forever); the overseer polls
  GitHub and flips rows whose PR **merged** (→ `merged`) or **closed unmerged** (→ `done`), writing
  through `emit-status.sh` so there is still a single state writer. Open PRs are left alone — babysit
  owns their live status. **No LLM, zero tokens** — one `gh pr view` call per reconcilable row per tick
  (default 60s; `--once` for a single pass). Run it from inside the repo whose PRs you want reconciled.

## Run

```bash
./run.sh                 # or: python3 dashboard.py
python3 overseer.py      # optional, spare pane (run from your repo): flips rows whose PR merged/closed
```

Ctrl-C to quit (it restores your terminal on exit). The dashboard shows:

- **runs** — every run (role · issue · state · model · age · pane · note): the `model` column shows which Claude model drives the run (opus/sonnet/haiku, color-coded), auto-detected per run. Escalated first, then `waiting`
  (paused at a human gate, bold yellow), active, and terminal (merged/done) dimmed then aged out after
  10m. Stale runs flagged (no update in 15m); rows capped at `AGENT_DASHBOARD_MAX_ROWS` with an overflow count.
- **escalations** (red panel) — any run in `escalated` state. Routine gate-waits show as `waiting`, not
  here — red is earned.
- footer — load average, the state dir, refresh interval.

## Environment

| Var | Default | Meaning |
|---|---|---|
| `AGENT_DASHBOARD_STATE_DIR` | `~/.claude/agent-dashboard/state` | where snapshots are written/read (emitter + dashboard must agree) |
| `AGENT_DASHBOARD_HOME` | unset | if set, commands resolve `emit-status.sh` from here (else they look on `PATH`) |
| `AGENT_DASHBOARD_MODEL` | unset | overrides the model shown for a run when `--model` isn't passed (else auto-detected from cmux's launch argv) |
| `AGENT_DASHBOARD_REFRESH` | `2` | dashboard refresh seconds |
| `AGENT_DASHBOARD_STALE_SECS` | `900` | a non-terminal run with no update past this is flagged stale |
| `AGENT_DASHBOARD_AGEOUT_SECS` | `600` | merged/done runs drop from view after this |
| `AGENT_DASHBOARD_MAX_ROWS` | `30` | max table rows rendered; overflow shown as a "+N more" line |
| `AGENT_DASHBOARD_DEBUG` | unset | when set, `emit-status.sh` prints why it no-op'd (else silent) |
| `NO_COLOR` | unset | standard — disables ANSI color |

## Wiring

The commands emit best-effort at their phase transitions (each has a "Dashboard status" section). For
them to find the emitter from inside *your* project (a different repo than this one), either:

- symlink the emitter onto your `PATH`: `ln -s "$PWD/emit-status.sh" ~/.local/bin/emit-status.sh`, or
- export `AGENT_DASHBOARD_HOME=/path/to/agent-dashboard`.

| Command | Role | States it emits |
|---|---|---|
| `plan` | planner | started |
| `implement` | worker | started → implementing (· escalated on a hard stop) |
| `code-review` | reviewer | reviewing → done |
| `pr-prep` | finisher | reviewing → pr-open |
| `babysit-pr` | finisher | pr-open → merged (· escalated on a required-gate stop) |
| `/ship` | planner → worker → reviewer → finisher (one `<issue>-pipeline` journey row) | started → waiting (each gate) → implementing → reviewing → pr-open → done |

A teammate who hasn't set up the dashboard sees zero change — the emit calls no-op.

## Test

```bash
./emit-status.test.sh    # contract test for emit-status.sh (needs jq for assertions only)
./check-generic.sh       # asserts no project-specific branding leaked in
```

## Scope + what's next

v1 is observe-only over manual runs. Deliberately deferred:

- a **cron + daemon** for autonomous overnight dispatch — only if a groomed backlog justifies the cost.
- a **`textual`** upgrade if you later want interactivity (jump-to-pane, scroll) instead of a refreshing view.
