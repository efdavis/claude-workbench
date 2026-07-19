#!/usr/bin/env python3
"""Agent orchestra dashboard — a live view of, and index into, background agent lanes.

Watches the status snapshots written by emit-status.sh (see status.schema.json) and
renders a live cross-section of what every run is doing: which issue, which role, what
state, how long, and which ones need you (escalations). Music-themed: the dashboard is
the Orchestra, each run is a soloist, the dispatch lane spawner is the cue.

It is a viewer that ROUTES: a row cursor (j/k or arrows) plus open keys (Enter / p / t)
that shell out to handler.sh (cmux tabs, tmux attach), `n` which expands the focused
row's note inline (local only), and `r` which reaps a card — for a live/ghost row that
means open+end the pane first (handler closes the cmux surface / kills the lane), then
remove the snapshot; for a finished/stale row it only removes the card. A malformed or
half-written snapshot is skipped, never fatal.

Point it at the same state dir the emitter uses (AGENT_DASHBOARD_STATE_DIR, default
~/Projects/claude-workbench/agent-dashboard/state) and leave it open in a pane while
dispatch spawns lanes on the private `tmux -L <socket>` socket
(AGENT_DASHBOARD_TMUX_SOCKET). Account-wide quota mirrors (claude 5h+7d, codex 7d,
grok SuperGrok weekly) live under the sibling quota/ dir and are dual-written to /tmp
for back-compat. Any vendor whose data is absent (no ~/.codex, no ~/.grok) simply
doesn't render — so this is vendor-neutral out of the box on an enterprise-Claude-only
machine.

Pure stdlib on purpose — no pip install. Box-drawing panels + ANSI; honors NO_COLOR.

Keys:  j/k or ↑/↓ move · Enter open (jump to a cmux run's tab / attach a live lane /
       replay a finished one) · p open PR · t open issue · n note (expand the focused
       row's full note — the table column truncates) · r reap (end a live/ghost run and
       remove its card; or just clear a dead/stale card) · q or Ctrl-C quit
Run:   python3 dashboard.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from datetime import datetime
from pathlib import Path

# Shared harness home under ~/Projects so every project (Emberfall, etc.) sees
# the same board + durable quota. Override with AGENT_DASHBOARD_HOME.
_DASHBOARD_HOME = Path(os.environ.get(
    "AGENT_DASHBOARD_HOME",
    str(Path.home() / "Projects" / "claude-workbench" / "agent-dashboard"),
)).expanduser()
STATE_DIR = Path(os.environ.get("AGENT_DASHBOARD_STATE_DIR",
                                str(_DASHBOARD_HOME / "state"))).expanduser()
QUOTA_DIR = Path(os.environ.get("AGENT_DASHBOARD_QUOTA_DIR",
                                str(_DASHBOARD_HOME / "quota"))).expanduser()
# Optional pilot-light sidecar (see ../../emberfall/pilot-light). When this points
# at a configured pilot-light dir, the dashboard co-launches its loop.sh and shows
# a pilot panel. Unset -> the dashboard is exactly its old view-only self.
PILOT_DIR = Path(os.environ["PILOT_LIGHT_DIR"]).expanduser() if os.environ.get("PILOT_LIGHT_DIR") else None
REFRESH_SECS = float(os.environ.get("AGENT_DASHBOARD_REFRESH", "2"))
STALE_SECS = int(os.environ.get("AGENT_DASHBOARD_STALE_SECS", str(15 * 60)))
# Terminal (merged/done) rows persist as tombstones until reaped; left alone they pile up
# (a week of dead cards, each re-read every refresh). Auto-reap the ones older than this so
# the board self-cleans. Default 2 days; set <=0 to disable and reap only by hand with `r`.
REAP_TERMINAL_HOURS = float(os.environ.get("AGENT_DASHBOARD_REAP_TERMINAL_HOURS", "48"))
# A run going `escalated` is the one event that needs you NOW — but you only see it if
# you're already staring at the board. Ring the terminal bell when a NEW escalation
# appears (transition, not every frame it stays up). Also post a macOS notification when
# AGENT_DASHBOARD_ESCALATION_NOTIFY=1. Bell honors NO_BELL / AGENT_DASHBOARD_NO_BELL.
ESCALATION_NOTIFY = os.environ.get("AGENT_DASHBOARD_ESCALATION_NOTIFY") == "1"
ESCALATION_BELL = not ("NO_BELL" in os.environ or "AGENT_DASHBOARD_NO_BELL" in os.environ)
# Neutral default (matches upstream). This board benefits from more width — it carries two
# extra columns (ctx + cost) — so widen it per-machine with AGENT_DASHBOARD_MAX_WIDTH
# rather than baking a wide default into the shared repo.
MAX_WIDTH = int(os.environ.get("AGENT_DASHBOARD_MAX_WIDTH", "140"))
MAX_ROWS = int(os.environ.get("AGENT_DASHBOARD_MAX_ROWS", "30"))
# Private tmux socket dispatch spawns lanes on; overridable for tests / parallel setups.
TMUX_SOCKET = os.environ.get("AGENT_DASHBOARD_TMUX_SOCKET", "agent-lanes")
HANDLER = str(Path(__file__).resolve().parent / "handler.sh")
EMIT = str(Path(__file__).resolve().parent / "emit-status.sh")

TERMINAL_STATES = {"merged", "done"}
COLOR = ("NO_COLOR" not in os.environ)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_C = {"reset": "0", "bold": "1", "dim": "2", "red": "31", "green": "32",
      "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
      "orange": "38;5;208", "teal": "38;5;37"}
STATE_COLOR = {
    "started": ("cyan",), "implementing": ("yellow",), "reviewing": ("blue",),
    "waiting": ("bold", "yellow"), "pr-open": ("magenta",), "merged": ("green",),
    "done": ("green", "dim"), "escalated": ("bold", "red"),
}
# A per-state glyph prefixes the state text in the same column. 🙋/🚨 stay the alarm
# pair — red is earned. 💀 is the derived "stale" pseudo-state (no live snapshot).
STATE_GLYPH = {
    "started": "🎬", "implementing": "🎻", "reviewing": "👀", "waiting": "🙋",
    "pr-open": "📬", "escalated": "🚨", "merged": "👏", "done": "✅", "stale": "💀",
}
ROLE_GLYPH = {"planner": "P", "worker": "W", "reviewer": "R",
              "finisher": "F", "groomer": "G", "other": "-"}
MODEL_COLOR = {"opus": ("magenta",), "sonnet": ("cyan",), "haiku": ("green",),
               "grok": ("orange",), "codex": ("teal",)}
PANE_COLOR = {"live": ("green",), "ghost": ("yellow",), "stale": ("red",)}


def model_cell(model: str) -> tuple[str, tuple[str, ...]]:
    """Short display name + color for a model string; matches on family substring
    so both 'opus' and 'claude-opus-4-8' render as 'opus'. Unknown -> raw + dim.
    Not all runs are Claude: peer seats emit model=grok or model=codex."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "grok", "codex"):
        if fam in m:
            return fam, MODEL_COLOR[fam]
    return (model or "-"), ("dim",)


COST_GLOB = "/tmp/claude-cost-usd-surface-*.txt"
_COST_PREFIX = "claude-cost-usd-surface-"
# Session-keyed mirrors (Grok seats and any non-cmux agent). Same units as surface files.
COST_SESSION_GLOB = "/tmp/claude-cost-usd-session-*.txt"
_COST_SESSION_PREFIX = "claude-cost-usd-session-"
# $ thresholds for the cost column; RED tracks the auto-lane's $100 handoff cap, so a
# red cell means "this lane is about to hand off", not just "expensive".
COST_WARN, COST_HOT = 25.0, 75.0


def _read_float_glob(pattern: str, prefix: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for path in glob.glob(pattern):
        key = os.path.basename(path)[len(prefix):-len(".txt")]
        try:
            out[key.upper()] = float(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
    return out


def read_costs() -> dict[str, float]:
    """Cumulative USD keyed by cmux surface id *and* dashboard session id.

    Surface keys: statusline.sh mirrors ($CMUX_SURFACE_ID) for Claude under cmux.
    Session keys: `/tmp/claude-cost-usd-session-<session>.txt` for non-cmux agents
    (Grok seats emit `session=ember-grok-N` with no surface). Same best-effort contract:
    unreadable/absent -> skipped; render joins surface first, then session (see
    `gauge_for`)."""
    costs = _read_float_glob(COST_GLOB, _COST_PREFIX)
    costs.update(_read_float_glob(COST_SESSION_GLOB, _COST_SESSION_PREFIX))
    return costs


def cost_cell(usd: "float | None") -> tuple[str, tuple[str, ...]]:
    """Display string + color for a run's spend. Unknown -> '-' + dim."""
    if usd is None:
        return "-", ("dim",)
    if usd >= COST_HOT:
        return f"${usd:,.2f}", ("bold", "red")
    if usd >= COST_WARN:
        return f"${usd:,.2f}", ("yellow",)
    return f"${usd:,.2f}", ("green",)


# --- Accurate, subagent-inclusive cost via cost.py's transcript walk ---------------
# The statusline mirror (read_costs) is Claude's own `total_cost_usd`: it under-reports
# fan-out because it misses subagent/Workflow spend, and it freezes whenever the pane
# stops rendering a statusline. cost.py reads the run's transcript (+ every nested
# subagent transcript) and prices it at list rates — that IS the billing basis, so it's
# accurate and can't go stale. We run it here, throttled, and let it override the mirror
# per row; rows we can't resolve (no surface bridge, non-cmux, grok seats) keep the
# mirror value. Vendor-neutral: a machine without cost.py just falls back to the mirror.
COST_PY = Path(os.environ.get(
    "AGENT_DASHBOARD_COST_PY",
    str(Path(__file__).resolve().parent.parent / "cmux" / "cost.py")))
SESSIONID_SURFACE_GLOB = "/tmp/claude-sessionid-surface-*.txt"
_SESSIONID_SURFACE_PREFIX = "claude-sessionid-surface-"
ACCURATE_COST_REFRESH_SECS = int(os.environ.get("AGENT_DASHBOARD_COST_REFRESH", "30"))
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
_accurate_cost_cache: dict[str, float] = {}
_accurate_cost_checked_at = 0.0
_accurate_ctx_cache: dict[str, int] = {}
_accurate_ctx_checked_at = 0.0


# A Claude session id is a canonical UUID. The bridge value comes from a world-writable
# /tmp file and is interpolated into a glob and a subprocess arg, so validate its shape
# here (the single read choke point) before it can steer either — a `..`/glob-metachar
# value must never reach _cost_for_session.
_SESSION_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _surface_to_sessionid() -> dict[str, str]:
    """UPPER(surface id) -> Claude session UUID, from the statusline bridge mirrors.
    Only well-formed UUIDs are kept; a malformed mirror value is skipped, never used."""
    out: dict[str, str] = {}
    for path in glob.glob(SESSIONID_SURFACE_GLOB):
        surface = os.path.basename(path)[len(_SESSIONID_SURFACE_PREFIX):-len(".txt")]
        try:
            uuid = Path(path).read_text().strip()
        except OSError:
            continue
        if uuid and _SESSION_UUID_RE.match(uuid):
            out[surface.upper()] = uuid
    return out


def _cost_for_session(uuid: str) -> "float | None":
    """Accurate USD for one Claude session, subagents included, via cost.py. Locates the
    project dir by globbing the UUID's top-level transcript (no slug math), then shells
    out with a timeout. Any failure (no transcript, cost.py missing, bad JSON, timeout)
    -> None so the caller falls back to the mirror. Never raises."""
    matches = glob.glob(str(_CLAUDE_PROJECTS / "*" / f"{uuid}.jsonl"))
    if not matches or not COST_PY.exists():
        return None
    project_dir = str(Path(matches[0]).parent)
    try:
        r = subprocess.run(
            [sys.executable, str(COST_PY), "--project-dir", project_dir,
             "--session", uuid, "--json"],
            capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return None
        total = json.loads(r.stdout).get("total_usd")
        return float(total) if total is not None else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


# Model context-window size (tokens) for the live ctx gauge. The account default is 200k;
# a row's transcript names its model, so a wider-window model could be special-cased later.
_CTX_WINDOW_TOKENS = 200_000


def _ctx_for_session(uuid: str) -> "int | None":
    """Live context-window % for one Claude session from the LAST assistant `usage` block
    in its top-level transcript: input + cache-read + cache-creation tokens = the prompt
    size sent that turn, i.e. current window occupancy. That's the same basis the statusline
    shows for interactive rows — computed locally here so a headless `--print` lane (which
    renders no statusline, hence no /tmp ctx mirror) still gets a live ctx gauge. Only the
    top-level transcript matters: ctx is the MAIN session's window, not its subagents'. Any
    failure (no transcript, unreadable, no usage) -> None. Never raises."""
    matches = glob.glob(str(_CLAUDE_PROJECTS / "*" / f"{uuid}.jsonl"))
    if not matches:
        return None
    usage = None
    try:
        # Files run to MBs; the `"usage"` substring pre-filter keeps this to a JSON parse
        # per assistant turn, not per line. Last matching record wins (most recent turn).
        with open(matches[0], "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                u = rec.get("message", {}).get("usage")
                if isinstance(u, dict) and ("input_tokens" in u or "cache_read_input_tokens" in u):
                    usage = u
    except OSError:
        return None
    if not usage:
        return None
    tokens = (int(usage.get("input_tokens", 0) or 0)
              + int(usage.get("cache_read_input_tokens", 0) or 0)
              + int(usage.get("cache_creation_input_tokens", 0) or 0))
    if tokens <= 0:
        return None
    return max(0, min(100, int(round(tokens / _CTX_WINDOW_TOKENS * 100))))


def _own_session_rows(snaps: list[dict]):
    """Yield (UPPER(dashboard session id), Claude session uuid) for non-terminal rows that
    carry their own `claude_session_id` — headless lanes that minted their session up front
    and have no statusline mirror. Keyed by the DASHBOARD session id (not surface) because
    that's the join key these rows resolve their gauges on (`gauge_for` falls back to it when
    the surface has no live mirror, which is exactly the headless case)."""
    for s in snaps:
        if s.get("state") in TERMINAL_STATES:
            continue
        csid = str(s.get("claude_session_id", "")).strip()
        if csid and _SESSION_UUID_RE.match(csid):
            key = str(s.get("session", "")).upper()
            if key:
                yield key, csid


def read_accurate_costs(snaps: list[dict], now: int | float) -> dict[str, float]:
    """gauge_key -> accurate USD, for the non-terminal rows we can bridge to a Claude
    session. Two sources, own-session first (authoritative, no /tmp): rows with a
    `claude_session_id` resolve keyed by their dashboard session id; the rest use the
    statusline surface->sessionid bridge, keyed by surface (original behavior). Throttled to
    ACCURATE_COST_REFRESH_SECS (cost.py walks transcripts, so it's not a per-2s cost); the
    cache paints between refreshes."""
    global _accurate_cost_checked_at
    now_f = float(now)
    # Gate on WHEN we last checked, not on whether the cache has entries: a window where
    # nothing resolved (cost.py erroring, transcript not yet flushed, no bridgeable row)
    # leaves the cache empty, and gating on emptiness would re-run the glob + a subprocess
    # per row every single frame — defeating the throttle exactly in the failure case.
    # `_accurate_cost_checked_at` starts at 0.0 (falsy), so the first call still computes.
    if _accurate_cost_checked_at and now_f - _accurate_cost_checked_at < ACCURATE_COST_REFRESH_SECS:
        return dict(_accurate_cost_cache)
    _accurate_cost_checked_at = now_f
    fresh: dict[str, float] = {}
    # 1) rows with their own session id (headless lanes) — keyed by dashboard session id
    for key, uuid in _own_session_rows(snaps):
        usd = _cost_for_session(uuid)
        if usd is not None:
            fresh[key] = usd
    # 2) statusline surface bridge for the rest (interactive cmux rows) — keyed by surface
    bridge = _surface_to_sessionid()
    for s in snaps:
        if s.get("state") in TERMINAL_STATES:
            continue
        if str(s.get("claude_session_id", "")).strip():
            continue  # own-session row already handled in (1)
        surface = str(s.get("cmux_surface", "")).upper()
        uuid = bridge.get(surface) if surface else None
        if not uuid:
            continue
        usd = _cost_for_session(uuid)
        if usd is not None:
            fresh[surface] = usd
    _accurate_cost_cache.clear()
    _accurate_cost_cache.update(fresh)
    return fresh


def read_accurate_ctx(snaps: list[dict], now: int | float) -> dict[str, int]:
    """UPPER(dashboard session id) -> live context-window %, for headless rows carrying a
    `claude_session_id`. Only these rows: interactive/cmux rows have an authoritative
    statusline ctx mirror (read_ctx), and the transcript derivation here is a 200k-window
    approximation, so it fills ONLY rows that otherwise show '-'. Same throttle/cache shape
    as read_accurate_costs; the transcript read is cheap (local last-usage scan, no subprocess)."""
    global _accurate_ctx_checked_at
    now_f = float(now)
    if _accurate_ctx_checked_at and now_f - _accurate_ctx_checked_at < ACCURATE_COST_REFRESH_SECS:
        return dict(_accurate_ctx_cache)
    _accurate_ctx_checked_at = now_f
    fresh: dict[str, int] = {}
    for key, uuid in _own_session_rows(snaps):
        pct = _ctx_for_session(uuid)
        if pct is not None:
            fresh[key] = pct
    _accurate_ctx_cache.clear()
    _accurate_ctx_cache.update(fresh)
    return fresh


CTX_GLOB = "/tmp/claude-context-pct-surface-*.txt"
_CTX_PREFIX = "claude-context-pct-surface-"
CTX_SESSION_GLOB = "/tmp/claude-context-pct-session-*.txt"
_CTX_SESSION_PREFIX = "claude-context-pct-session-"
CTX_BAR_CELLS = 8
# Context-window fill thresholds, same ladder statusline.sh colors on. RED means the run
# is near auto-compact, i.e. about to lose fidelity — not merely "a long session".
CTX_WARN, CTX_HOT, CTX_CRIT = 30, 60, 80


def _read_int_glob(pattern: str, prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for path in glob.glob(pattern):
        key = os.path.basename(path)[len(prefix):-len(".txt")]
        try:
            out[key.upper()] = int(float(Path(path).read_text().strip()))
        except (OSError, ValueError):
            continue
    return out


def read_ctx() -> dict[str, int]:
    """Context-window % used, keyed by cmux surface id *and* dashboard session id.

    Surface keys: statusline.sh (Claude). Session keys: Grok seat hook writes
    `/tmp/claude-context-pct-session-ember-grok-N.txt` from signals.json
    `contextWindowUsage`. Join prefers surface, then session (see `gauge_for`)."""
    pcts = _read_int_glob(CTX_GLOB, _CTX_PREFIX)
    pcts.update(_read_int_glob(CTX_SESSION_GLOB, _CTX_SESSION_PREFIX))
    return pcts


def gauge_for(snap: dict, gauges: dict):
    """Look up a gauge for a run: cmux_surface first, then dashboard session id.

    Grok seats have no cmux_surface; Claude under cmux has both (surface wins when both
    exist so a hijacked/stale session key cannot override a live surface mirror)."""
    surface = str(snap.get("cmux_surface", "")).upper()
    if surface and surface in gauges:
        return gauges[surface]
    session = str(snap.get("session", "")).upper()
    if session and session in gauges:
        return gauges[session]
    return None


def ctx_cell(pct: "int | None", bar_cells: int = CTX_BAR_CELLS) -> tuple[str, tuple[str, ...]]:
    """Mini fill-bar + percent for a run's context window. bar_cells=0 drops the bar and
    shows the bare percent, which is how this column survives a narrow pane without
    starving `note`. Unknown -> '-' + dim."""
    if pct is None:
        return "-", ("dim",)
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * bar_cells)
    bar = ("█" * filled + "░" * (bar_cells - filled) + " ") if bar_cells else ""
    if pct > CTX_CRIT:
        color = ("bold", "red")
    elif pct > CTX_HOT:
        color = ("orange",)
    elif pct > CTX_WARN:
        color = ("yellow",)
    else:
        color = ("green",)
    return f"{bar}{pct:>3}%", color


# Account-wide quota mirrors — durable under QUOTA_DIR, dual-written to /tmp so
# older readers (and anything that still greps /tmp) keep working.
# Claude: statusline.sh from rate_limits.five_hour / seven_day.
# Codex: dashboard refreshes from ChatGPT wham/usage (or latest rollout fallback).
# Grok: dashboard refreshes SuperGrok *weekly* pool from
#       grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig (Connect/protobuf).
#       That is the "Weekly SuperGrok Limit" bar in grok.com Settings → Usage
#       (Grok Build + Imagine + API share one weekly %). There is no 5h window.
RATE_LIMIT_QUOTA = "claude-5h.txt"       # durable name
RATE_LIMIT_TMP = "/tmp/claude-rate-limit-5h.txt"
CLAUDE_WEEKLY_QUOTA = "claude-7d.txt"
CLAUDE_WEEKLY_TMP = "/tmp/claude-rate-limit-7d.txt"
CODEX_WEEKLY_QUOTA = "codex-7d.txt"
CODEX_WEEKLY_TMP = "/tmp/codex-rate-limit-7d.txt"
GROK_WEEKLY_QUOTA = "grok-7d.txt"
GROK_WEEKLY_TMP = "/tmp/grok-rate-limit-7d.txt"
# Optional monthly Grok Build API-credit pool (cli-chat-proxy /v1/billing) — different
# metric from SuperGrok weekly; kept as a read fallback only.
GROK_MONTHLY_QUOTA = "grok-mo.txt"
GROK_MONTHLY_TMP = "/tmp/grok-rate-limit-mo.txt"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GROK_AUTH_FILE = Path.home() / ".grok" / "auth.json"
GROK_TOKEN_URL = "https://auth.x.ai/oauth2/token"
# SuperGrok weekly pool (Settings → Usage). Accepts Grok CLI OIDC Bearer.
GROK_WEEKLY_USAGE_URL = (
    "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
)
# Don't hammer OpenAI / xAI on every 2s refresh; 60s is plenty for a quota bar.
CODEX_WEEKLY_REFRESH_SECS = int(os.environ.get("AGENT_DASHBOARD_CODEX_WEEKLY_REFRESH", "60"))
GROK_WEEKLY_REFRESH_SECS = int(os.environ.get(
    "AGENT_DASHBOARD_GROK_WEEKLY_REFRESH",
    os.environ.get("AGENT_DASHBOARD_GROK_MONTHLY_REFRESH", "60"),  # back-compat
))
QUOTA_BAR_CELLS = 10
# The 5-hour plan usage limit ("Current session" in claude.ai settings) — this is the one
# that actually stops work when it hits 100, so RED starts well before the wall.
QUOTA_WARN, QUOTA_HOT, QUOTA_CRIT = 50, 75, 90
_codex_weekly_checked_at = 0.0
_grok_weekly_checked_at = 0.0


def _quota_path(name: str) -> Path:
    return QUOTA_DIR / name


def read_pct_reset(path: str | Path) -> "tuple[int, int] | None":
    """(usage %, reset epoch) from a two-field mirror file. Unreadable/absent -> None."""
    try:
        pct, reset = Path(path).read_text().split()
        return int(float(pct)), int(float(reset))
    except (OSError, ValueError):
        return None


def read_quota(durable_name: str, tmp_path: str) -> "tuple[int, int] | None":
    """Prefer durable Projects/quota mirror; fall back to /tmp (live statusline)."""
    return read_pct_reset(_quota_path(durable_name)) or read_pct_reset(tmp_path)


def read_rate_limit() -> "tuple[int, int] | None":
    """(5h usage %, reset epoch) as mirrored by statusline.sh from Claude's own
    rate_limits payload. Account-wide, so any session's mirror speaks for all of them.
    Same best-effort contract as the other readers: unreadable/absent -> None."""
    return read_quota(RATE_LIMIT_QUOTA, RATE_LIMIT_TMP)


def _write_pct_reset(path: str | Path, pct: int, reset: int) -> None:
    """Write one mirror file. Best-effort; never raises."""
    try:
        p = Path(path)
        if p.parent and str(p.parent) not in ("", "."):
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{int(pct)} {int(reset)}\n")
    except OSError:
        pass


def _update_quota_snapshot(key: str, pct: int, reset: int) -> None:
    """Refresh quota/snapshot.json so humans can eyeball last-known harness %.

    Best-effort; never raises. Keys: claude_5h, claude_7d, codex_7d, grok_7d
    (optional grok_mo monthly credit mirror is still recorded if written).
    """
    snap_path = _quota_path("snapshot.json")
    try:
        try:
            snap = json.loads(snap_path.read_text())
            if not isinstance(snap, dict):
                snap = {}
        except (OSError, ValueError):
            snap = {}
        snap["updated_at"] = int(time.time())
        snap[key] = {"pct": int(pct), "reset_epoch": int(reset)}
        sources = snap.get("sources")
        if not isinstance(sources, dict):
            sources = {}
        sources[key] = str(_quota_path({
            "claude_5h": RATE_LIMIT_QUOTA,
            "claude_7d": CLAUDE_WEEKLY_QUOTA,
            "codex_7d": CODEX_WEEKLY_QUOTA,
            "grok_mo": GROK_MONTHLY_QUOTA,
            "grok_7d": GROK_WEEKLY_QUOTA,
        }.get(key, key + ".txt")))
        snap["sources"] = sources
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(snap, indent=2) + "\n")
    except OSError:
        pass


def write_quota(durable_name: str, tmp_path: str, pct: int, reset: int,
                snapshot_key: str | None = None) -> None:
    """Dual-write durable Projects/quota + /tmp mirror. Best-effort."""
    _write_pct_reset(_quota_path(durable_name), pct, reset)
    _write_pct_reset(tmp_path, pct, reset)
    if snapshot_key:
        _update_quota_snapshot(snapshot_key, pct, reset)


def _quota_color(pct: int) -> tuple[str, ...]:
    if pct > QUOTA_CRIT:
        return ("bold", "red")
    if pct > QUOTA_HOT:
        return ("orange",)
    if pct > QUOTA_WARN:
        return ("yellow",)
    return ("green",)


def _format_resets(reset: int, now: int) -> str:
    left = max(0, reset - now)
    if left >= 86400:
        d, rem = divmod(left, 86400)
        return f"{d}d{rem // 3600:02d}h"
    if left >= 3600:
        return f"{left // 3600}h{(left % 3600) // 60:02d}m"
    return f"{left // 60}m"


def _quota_bar(pct: int) -> str:
    """The ██░░ fill for a usage %, colored by the same ladder as the rest of the board."""
    pct = max(0, min(100, pct))
    color = _quota_color(pct)
    filled = round(pct / 100 * QUOTA_BAR_CELLS)
    return c("█" * filled, *color) + c("░" * (QUOTA_BAR_CELLS - filled), "dim")


def _quota_win(label: str, rl: "tuple[int, int] | None", now: int,
               label_styles: tuple[str, ...] = ("dim",)) -> str:
    """One window's cell — '5h ██████░░░░ 62% used·4h30'. rl is (pct, reset) or None; an
    absent mirror renders '<label> —' so the slot still shows which window is missing.

    "used" stays explicit because a bare "0%" next to a mostly empty bar reads as
    "0% left" — the opposite of what it means — right when it matters most (a near-empty
    quota looks alarming instead of reassuring)."""
    if rl is None:
        return c(label, *label_styles) + " " + c("—", "dim")
    pct, reset = rl
    pct = max(0, min(100, pct))
    return (c(label, *label_styles) + " " + _quota_bar(pct) + " "
            + c(f"{pct}% used", *_quota_color(pct))
            + c(f"·{_format_resets(reset, now)}", "dim"))


def _refresh_codex_weekly_from_api() -> bool:
    """Pull Codex weekly (primary window) % from ChatGPT wham/usage. Returns True on write."""
    try:
        auth = json.loads(CODEX_AUTH_FILE.read_text())
    except (OSError, ValueError):
        return False
    tokens = auth.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        return False
    account_id = tokens.get("account_id") or ""
    try:
        import urllib.request
        req = urllib.request.Request(
            CODEX_USAGE_URL,
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
                "User-Agent": "agent-dashboard",
                **({"ChatGPT-Account-ID": account_id} if account_id else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return False
    window = (data.get("rate_limit") or {}).get("primary_window") or {}
    try:
        pct = int(float(window["used_percent"]))
        reset = int(float(window.get("reset_at")
                          or (time.time() + float(window.get("reset_after_seconds", 0)))))
    except (KeyError, TypeError, ValueError):
        return False
    write_quota(CODEX_WEEKLY_QUOTA, CODEX_WEEKLY_TMP, pct, reset, "codex_7d")
    return True


def _refresh_codex_weekly_from_rollouts() -> bool:
    """Fallback: newest payload.rate_limits.primary in ~/.codex/sessions rollouts."""
    if not CODEX_SESSIONS_DIR.is_dir():
        return False
    # sessions/YYYY/MM/DD/rollout-*.jsonl — take the newest few files by mtime.
    try:
        rollouts = sorted(
            CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:8]
    except OSError:
        return False
    for path in rollouts:
        try:
            # Tail-scan: rate_limits appear often; last write wins.
            with path.open("r", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines[-200:]):
            if "rate_limits" not in line or "used_percent" not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            # Walk for {used_percent, resets_at} under rate_limits.primary
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    rl = cur.get("rate_limits")
                    if isinstance(rl, dict):
                        primary = rl.get("primary")
                        if isinstance(primary, dict) and "used_percent" in primary:
                            try:
                                pct = int(float(primary["used_percent"]))
                                reset = int(float(primary.get("resets_at")
                                                  or primary.get("reset_at") or 0))
                                if reset <= 0:
                                    continue
                                write_quota(CODEX_WEEKLY_QUOTA, CODEX_WEEKLY_TMP,
                                            pct, reset, "codex_7d")
                                return True
                            except (TypeError, ValueError):
                                pass
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
    return False


def ensure_codex_weekly_fresh(now: int | float | None = None) -> None:
    """Best-effort refresh of the Codex weekly mirror. Throttled; never raises.

    Fast-path out on a machine with no Codex at all (no ~/.codex/auth.json and no
    sessions dir): nothing to refresh, so skip even the throttle bookkeeping — this is
    what keeps an enterprise-Claude-only box from doing any Codex work every 2s."""
    if not CODEX_AUTH_FILE.exists() and not CODEX_SESSIONS_DIR.is_dir():
        return
    global _codex_weekly_checked_at
    now_f = float(now if now is not None else time.time())
    if now_f - _codex_weekly_checked_at < CODEX_WEEKLY_REFRESH_SECS:
        # Still refresh if both durable and /tmp mirrors are missing.
        if _quota_path(CODEX_WEEKLY_QUOTA).exists() or Path(CODEX_WEEKLY_TMP).exists():
            return
    _codex_weekly_checked_at = now_f
    if _refresh_codex_weekly_from_api():
        return
    _refresh_codex_weekly_from_rollouts()


def _grok_money_val(node) -> "float | None":
    """Unwrap Grok billing money objects ({"val": N}) or plain numbers. None if missing."""
    if isinstance(node, dict) and "val" in node:
        try:
            return float(node["val"])
        except (TypeError, ValueError):
            return None
    if isinstance(node, (int, float)):
        return float(node)
    return None


def _parse_iso_epoch(s: str) -> "int | None":
    """Parse an ISO-8601 timestamp (with Z or offset) to a unix epoch. None on failure."""
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        # datetime.fromisoformat handles "+00:00"; strptime fallback for older shapes.
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                return int(datetime.strptime(raw, fmt).timestamp())
            except ValueError:
                continue
    return None


def _grok_auth_entry() -> "tuple[str, dict] | None":
    """Return (auth.json top-level key, entry dict) for the first OIDC session, or None."""
    try:
        auth = json.loads(GROK_AUTH_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(auth, dict):
        return None
    for k, v in auth.items():
        if isinstance(v, dict) and (v.get("refresh_token") or v.get("key") or v.get("access_token")):
            return k, v
    return None


def _grok_access_token(persist_refresh: bool = True) -> "str | None":
    """Return a usable Grok OIDC access token.

    Prefer the still-valid stored access token (auth entry `key`) so we do not burn a
    refresh on every dashboard tick. When the access token is missing/expired, refresh
    via auth.x.ai and — if the IdP rotates the refresh token — write the new tokens
    back to ~/.grok/auth.json so the Grok CLI keeps working. Best-effort; never raises.
    """
    found = _grok_auth_entry()
    if not found:
        return None
    auth_key, entry = found
    access = entry.get("key") or entry.get("access_token")
    exp = _parse_iso_epoch(str(entry.get("expires_at") or ""))
    # 90s skew: refresh a little early so a long paint doesn't race the expiry.
    if access and exp is not None and exp > time.time() + 90:
        return str(access)
    if access and exp is None:
        # No expiry recorded — try the stored token; caller will fall through on 401.
        return str(access)

    refresh = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not refresh or not client_id:
        return str(access) if access else None

    try:
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }).encode()
        req = urllib.request.Request(
            GROK_TOKEN_URL,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "agent-dashboard",
            },
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            tok = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return str(access) if access else None

    new_access = tok.get("access_token")
    if not new_access:
        return str(access) if access else None

    if persist_refresh:
        try:
            auth = json.loads(GROK_AUTH_FILE.read_text())
            cur = auth.get(auth_key) if isinstance(auth, dict) else None
            if isinstance(cur, dict):
                cur = dict(cur)
                cur["key"] = new_access
                if tok.get("refresh_token"):
                    cur["refresh_token"] = tok["refresh_token"]
                expires_in = tok.get("expires_in")
                try:
                    exp_epoch = int(time.time()) + int(expires_in)
                    cur["expires_at"] = datetime.utcfromtimestamp(exp_epoch).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    )
                except (TypeError, ValueError, OSError):
                    pass
                auth[auth_key] = cur
                GROK_AUTH_FILE.write_text(json.dumps(auth, indent=2) + "\n")
        except (OSError, ValueError, TypeError):
            pass
    return str(new_access)


def _pb_read_varint(buf: bytes, i: int) -> "tuple[int, int]":
    """Read a protobuf varint; returns (value, new_index)."""
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, i


def _pb_decode_fields(buf: bytes) -> "list[tuple[int, str, object]]":
    """Decode a protobuf message into (field_no, kind, value) tuples. Best-effort."""
    fields: list[tuple[int, str, object]] = []
    i = 0
    n = len(buf)
    while i < n:
        key, i = _pb_read_varint(buf, i)
        field_no = key >> 3
        wire = key & 7
        if wire == 0:
            val, i = _pb_read_varint(buf, i)
            fields.append((field_no, "varint", val))
        elif wire == 1:
            if i + 8 > n:
                break
            fields.append((field_no, "64bit", buf[i:i + 8]))
            i += 8
        elif wire == 2:
            ln, i = _pb_read_varint(buf, i)
            if i + ln > n:
                break
            fields.append((field_no, "bytes", buf[i:i + ln]))
            i += ln
        elif wire == 5:
            if i + 4 > n:
                break
            fields.append((field_no, "32bit", buf[i:i + 4]))
            i += 4
        else:
            break
    return fields


def _pb_ts_seconds(fields: "list[tuple[int, str, object]]") -> "int | None":
    """google.protobuf.Timestamp: field 1 = seconds."""
    for n, kind, val in fields:
        if n == 1 and kind == "varint" and isinstance(val, int) and val > 1_000_000_000:
            return val
    return None


def _parse_grok_credits_proto(payload: bytes) -> "tuple[int, int] | None":
    """Parse GetGrokCreditsConfig Connect/protobuf body → (usage_pct, reset_epoch).

    Observed GrokUsageInfo shape (field numbers stable as of 2026-07):
      1: credit_usage_percent (float32)
      4: current_period.start (Timestamp)
      5: current_period.end   (Timestamp)  ← weekly SuperGrok reset
    Outer response wraps that message in field 1 (`config`).
    """
    import struct as _struct
    try:
        # Connect unary envelope: flags(1) + big-endian length(4) + message
        if len(payload) >= 5 and payload[0] in (0, 1):
            msg_len = int.from_bytes(payload[1:5], "big")
            msg = payload[5:5 + msg_len]
        else:
            msg = payload
        top = _pb_decode_fields(msg)
        # Prefer nested config (field 1 length-delimited); else treat top as the config.
        cfg_bytes = None
        for n, kind, val in top:
            if n == 1 and kind == "bytes" and isinstance(val, (bytes, bytearray)):
                cfg_bytes = bytes(val)
                break
        cfg = _pb_decode_fields(cfg_bytes if cfg_bytes is not None else msg)

        pct: "int | None" = None
        reset: "int | None" = None
        start: "int | None" = None
        for n, kind, val in cfg:
            if n == 1 and kind == "32bit" and isinstance(val, (bytes, bytearray)) and len(val) == 4:
                pct = int(round(float(_struct.unpack("<f", val)[0])))
            elif kind == "bytes" and isinstance(val, (bytes, bytearray)):
                ts = _pb_ts_seconds(_pb_decode_fields(bytes(val)))
                if ts is None:
                    continue
                if n == 4:
                    start = ts
                elif n == 5:
                    reset = ts
                elif reset is None and ts > time.time():
                    # Fallback: any future Timestamp-looking field.
                    reset = ts
        if pct is None:
            return None
        pct = max(0, min(100, pct))
        if reset is None:
            # Weekly window default: 7d from start, else 7d from now.
            reset = (start + 7 * 86400) if start else int(time.time()) + 7 * 86400
        return pct, int(reset)
    except Exception:
        return None


def _refresh_grok_weekly_from_api() -> bool:
    """Pull SuperGrok weekly usage % from GetGrokCreditsConfig. Returns True on write.

    This is the same "Weekly SuperGrok Limit" bar as grok.com Settings → Usage
    (Grok Build + Imagine + API share one weekly pool). Auth: Grok CLI OIDC Bearer
    from ~/.grok/auth.json. Protocol: Connect unary protobuf (not JSON)."""
    access = _grok_access_token()
    if not access:
        return False
    try:
        import struct as _struct
        import urllib.request
        # Empty protobuf request + Connect envelope (flags=0, length=0).
        body = _struct.pack(">BI", 0, 0)
        req = urllib.request.Request(
            GROK_WEEKLY_USAGE_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/connect+proto",
                "Accept": "application/connect+proto",
                "Connect-Protocol-Version": "1",
                "User-Agent": "agent-dashboard",
                "Origin": "https://grok.com",
                "Referer": "https://grok.com/",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = resp.read()
    except Exception:
        return False
    parsed = _parse_grok_credits_proto(payload)
    if not parsed:
        return False
    pct, reset = parsed
    write_quota(GROK_WEEKLY_QUOTA, GROK_WEEKLY_TMP, pct, reset, "grok_7d")
    return True


def ensure_grok_weekly_fresh(now: int | float | None = None) -> None:
    """Best-effort refresh of the SuperGrok weekly mirror. Throttled; never raises.

    Fast-path out when ~/.grok/auth.json is missing so Claude/Codex-only machines never
    hit xAI. Same 60s throttle shape as the Codex weekly poll."""
    if not GROK_AUTH_FILE.exists():
        return
    global _grok_weekly_checked_at
    now_f = float(now if now is not None else time.time())
    if now_f - _grok_weekly_checked_at < GROK_WEEKLY_REFRESH_SECS:
        if (_quota_path(GROK_WEEKLY_QUOTA).exists() or Path(GROK_WEEKLY_TMP).exists()
                or _quota_path(GROK_MONTHLY_QUOTA).exists() or Path(GROK_MONTHLY_TMP).exists()):
            return
    _grok_weekly_checked_at = now_f
    _refresh_grok_weekly_from_api()


def read_grok_quota() -> "tuple[int, int] | None":
    """Weekly SuperGrok mirror first; fall back to an optional monthly credit mirror."""
    return (read_quota(GROK_WEEKLY_QUOTA, GROK_WEEKLY_TMP)
            or read_quota(GROK_MONTHLY_QUOTA, GROK_MONTHLY_TMP))


def quota_lines(now: int) -> list[str]:
    """The quota block under the title — one line per vendor whose data is present.

    claude ALWAYS renders: 5h first and bold (it's the limit that actually STOPS work),
    then 7d riding along on the same line. codex and grok each render a 7d line when
    their mirror exists (grok = SuperGrok weekly pool). An enterprise-Claude-only
    machine (no ~/.codex, no ~/.grok) shows just the claude line, no empty vendor slots.
    Presence of data, not a config flag, decides what shows. Vendor labels are padded
    to a common width so the bars line up vertically across lines; colors match
    MODEL_COLOR. Reads durable Projects/quota first, then /tmp."""
    ensure_codex_weekly_fresh(now)
    ensure_grok_weekly_fresh(now)
    pad = len("claude")  # widest vendor label -> window labels + bars align across lines
    # claude line: the 5h wall (bold) leads, then 7d. A missing mirror still shows the
    # labeled slot ('5h —') rather than vanishing, so the headline row never disappears.
    claude = (c(f"{'claude':<{pad}} ", "bold")
              + _quota_win("5h", read_rate_limit(), now, ("bold",))
              + "   " + _quota_win("7d", read_quota(CLAUDE_WEEKLY_QUOTA, CLAUDE_WEEKLY_TMP), now))
    out: list[str] = [claude]
    codex_rl = read_quota(CODEX_WEEKLY_QUOTA, CODEX_WEEKLY_TMP)
    if codex_rl is not None:
        out.append(c(f"{'codex':<{pad}} ", *MODEL_COLOR["codex"])
                   + _quota_win("7d", codex_rl, now))
    grok_rl = read_grok_quota()
    if grok_rl is not None:
        # SuperGrok weekly pool (same bar as grok.com Settings → Usage).
        out.append(c(f"{'grok':<{pad}} ", *MODEL_COLOR["grok"])
                   + _quota_win("7d", grok_rl, now))
    return out


def c(text: str, *styles: str) -> str:
    if not COLOR or not styles:
        return text
    codes = ";".join(_C[s] for s in styles if s in _C)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def char_width(ch: str) -> int:
    """Display columns for one char. Emoji (East-Asian 'W'/'F') take 2; nonspacing/
    enclosing marks and format chars (VS16, ZWJ) take 0; everything else 1. Keeps the
    glyph-prefixed state column aligned — len() miscounts a 2-cell emoji as 1. Detect
    zero-width by category (Mn/Me/Cf) not combining(): VS16 (U+FE0F) has combining
    class 0 but category Mn, so combining() misses it and a note with a redundant VS16
    would over-count by one."""
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(s: str) -> int:
    return sum(char_width(ch) for ch in _ANSI.sub("", s))


def vpad(s: str, width: int) -> str:
    return s + " " * max(0, width - vlen(s))


def _cell(text, width: int) -> str:
    """Left-justify to `width` DISPLAY columns, truncating with … by display width so an
    emoji-prefixed cell neither overflows nor misaligns the columns to its right."""
    s = str(text)
    if vlen(s) > width:
        out, w = [], 0
        for ch in s:
            cw = char_width(ch)
            if w + cw > width - 1:  # leave one column for the ellipsis
                break
            out.append(ch)
            w += cw
        s = "".join(out) + "…"
    return s + " " * max(0, width - vlen(s))


def wrap_display(text: str, width: int) -> list[str]:
    """Word-wrap plain text to `width` display columns. Preserves explicit newlines;
    hard-breaks tokens longer than `width`. Empty/None -> a single empty line so a
    caller can still put a placeholder on top."""
    width = max(1, int(width))
    raw = "" if text is None else str(text)
    if not raw:
        return [""]
    lines: list[str] = []
    for para in raw.splitlines() or [""]:
        if not para:
            lines.append("")
            continue
        # Prefer breaks on spaces; fall back to hard-breaking an overlong token.
        words = para.split(" ")
        cur = ""
        for w in words:
            cand = w if not cur else f"{cur} {w}"
            if vlen(cand) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if vlen(w) <= width:
                cur = w
                continue
            # Hard-break by display width (emoji-safe via char_width).
            chunk, w_disp = [], 0
            for ch in w:
                cw = char_width(ch)
                if chunk and w_disp + cw > width:
                    lines.append("".join(chunk))
                    chunk, w_disp = [], 0
                chunk.append(ch)
                w_disp += cw
            cur = "".join(chunk)
        if cur or not lines or lines[-1] != "":
            # Always emit the trailing piece; an empty cur after a hard-break of the
            # last token is already on `lines`.
            if cur:
                lines.append(cur)
    return lines or [""]


def note_panel_lines(snap: dict | None, width: int) -> list[str]:
    """Box panel with the focused row's full note (the table's note column truncates).
    `snap` None -> a one-line empty-selection panel so the key never fails silently."""
    if snap is None:
        return panel("note", [c("(no row selected)", "dim")], width)
    tkt = snap.get("ticket") or snap.get("session") or "?"
    sess = snap.get("session") or ""
    title = f"note · {tkt}" + (f" · {sess}" if sess and sess != tkt else "")
    note = snap.get("note")
    if note is None or str(note).strip() == "":
        body = [c("(no note)", "dim")]
    else:
        # Inner width matches panel() (width - 4 for "│ " + " │").
        body = [c(ln, "white") for ln in wrap_display(str(note), max(1, width - 4))]
    return panel(title, body, width, border=("cyan",))


def load_snapshots() -> list[dict]:
    snaps: list[dict] = []
    for path in glob.glob(str(STATE_DIR / "*.json")):
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue  # half-written / unreadable — skip, never fatal
        if isinstance(data, dict) and data.get("session"):
            snaps.append(data)
    return snaps


def tmux_lane_live() -> set[str]:
    """Session names on the private `-L <socket>` lane socket, one `list-sessions` per
    refresh (never per-row). dispatch names each lane <issue>; match to rows EXACTLY (see
    lane_live_match) — a prefix rule would cross-match sibling numbers."""
    try:
        out = subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return {x for x in out.stdout.split() if x}
    except (OSError, subprocess.SubprocessError):
        pass
    return set()


def lane_live_match(session: str, lane_sessions: set[str]) -> bool:
    """Exact match only. A row session (<issue> or <issue>-worker) is live iff some
    lane session name <issue> satisfies session == <issue> or session ==
    <issue>-worker. NOT a prefix test: a live 'PROJ-7' must never light up
    'PROJ-76-worker' (reachable under the parallel lanes dispatch creates)."""
    if not session:
        return False
    for t in lane_sessions:
        if session == t or session == f"{t}-worker":
            return True
    return False


_UUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")


def cmux_live() -> set[str]:
    """UUIDs of live cmux nodes (surfaces included); empty when cmux is absent.
    Snapshots carry their emitter's $CMUX_SURFACE_ID (see emit-status.sh); the set
    membership here is the anchored match for hands-on runs (which live in cmux, not on
    the lane socket) — no title/substring matching."""
    try:
        out = subprocess.run(["cmux", "tree", "--all", "--id-format", "uuids"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return {m.group(0).upper() for m in _UUID_RE.finditer(out.stdout)}
    except (OSError, subprocess.SubprocessError):
        pass
    return set()


def pane_state(s: dict, cmux: set[str], lanes: set[str], now: int) -> str:
    """Liveness for one row: live | ghost | stale | -. Precedence: a TERMINAL row is
    never live (no session synthesis for merged/done), a live match past the stale
    threshold is a ghost (not resurrected), a non-terminal row with no match is stale
    past the threshold, else '-'."""
    st = s.get("state", "?")
    if st in TERMINAL_STATES:
        return "-"
    age = now - int(s.get("epoch", 0))
    live_match = (str(s.get("cmux_surface", "")).upper() in cmux
                  or lane_live_match(s.get("session", ""), lanes))
    if live_match:
        return "live" if age <= STALE_SECS else "ghost"
    return "stale" if age > STALE_SECS else "-"


def reapable(s: dict, cmux: set[str], lanes: set[str], now: int) -> bool:
    """Whether a row is a dead card that needs no kill step: terminal (merged/done) or
    stale (💀 — no live pane past the threshold). Live/ghost rows are not "reapable" in
    this sense; `r` still removes them, but only after handler.sh ends the run first."""
    return s.get("state") in TERMINAL_STATES or pane_state(s, cmux, lanes, now) == "stale"


def reap_snapshot(session: str) -> str:
    """Remove one row's on-disk snapshot through the single writer (emit-status.sh
    --remove). Card-only; never kills a process — the live/ghost kill step is handler.sh."""
    if not session:
        return "no row selected"
    try:
        subprocess.run(["bash", EMIT, "--remove", "--session", session], timeout=8)
        return f"reaped {session}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"reap failed: {e}"


_prune_checked_at = 0.0


def auto_prune_terminal(snaps: list[dict], now: int | float) -> int:
    """Reap terminal (merged/done) snapshots older than REAP_TERMINAL_HOURS, so week-old
    tombstones don't accumulate. Returns the count reaped. Best-effort and same single
    writer as the `r` key (reap_snapshot -> emit-status --remove): only ever touches
    terminal rows, never a live/ghost/stale run. Throttled to once a minute, and capped
    per pass so a large first-run backlog reaps over a few passes instead of hitching one
    frame. Disabled (no-op) when REAP_TERMINAL_HOURS <= 0."""
    global _prune_checked_at
    if REAP_TERMINAL_HOURS <= 0:
        return 0
    now_f = float(now)
    if _prune_checked_at and now_f - _prune_checked_at < 60:
        return 0
    _prune_checked_at = now_f
    cutoff = REAP_TERMINAL_HOURS * 3600
    reaped = 0
    for s in snaps:
        if reaped >= 25:  # cap per pass; the rest reap on the next minute's check
            break
        if s.get("state") in TERMINAL_STATES and (now_f - int(s.get("epoch", 0))) > cutoff:
            reap_snapshot(s.get("session", ""))
            reaped += 1
    return reaped


def humanize_age(epoch: int, now: int) -> str:
    d = max(0, now - int(epoch or 0))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    return f"{d // 3600}h{(d % 3600) // 60}m"


def truncate_visible(s: str, width: int) -> str:
    """Clip a styled string to `width` DISPLAY columns, copying ANSI escapes (zero
    display width) verbatim and closing any open style. No-op when it already fits, so
    a wide terminal renders byte-identically; only a too-narrow frame is clipped here
    instead of spilling a row past the panel's right border (the column geometry can
    exceed a narrow terminal once the cursor gutter + glyph-widened state column are
    counted)."""
    if vlen(s) <= width:
        return s
    out: list[str] = []
    w, i, n = 0, 0, len(s)
    while i < n:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        cw = char_width(s[i])
        if w + cw > width:
            break
        out.append(s[i])
        w += cw
        i += 1
    return "".join(out) + ("\033[0m" if COLOR else "")


def panel(title: str, body: list[str], width: int, border: tuple[str, ...] = ()) -> list[str]:
    """Box-drawing panel. body lines are clipped-then-padded to the inner width, so no
    row ever overruns the border regardless of terminal size."""
    inner = width - 4  # "│ " + inner + " │"
    seg = f"─ {title} " if title else "──"
    top = c("┌" + seg + "─" * max(0, width - 2 - vlen(seg)) + "┐", *border)
    bot = c("└" + "─" * (width - 2) + "┘", *border)
    out = [top]
    for ln in body:
        out.append(c("│", *border) + " " + vpad(truncate_visible(ln, inner), inner) + " " + c("│", *border))
    out.append(bot)
    return out


def sys_line() -> str:
    try:
        one, five, _ = os.getloadavg()
        return f"load {one:.2f} {five:.2f}"
    except (OSError, AttributeError):
        return ""


def _read_int(path: Path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _pilot_config() -> tuple[bool, int]:
    """(configured, max_windows) parsed from pilot-light's config.sh. Best-effort:
    a missing/odd config reads as unconfigured, max 0 (unlimited)."""
    configured, mx = False, 0
    try:
        txt = (PILOT_DIR / "config.sh").read_text()
    except OSError:
        return configured, mx
    m = re.search(r'PILOT_LIGHT_CONFIGURED="\$\{PILOT_LIGHT_CONFIGURED:-(\d+)\}"', txt)
    configured = bool(m and m.group(1) == "1")
    m = re.search(r'MAX_WINDOWS="\$\{MAX_WINDOWS:-(\d+)\}"', txt)
    if m:
        mx = int(m.group(1))
    return configured, mx


def _pilot_last_verdict() -> str:
    """Most recent outcome line from runner.log, condensed. '' if none yet."""
    try:
        lines = (PILOT_DIR / "logs" / "runner.log").read_text().splitlines()
    except OSError:
        return ""
    for ln in reversed(lines):
        low = ln.lower()
        if "clean; cooldown" in low:
            return "clean"
        if "quota hit" in low:
            return "quota"
        if "error (rc=" in low:
            return "error"
    return ""


def _pilot_last_reason() -> str:
    """Why the last failing run failed, as wrapper.sh recorded it (state/last.reason).
    'error' alone is useless when you're staring at the panel wondering what broke, and
    a failing `claude --print` writes nothing to stderr — the reason is in its JSON
    envelope. Cleared by wrapper.sh on a clean run, so a stale failure never lingers."""
    try:
        return " ".join((PILOT_DIR / "state" / "last.reason").read_text().split())
    except OSError:
        return ""


def read_pilot() -> dict:
    """Snapshot of the pilot-light sidecar's state (all files it writes). Pure
    reads — the dashboard never mutates pilot state, same contract as the runs."""
    st = PILOT_DIR / "state"
    configured, mx = _pilot_config()
    return {
        "configured": configured,
        "max_windows": mx,
        "run_count": _read_int(st / "run_count") or 0,
        "next_fire_at": _read_int(st / "next_fire_at"),
        "disarmed": (st / "disarmed").exists(),
        "running": (st / "run.lock").is_dir(),
        "dryrun_ok": (st / ".dryrun-ok").exists(),
        "verdict": _pilot_last_verdict(),
        "reason": _pilot_last_reason(),
    }


def pilot_panel(width: int, now: int) -> list[str]:
    p = read_pilot()
    if not p["configured"]:
        return panel("pilot", [c("not configured — set PILOT_LIGHT_CONFIGURED=1 in config.sh", "yellow")],
                     width, border=("yellow",))
    mx = p["max_windows"]
    count = f"launch {p['run_count']}" + (f"/{mx}" if mx else "")
    if p["disarmed"]:
        status, border = c("disarmed", "bold", "red") + c(" (ceiling/deadline — rm state/disarmed to re-arm)", "dim"), ("red",)
    elif p["running"]:
        status, border = c("running now", "bold", "green"), ("green",)
    elif p["next_fire_at"] and now < p["next_fire_at"]:
        mins = (p["next_fire_at"] - now) // 60
        status, border = c(f"armed · gated {mins}m", "cyan"), ("cyan",)
    else:
        status, border = c("armed · window open", "bold", "cyan"), ("cyan",)
    line1 = f"{status}   ·   {count}"
    nf = (datetime.fromtimestamp(p["next_fire_at"]).strftime("%a %H:%M")
          if p["next_fire_at"] else "-")
    verdict = p["verdict"] or "—"
    vcol = {"clean": ("green",), "quota": ("yellow",), "error": ("red",)}.get(verdict, ("dim",))
    line2 = c("next fire ", "dim") + nf + c("   ·   last ", "dim") + c(verdict, *vcol)
    body = [line1, line2]
    # The failure reason itself, not just the word "error" — truncated to the panel width.
    if p["reason"] and verdict == "error":
        body.append(c("why ", "dim") + c(_cell(p["reason"], width - 10).rstrip(), "red"))
    return panel("pilot", body, width, border=border)


# Column geometry. GUTTER is the 2-col cursor marker ("▸ " / "  "); W_ST is wide enough
# for the longest glyph-prefixed state ("🎻 implementing" = 2+1+12 = 15 display cols).
# W_CTX fits an 8-cell fill bar + " " + "100%"; W_COST fits "$1,234.56".
GUTTER = 2
W_RUN, W_ROLE, W_TKT, W_ST, W_MODEL, W_CTX, W_COST, W_AGE, W_PANE = 24, 4, 9, 15, 7, 13, 9, 7, 5
_FIXED = W_RUN + W_ROLE + W_TKT + W_ST + W_MODEL + W_CTX + W_COST + W_AGE + W_PANE
_SEPS = 9  # single spaces between the 10 columns


def order_rows(snaps: list[dict], now: int, max_rows: int = MAX_ROWS) -> list[tuple[dict, bool]]:
    """The rows in the exact order they render (and the cursor walks): escalated, then
    waiting-at-gate, then active — each oldest-first — then terminal (dimmed, newest
    first). Terminal rows persist until reaped with `r`; capped to max_rows. Returns
    (snap, dim)."""
    escalated = sorted([s for s in snaps if s.get("state") == "escalated"],
                       key=lambda s: int(s.get("epoch", 0)))
    waiting = sorted([s for s in snaps if s.get("state") == "waiting"],
                     key=lambda s: int(s.get("epoch", 0)))
    active = sorted([s for s in snaps
                     if s.get("state") not in TERMINAL_STATES
                     and s.get("state") not in ("escalated", "waiting")],
                    key=lambda s: int(s.get("epoch", 0)))
    terminal = sorted([s for s in snaps if s.get("state") in TERMINAL_STATES],
                      key=lambda s: int(s.get("epoch", 0)), reverse=True)
    rendered = ([(s, False) for s in escalated + waiting + active]
                + [(s, True) for s in terminal])
    return rendered[:max_rows]


def render_frame(snaps: list[dict], cmux: set[str], lanes: set[str],
                 costs: dict[str, float], ctxs: dict[str, int], now: int,
                 term_width: int, sel_session: str | None = None,
                 status_msg: str = "", term_height: int | None = None,
                 scroll: list[int] | None = None,
                 show_note: bool = False) -> str:
    """scroll, if given, is a 1-element list used as an in/out parameter: read as this
    frame's starting scroll offset, written back with the offset actually used (clamped
    to keep sel_session in view) — the caller carries the same list into the next frame,
    the same way it already threads sel_session across loop iterations.

    show_note: when True, render a full-note panel for the focused row (the table's note
    column truncates; `n` toggles this)."""
    width = max(64, min(term_width, MAX_WIDTH))
    inner = width - 4
    w_note = max(6, inner - GUTTER - (_FIXED + _SEPS))
    bar_cells = CTX_BAR_CELLS

    # Full, uncapped order — used for the title-bar counts and the escalations panel
    # (never truncated — that's the "need you" list), and as the scrollable window source.
    all_rows = order_rows(snaps, now, max_rows=len(snaps))
    escal = [s for s, d in all_rows if s.get("state") == "escalated"]
    quota_block = quota_lines(now)
    pilot_lines = pilot_panel(width, now) if PILOT_DIR is not None else None
    note_snap = next((s for s, _ in all_rows if s.get("session") == sel_session), None) \
        if show_note else None
    # Cap the expanded note so a novel-length field can't push the soloists off-screen.
    # wrap first at panel width, then hard-cap body lines (panel chrome is 2).
    if show_note:
        raw_note = note_panel_lines(note_snap, width)
        # Keep borders + at most ~12 body lines (title chrome is already in the panel).
        max_note_body = 12
        if len(raw_note) > max_note_body + 2:
            note_lines = raw_note[: max_note_body + 1] + [raw_note[-1]]  # keep bottom border
            # Flag truncation on the last body line before the border.
            if len(note_lines) >= 2:
                note_lines[-2] = c("… (truncated — note too long for the pane)", "dim")
        else:
            note_lines = raw_note
    else:
        note_lines = None

    if term_height is not None:
        # Everything in the frame *besides* the soloists panel's data rows: title, optional
        # weekly/status lines, the panel's own chrome (blank + borders + header row), the
        # pilot panel, the note detail panel, the escalations panel, and the trailing blank
        # + footer. Getting this wrong just under- or over-fills the row budget by a line
        # or two — not fatal — but skipping it entirely is what let the frame grow taller
        # than the pane and scroll the title/header off the top before the user ever saw them.
        fixed = 1 + len(quota_block) + (1 if status_msg else 0)
        fixed += 1 + 2 + 1  # blank + panel borders + header row
        if note_lines is not None:
            fixed += 1 + len(note_lines)
        if pilot_lines is not None:
            fixed += 1 + len(pilot_lines)
        if escal:
            fixed += 1 + 2 + len(escal)
        fixed += 1 + 1  # blank + footer
        # Reserve 2 rows for the "N more above/below" scroll indicators — worst case both
        # show at once. Unused reserve just leaves a little slack, which is harmless.
        cap = max(1, min(MAX_ROWS, term_height - fixed - 2))
    else:
        cap = MAX_ROWS

    max_offset = max(0, len(all_rows) - cap)
    scroll_offset = scroll[0] if scroll is not None else 0
    sel_idx = next((i for i, (s, _) in enumerate(all_rows) if s.get("session") == sel_session), None)
    if sel_idx is not None:
        if sel_idx < scroll_offset:
            scroll_offset = sel_idx
        elif sel_idx >= scroll_offset + cap:
            scroll_offset = sel_idx - cap + 1
    scroll_offset = max(0, min(scroll_offset, max_offset))
    if scroll is not None:
        scroll[0] = scroll_offset

    def rowcells(run, role, tkt, st, model, ctx, cost, age, pane, note):
        return " ".join([_cell(run, W_RUN), _cell(role, W_ROLE), _cell(tkt, W_TKT),
                         _cell(st, W_ST), _cell(model, W_MODEL), _cell(ctx, W_CTX),
                         _cell(cost, W_COST), _cell(age, W_AGE), _cell(pane, W_PANE),
                         _cell(note, w_note)])

    header = "  " + rowcells("run", "role", "issue", "state", "model", "ctx",
                             "cost", "age", "pane", "note")
    body = [c(header, "dim")]

    def add(s: dict, dim: bool, selected: bool):
        st = s.get("state", "?")
        epoch = int(s.get("epoch", 0))
        stale = st not in TERMINAL_STATES and (now - epoch) > STALE_SECS
        pane = pane_state(s, cmux, lanes, now)
        # 💀 flags a stalled row, but never masks the 🚨 alarm on an escalated one.
        glyph = STATE_GLYPH.get("stale" if (stale and pane == "stale" and st != "escalated") else st, "")
        st_disp = f"{glyph} {st}" if glyph else st
        mdisp, mcolor = model_cell(s.get("model", ""))
        xdisp, xcolor = ctx_cell(gauge_for(s, ctxs), bar_cells)
        cdisp, ccolor = cost_cell(gauge_for(s, costs))
        marker = "▸ " if selected else "  "
        if selected:
            # A clean reverse-video bar (+ ▸). Plain cells so the row's inner ANSI resets
            # don't punch holes in the reverse run; the ▸ carries selection under NO_COLOR.
            line = marker + rowcells(
                s.get("session", "?"), ROLE_GLYPH.get(s.get("role", "other"), "-"),
                s.get("ticket", "-"), st_disp, mdisp, xdisp, cdisp,
                humanize_age(epoch, now), pane, s.get("note", ""))
            body.append(f"\033[7m{line}\033[0m" if COLOR else line)
            return
        base = ("dim",) if dim else ()
        line = marker + " ".join([
            c(_cell(s.get("session", "?"), W_RUN), *base),
            c(_cell(ROLE_GLYPH.get(s.get("role", "other"), "-"), W_ROLE), *base),
            c(_cell(s.get("ticket", "-"), W_TKT), *base),
            c(_cell(st_disp, W_ST), *STATE_COLOR.get(st, ("white",))),
            c(_cell(mdisp, W_MODEL), *(("dim",) if dim else mcolor)),
            c(_cell(xdisp, W_CTX), *(("dim",) if dim else xcolor)),
            c(_cell(cdisp, W_COST), *(("dim",) if dim else ccolor)),
            c(_cell(humanize_age(epoch, now), W_AGE), *(("red",) if stale else base)),
            c(_cell(pane, W_PANE), *PANE_COLOR.get(pane, ("dim",))),
            c(_cell(s.get("note", ""), w_note), *base),
        ])
        body.append(line)

    rendered = all_rows[scroll_offset:scroll_offset + cap]
    above = scroll_offset
    below = max(0, len(all_rows) - scroll_offset - len(rendered))
    if not rendered:
        body.append("  " + c("no active runs — start a command or a dispatch lane", "dim"))
    else:
        if above:
            body.append("  " + c(f"↑ {above} more above — j/k to scroll", "dim"))
        for s, dim in rendered:
            add(s, dim=dim, selected=(s.get("session") == sel_session))
        if below:
            height_bound = term_height is not None and cap < MAX_ROWS
            reason = "j/k to scroll" if height_bound else "j/k to scroll, or raise AGENT_DASHBOARD_MAX_ROWS"
            body.append("  " + c(f"↓ {below} more below — {reason}", "dim"))

    # Counts/spend reflect ALL runs, not just the ones that fit on screen this frame.
    active_n = sum(1 for s, d in all_rows if not d and s.get("state") not in ("escalated", "waiting"))
    waiting_n = sum(1 for s, d in all_rows if s.get("state") == "waiting")
    done_n = sum(1 for s, d in all_rows if d)
    spend = sum(gauge_for(s, costs) or 0.0 for s, d in all_rows)
    title = ("🎼 orchestra  " + datetime.now().strftime("%H:%M:%S")
             + f"   active {active_n}"
             + (f"   waiting {waiting_n}" if waiting_n else "")
             + (f"   🚨 escalated {len(escal)}" if escal else "   escalated 0")
             + f"   done {done_n}"
             + (f"   spend ${spend:,.2f}" if spend else ""))
    lines: list[str] = [c(title, "bold")]
    lines.extend(quota_block)
    if status_msg:
        lines.append(c("• " + status_msg, "cyan"))
    lines.append("")
    lines += panel("soloists", body, width)

    if note_lines is not None:
        lines.append("")
        lines += note_lines

    if pilot_lines is not None:
        lines.append("")
        lines += pilot_lines

    if escal:
        ebody = []
        for s in escal:
            tkt = s.get("ticket", s.get("session", "?"))
            ebody.append(c(_cell(tkt, W_TKT + 2), "bold", "red") + " " + c(s.get("note", "(no note)"), "red"))
        lines.append("")
        lines += panel("🚨 escalations - need you", ebody, width, border=("red",))

    lines.append("")
    sysl = sys_line()
    keys = "j/k ↑↓ move · ⏎ open · p PR · t issue · n note · r end+reap · q quit"
    footer = ((f"{sysl}   " if sysl else "") + keys
              + f"   state:{STATE_DIR}   quota:{QUOTA_DIR}   refresh:{REFRESH_SECS:g}s")
    lines.append(c(footer, "dim"))
    # No trailing "\n" after the last line: a \n after the Nth line on an N-row terminal
    # forces an implicit scroll (cursor lands on the nonexistent row N+1), which would
    # push row 1 off-screen even when the frame is sized to fit exactly.
    return "\033[H" + "\n".join(ln + "\033[K" for ln in lines) + "\033[J"


def _pilot_pidfile() -> "Path | None":
    return (PILOT_DIR / "state" / "loop.pid") if PILOT_DIR is not None else None


def _dashboard_registry_dir() -> "Path | None":
    """Directory of currently-open dashboards, one empty file per instance named by
    its own pid (e.g. dashboards/42871). Reference-counts consumers of the pilot so
    it lives iff at least one dashboard is open: the first dashboard to open spawns it
    (if not already running), the last to close stops it — any open/close order.

    A directory-of-files (vs a single locked set file) is deliberate: each dashboard
    only ever creates/removes ITS OWN file, so add/remove never races and needs no
    lock; membership is just the live pids among the filenames. Scoped under PILOT_DIR
    so two different projects' pilots (each its own PILOT_LIGHT_DIR) never cross-count.
    Mirrors _pilot_already_running's os.kill(pid, 0) liveness idiom rather than
    inventing a second one."""
    return (PILOT_DIR / "state" / "dashboards") if PILOT_DIR is not None else None


def _prune_registry() -> int:
    """Remove registry entries whose pid is dead (a dashboard force-killed without
    running stop_pilot leaves its file behind — this is where that gets reaped) and
    return the count of live dashboards remaining. Best-effort: an unreadable entry is
    left in place rather than guessed-dead, so we never under-count live consumers and
    kill a pilot someone's still using."""
    reg = _dashboard_registry_dir()
    if reg is None or not reg.exists():
        return 0
    live = 0
    for entry in reg.iterdir():
        try:
            pid = int(entry.name)
        except ValueError:
            continue  # not a pid file — leave foreign contents untouched
        try:
            os.kill(pid, 0)  # liveness only
            live += 1
        except ProcessLookupError:
            try:
                entry.unlink()  # dead: reap the stale entry
            except OSError:
                pass
        except PermissionError:
            live += 1  # alive, owned elsewhere — still a live consumer
        except OSError:
            live += 1  # can't tell — assume live rather than kill someone's pilot
    return live


def _register_dashboard(pid: int) -> None:
    """Record this dashboard as an open consumer of the pilot. Idempotent; best-effort
    (a failure just means this instance won't hold the pilot open — the same
    conservative direction as a missing pidfile)."""
    reg = _dashboard_registry_dir()
    if reg is None:
        return
    try:
        reg.mkdir(parents=True, exist_ok=True)
        (reg / str(pid)).touch()
    except OSError:
        pass


def _unregister_dashboard(pid: int) -> None:
    """Drop this dashboard's own registry entry. Best-effort."""
    reg = _dashboard_registry_dir()
    if reg is None:
        return
    try:
        (reg / str(pid)).unlink(missing_ok=True)
    except OSError:
        pass


def _terminate_pilot_by_pid(pid: int, timeout: float = 5.0) -> None:
    """SIGTERM a pilot we don't hold a Popen for (the last dashboard to close may be
    one that only adopted the pilot, never spawned it) and best-effort wait for it to
    actually exit by polling os.kill(pid, 0). loop.sh traps TERM and finishes any
    in-flight wrapper run before quitting, so it may outlive the timeout — that's fine,
    we don't block dashboard exit indefinitely, same 5s bound as the owned-Popen path."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return  # already gone, or not ours to signal — nothing to wait on
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # exited
        except OSError:
            return  # can't probe — stop waiting
        time.sleep(0.05)


def _pilot_already_running() -> int | None:
    """PID of a live loop.sh already owning this PILOT_DIR, if any. Guards against the
    leak this once caused: start_new_session=True deliberately detaches the pilot from
    the dashboard so it survives a clean exit, but that also means every dashboard
    launch (new tab, new cmux workspace) spawned ANOTHER one with nothing to stop the
    old ones — four accumulated in practice before this check existed. Verifies the pid
    is both alive and still actually loop.sh (not a reused pid) before trusting it;
    removes a stale/bogus pidfile so it doesn't wedge future launches."""
    pidfile = _pilot_pidfile()
    if pidfile is None or not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # liveness only — no signal actually sent
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)  # stale: the pid is gone
        return None
    except PermissionError:
        pass  # alive, just owned elsewhere — treat as running
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=2)
        if out.returncode != 0 or "loop.sh" not in out.stdout:
            pidfile.unlink(missing_ok=True)  # pid recycled onto an unrelated process
            return None
    except (OSError, subprocess.SubprocessError):
        pass  # can't confirm either way — trust the pidfile rather than double-spawn
    return pid


def start_pilot() -> "subprocess.Popen | None":
    """Co-launch pilot-light's sidecar loop, if configured. Returns the process
    (to stop on exit) or None — either because nothing is configured, or because a
    loop.sh is already running (see _pilot_already_running): the panel still renders
    from PILOT_DIR/state either way, and — with the dashboard registry below — this
    instance is now counted as a consumer keeping that pilot alive even though it
    didn't spawn it. Never fatal: a pilot that won't start just means the dashboard
    renders its panel as idle, same as any other read failure.

    Registration is independent of spawning: we mark "this dashboard is now using the
    pilot" whether or not this instance is the one that starts the process, so the
    last dashboard to close (not just the one that spawned) knows to stop it."""
    if PILOT_DIR is None:
        return None
    loop = PILOT_DIR / "loop.sh"
    if not loop.exists():
        return None
    # Prune dead siblings, then count THIS dashboard as an open consumer before
    # deciding whether to spawn — so refcounting is correct even when we adopt an
    # already-running pilot rather than starting one.
    _prune_registry()
    _register_dashboard(os.getpid())
    if _pilot_already_running() is not None:
        return None
    try:
        # Own process group so our Ctrl-C doesn't SIGINT it mid-window — we stop
        # it explicitly (SIGTERM) so an in-flight run is allowed to finish first.
        proc = subprocess.Popen(["bash", str(loop)], cwd=str(PILOT_DIR),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return None
    pidfile = _pilot_pidfile()
    if pidfile is not None:
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(proc.pid))
        except OSError:
            pass  # best-effort — a missing pidfile just re-opens the race, not fatal
    return proc


def stop_pilot(proc: "subprocess.Popen | None") -> None:
    """Unregister this dashboard and, ONLY if it was the last one open, stop the pilot.
    loop.sh traps TERM and finishes any in-flight run before exiting, so the quota
    window isn't wasted.

    The gate moved from "did *I* start it" (the old `proc is None` no-op) to "is anyone
    else still using it": the last dashboard to close might be one that merely adopted
    the pilot and holds no Popen, so we terminate by the pid in loop.pid, not via proc.
    We only ever terminate a pilot whose loop.pid we can confirm is a live loop.sh —
    and since only start_pilot() ever writes loop.pid (launchd runs wrapper.sh directly,
    a hand-started `loop.sh` writes no pidfile), a durable launchd/manual pilot is never
    matched here and never killed."""
    if PILOT_DIR is None:
        return
    _unregister_dashboard(os.getpid())
    still_open = _prune_registry()  # also reaps any siblings that crashed without cleanup
    if still_open > 0:
        # Others still using the pilot — leave it running. If we own the Popen it's
        # detached (start_new_session) and survives our exit, exactly as intended.
        return

    # We were the last dashboard open: stop the pilot it/we left running.
    pid = _pilot_already_running()  # live loop.sh owning this PILOT_DIR, else None
    if pid is not None:
        if proc is not None and proc.poll() is None and proc.pid == pid:
            try:
                proc.terminate()      # fast path: we hold the Popen for this exact pid
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            _terminate_pilot_by_pid(pid)  # adopted pilot: signal by bare pid
    pidfile = _pilot_pidfile()
    if pidfile is not None:
        try:
            # Only clear the pidfile if it still names the pilot we just stopped, so a
            # racing start_pilot that spawned a fresh one keeps its own record.
            if pid is not None and int(pidfile.read_text().strip()) == pid:
                pidfile.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def move_sel(order: list[str], current: str | None, delta: int) -> str | None:
    """Session id `delta` steps from `current` in display order `order`. Clamps at the
    ends; if `current` has disappeared, snaps to the top; empty order -> None."""
    if not order:
        return None
    if current not in order:
        return order[0]
    i = order.index(current)
    return order[max(0, min(len(order) - 1, i + delta))]


def dispatch_action(key: str, snap: dict | None, cmux: set[str], lanes: set[str], now: int) -> str:
    """Shell out to handler.sh for the focused row (the dashboard mutates nothing).
    Returns the handler's last stdout line as a transient status message."""
    if snap is None:
        return "no row selected"
    pane = pane_state(snap, cmux, lanes, now)
    # Coerce every field to str: a snapshot is best-effort JSON that may carry a
    # non-string (e.g. a hand-edited `"pr_number": 51`), and subprocess.run's argv must
    # be all strings — an int there would raise TypeError and crash the loop, breaking
    # this module's "a malformed snapshot is skipped, never fatal" contract.
    def _s(v) -> str:
        return v if isinstance(v, str) else ("" if v is None else str(v))
    argv = ["bash", HANDLER, "runs", key, _s(snap.get("ticket")), _s(snap.get("state")),
            pane, _s(snap.get("pr_number")), _s(snap.get("worktree_path")),
            _s(snap.get("cmux_surface"))]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=8)
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        return out.splitlines()[-1] if out else f"{key} → {_s(snap.get('ticket'))}"
    except FileNotFoundError:
        return "handler.sh not found"
    except subprocess.TimeoutExpired:
        return "handler timed out"
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as e:
        return f"handler error: {e}"


def new_escalations(snaps: list[dict], seen: "set[str] | None") -> "tuple[list[dict], set[str]]":
    """(rows that just went escalated, the current escalated-session set). `seen` is the
    prior frame's set, threaded across frames like sel_session; None on the very first
    frame means "don't alert on startup" — a board opened while an escalation already
    stands shouldn't ring (you're looking now). Returns [] for `new` on that first frame."""
    current = {s.get("session") for s in snaps if s.get("state") == "escalated" and s.get("session")}
    if seen is None:
        return [], current
    fresh = current - seen
    rows = [s for s in snaps if s.get("session") in fresh]
    return rows, current


def alert_escalation(rows: list[dict]) -> None:
    """Ring the terminal bell (unless muted) and optionally post a macOS notification for
    newly-escalated rows. Best-effort — an alert must never crash or block the loop."""
    if not rows:
        return
    if ESCALATION_BELL:
        try:
            sys.stdout.write("\a")
        except (OSError, ValueError):
            pass
    if ESCALATION_NOTIFY and sys.platform == "darwin":
        # osascript is passed as argv (no shell), and the strings are scrubbed to a safe
        # charset so a hand-edited note/session can't break out of the quoted literal.
        def _safe(v: str) -> str:
            return re.sub(r"[^A-Za-z0-9 ._#/-]", "", str(v))[:80]
        who = ", ".join(_safe(s.get("ticket") or s.get("session") or "?") for s in rows[:4])
        body = f"{len(rows)} run(s) need you: {who}"
        try:
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{_safe(body)}" with title "🚨 orchestra escalation"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


_EOF = object()  # _read_key sentinel: stdin closed, distinct from a plain timeout (None)


def _read_key(timeout: float):
    """One keypress within `timeout` seconds; None on timeout; the _EOF sentinel when
    stdin is closed. Reads the raw fd with os.read (not buffered sys.stdin.read, which
    can hold bytes select never sees), and resolves an arrow escape (\\x1b[A/B/C/D) with
    a short 0.01s tail read so a bare ESC doesn't swallow the next byte. EOF is returned
    distinctly (not None): on a closed pane select() reports the fd readable every call
    with no delay, so treating EOF as a timeout would busy-spin the refresh loop at full
    CPU."""
    fd = sys.stdin.fileno()
    try:
        r, _, _ = select.select([fd], [], [], timeout)
    except (OSError, ValueError):
        return _EOF
    if not r:
        return None
    try:
        b = os.read(fd, 1)
    except OSError:
        return None
    if not b:  # EOF / closed pane
        return _EOF
    ch = b.decode("utf-8", "replace")
    if ch == "\x1b":
        r2, _, _ = select.select([fd], [], [], 0.01)
        if r2:
            try:
                return "\x1b" + os.read(fd, 2).decode("utf-8", "replace")
            except OSError:
                return "\x1b"
        return "\x1b"
    return ch


def _oneshot() -> int:
    """Non-tty (piped / contract test): render a single frame and exit."""
    now = int(time.time())
    size = shutil.get_terminal_size((120, 40))
    snaps = load_snapshots()
    costs = read_costs()
    costs.update(read_accurate_costs(snaps, now))
    ctxs = read_ctx()
    ctxs.update(read_accurate_ctx(snaps, now))  # live transcript ctx for headless lanes
    sys.stdout.write(render_frame(snaps, cmux_live(), tmux_lane_live(),
                                  costs, ctxs, now, size.columns,
                                  term_height=size.lines))
    return 0


def main() -> int:
    if not sys.stdout.isatty():
        return _oneshot()

    interactive = sys.stdin.isatty()  # guard raw-mode setup on STDIN, not stdout
    if not STATE_DIR.exists():
        sys.stderr.write(f"state dir {STATE_DIR} does not exist yet — waiting for the first snapshot…\n")

    def _restore(*_a):
        # Route SIGTERM/SIGHUP through the same teardown as the finally — the bare
        # finally does NOT run on a signal kill, and raw mode left on strands the shell
        # in no-echo (needs `stty sane`).
        raise SystemExit(0)

    # Install signal handlers BEFORE engaging raw mode, and engage raw mode INSIDE the
    # try below — so no exit path between "raw mode on" and "raw mode restored" can skip
    # the finally. A Ctrl-C or SIGTERM landing in the setup window would otherwise leave
    # the tty in cbreak (no-echo).
    prev_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            prev_handlers[sig] = signal.signal(sig, _restore)
        except (ValueError, OSError):
            pass

    pilot = start_pilot()
    old_termios = None
    sel_session: str | None = None
    status_msg = ""
    show_note = False  # `n` toggles a full-note panel for the focused row
    scroll = [0]  # persists across frames, same as sel_session — render_frame follows the cursor
    seen_escalated: set[str] | None = None  # None on frame 1 -> no ring for a standing escalation
    try:
        if interactive:
            try:
                old_termios = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())  # cbreak keeps ISIG so Ctrl-C still fires
            except (termios.error, OSError):
                old_termios = None
                interactive = False
        sys.stdout.write("\033[?1049h\033[?25l")  # alt screen + hide cursor
        while True:
            now = int(time.time())
            size = shutil.get_terminal_size((120, 40))
            snaps = load_snapshots()
            auto_prune_terminal(snaps, now)  # self-clean week-old tombstones (throttled inside)
            new_esc, seen_escalated = new_escalations(snaps, seen_escalated)
            alert_escalation(new_esc)  # ring/notify on a NEW escalation only
            cmux, lanes = cmux_live(), tmux_lane_live()
            costs, ctxs = read_costs(), read_ctx()
            costs.update(read_accurate_costs(snaps, now))  # accurate overrides mirror where resolvable
            ctxs.update(read_accurate_ctx(snaps, now))     # live transcript ctx for headless lanes
            # Uncapped: MAX_ROWS/the height budget only limit what's visible per frame — the
            # cursor (and scrolling) can still reach every session, not just the first screenful.
            order = [s.get("session") for s, _ in order_rows(snaps, now, max_rows=len(snaps))]
            if sel_session not in order:
                sel_session = order[0] if order else None
            sys.stdout.write(render_frame(snaps, cmux, lanes, costs, ctxs, now, size.columns,
                                          sel_session, status_msg, term_height=size.lines,
                                          scroll=scroll, show_note=show_note))
            sys.stdout.flush()

            if not interactive:
                time.sleep(REFRESH_SECS)
                continue

            key = _read_key(REFRESH_SECS)
            if key is _EOF:  # stdin closed — exit cleanly instead of busy-spinning
                break
            if key is None:
                continue
            status_msg = ""  # a keypress clears the previous transient message
            if key in ("q", "\x03"):  # q / Ctrl-C
                break
            elif key in ("k", "\x1b[A"):
                sel_session = move_sel(order, sel_session, -1)
                # Note panel (if open) retargets to the newly focused row next frame.
            elif key in ("j", "\x1b[B"):
                sel_session = move_sel(order, sel_session, +1)
            elif key == "n":
                # Toggle the full-note panel for the focused row. Local only — the note is
                # already on the snapshot; no handler/cmux side effect.
                show_note = not show_note
                if show_note:
                    by_id = {s.get("session"): s for s, _ in order_rows(snaps, now, max_rows=len(snaps))}
                    snap = by_id.get(sel_session)
                    if snap is None:
                        status_msg = "no row selected"
                    elif not str(snap.get("note") or "").strip():
                        status_msg = f"no note for {snap.get('ticket') or snap.get('session') or 'this row'}"
                    else:
                        status_msg = "note open · n again to close"
                else:
                    status_msg = "note closed"
            elif key == "\x1b" and show_note:
                # Bare ESC closes an open note panel (doesn't steal ESC from other uses).
                show_note = False
                status_msg = "note closed"
            elif key in ("\r", "\n", "p", "t"):
                by_id = {s.get("session"): s for s, _ in order_rows(snaps, now, max_rows=len(snaps))}
                snap = by_id.get(sel_session)
                act = "enter" if key in ("\r", "\n") else key
                status_msg = dispatch_action(act, snap, cmux, lanes, now)
            elif key == "r":
                # End+reap: for a live/ghost row, open/focus the pane and kill it (handler),
                # then always remove the card. Dead cards (merged/done/stale) just drop the
                # snapshot — never close their recorded cmux_surface (often a shared stale
                # UUID from an earlier process). Non-terminal "-" rows (no pane match yet)
                # also just clear the card so stuck board noise can be wiped in one key.
                by_id = {s.get("session"): s for s, _ in order_rows(snaps, now, max_rows=len(snaps))}
                snap = by_id.get(sel_session)
                if snap is None:
                    status_msg = "no row selected"
                else:
                    parts: list[str] = []
                    pane = pane_state(snap, cmux, lanes, now)
                    if pane in ("live", "ghost"):
                        # If another live/ghost row still shares this cmux_surface, blank it
                        # out for the handler so we don't close a tab another seat still owns.
                        # (Tombstones already skip the end path; this covers the rare dual-live case.)
                        end_snap = snap
                        surf = str(snap.get("cmux_surface") or "").strip().upper()
                        if surf:
                            shared = any(
                                s.get("session") != snap.get("session")
                                and str(s.get("cmux_surface") or "").strip().upper() == surf
                                and pane_state(s, cmux, lanes, now) in ("live", "ghost")
                                for s in snaps
                            )
                            if shared:
                                end_snap = dict(snap)
                                end_snap["cmux_surface"] = ""
                                parts.append("shared surface: skipped close")
                        parts.append(dispatch_action("r", end_snap, cmux, lanes, now))
                    parts.append(reap_snapshot(snap.get("session", "")))
                    status_msg = " · ".join(p for p in parts if p)
                    # Move to the row above (computed against the still-current `order`, before
                    # the reaped row vanishes). Reaping the topmost row has no "above" to move
                    # to — move_sel clamps in place, so the now-gone id falls through the
                    # `sel_session not in order` check next frame and lands on the new top.
                    sel_session = move_sel(order, sel_session, -1)
            else:
                printable = key if key.isprintable() else repr(key)
                status_msg = f"no action for '{printable}'"
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        stop_pilot(pilot)
        if old_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
            except (termios.error, OSError):
                pass
        for sig, h in prev_handlers.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass
        sys.stdout.write("\033[?25h\033[?1049l")  # restore
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
