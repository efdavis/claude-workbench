#!/usr/bin/env python3
"""Agent orchestra dashboard — a live view of, and index into, background agent lanes.

Watches the status snapshots written by emit-status.sh (see status.schema.json) and
renders a live cross-section of what every run is doing: which issue, which role, what
state, how long, and which ones need you (escalations). Music-themed: the dashboard is
the Orchestra, each run is a soloist, the dispatch lane spawner is the cue.

It is a viewer that ROUTES, not a controller: a row cursor (j/k or arrows) plus three
"open something" keys (Enter / p / t) that shell out to handler.sh (which owns every
side effect — cmux tabs, tmux attach). dashboard.py mutates no live run; its only local
write is the `r` key removing a finished/dead row's snapshot. A malformed or half-written
snapshot is skipped, never fatal.

Point it at the same state dir the emitter uses (AGENT_DASHBOARD_STATE_DIR, default
~/Projects/claude-workbench/agent-dashboard/state) and leave it open in a pane while
dispatch spawns lanes on the private `tmux -L <socket>` socket
(AGENT_DASHBOARD_TMUX_SOCKET). Account-wide quota mirrors (claude/codex/grok 5h+7d)
live under the sibling quota/ dir and are dual-written to /tmp for back-compat. Any
vendor whose data is absent (no ~/.codex, no grok mirror) simply doesn't render — so
this is vendor-neutral out of the box on an enterprise-Claude-only machine.

Pure stdlib on purpose — no pip install. Box-drawing panels + ANSI; honors NO_COLOR.

Keys:  j/k or ↑/↓ move · Enter open (jump to a cmux run's tab / attach a live lane /
       replay a finished one) · p open PR · t open issue · r reap (remove a dead/merged
       row) · q or Ctrl-C quit
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
# Wider cap than upstream's 140: this board carries two extra columns (ctx + cost), so
# a narrower cap would squeeze `note` to nothing on a wide terminal.
MAX_WIDTH = int(os.environ.get("AGENT_DASHBOARD_MAX_WIDTH", "220"))
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


def read_accurate_costs(snaps: list[dict], now: int | float) -> dict[str, float]:
    """UPPER(surface id) -> accurate USD, for the non-terminal rows we can bridge to a
    Claude session. Throttled to ACCURATE_COST_REFRESH_SECS (cost.py walks transcripts,
    so it's not a per-2s-refresh cost); the cache paints between refreshes."""
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
    bridge = _surface_to_sessionid()
    fresh: dict[str, float] = {}
    for s in snaps:
        if s.get("state") in TERMINAL_STATES:
            continue
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
# Grok: optional mirror only (no stable public weekly % API yet) — dashboard shows "—".
RATE_LIMIT_QUOTA = "claude-5h.txt"       # durable name
RATE_LIMIT_TMP = "/tmp/claude-rate-limit-5h.txt"
CLAUDE_WEEKLY_QUOTA = "claude-7d.txt"
CLAUDE_WEEKLY_TMP = "/tmp/claude-rate-limit-7d.txt"
CODEX_WEEKLY_QUOTA = "codex-7d.txt"
CODEX_WEEKLY_TMP = "/tmp/codex-rate-limit-7d.txt"
GROK_WEEKLY_QUOTA = "grok-7d.txt"
GROK_WEEKLY_TMP = "/tmp/grok-rate-limit-7d.txt"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Don't hammer OpenAI on every 2s refresh; 60s is plenty for a weekly bar.
CODEX_WEEKLY_REFRESH_SECS = int(os.environ.get("AGENT_DASHBOARD_CODEX_WEEKLY_REFRESH", "60"))
QUOTA_BAR_CELLS = 10
# The 5-hour plan usage limit ("Current session" in claude.ai settings) — this is the one
# that actually stops work when it hits 100, so RED starts well before the wall.
QUOTA_WARN, QUOTA_HOT, QUOTA_CRIT = 50, 75, 90
_codex_weekly_checked_at = 0.0


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

    Best-effort; never raises. Keys: claude_5h, claude_7d, codex_7d, grok_7d.
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


def format_quota_bar(label: str, pct: int, reset: int, now: int,
                     name_styles: tuple[str, ...] = ()) -> str:
    """One compact quota cell: 'claude 7d ███░░░░░░░ 42% · 3d04h'."""
    pct = max(0, min(100, pct))
    color = _quota_color(pct)
    filled = round(pct / 100 * QUOTA_BAR_CELLS)
    bar = c("█" * filled, *color) + c("░" * (QUOTA_BAR_CELLS - filled), "dim")
    name = c(label, *name_styles) if name_styles else c(label, "dim")
    return (name + " " + bar + " " + c(f"{pct}%", *color)
            + c(f" · {_format_resets(reset, now)}", "dim"))


def quota_cell(now: int) -> str:
    """Compact '5h ███░░░░░░░ 80% · 28m' for the title line. '' when unknown, so the
    title reads exactly as it used to on a client that doesn't report rate limits."""
    rl = read_rate_limit()
    if rl is None:
        return ""
    pct, reset = rl
    pct = max(0, min(100, pct))
    color = _quota_color(pct)
    filled = round(pct / 100 * QUOTA_BAR_CELLS)
    bar = c("█" * filled, *color) + c("░" * (QUOTA_BAR_CELLS - filled), "dim")
    return (c("5h ", "dim") + bar + " " + c(f"{pct}% used", *color)
            + c(f" · resets {_format_resets(reset, now)}", "dim"))


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


def weekly_quota_line(now: int) -> str:
    """One line under the title: weekly bars, one slot per vendor whose data is present.

    Claude always renders (the always-on seat, '—' until its first mirror). codex/grok
    render ONLY when a mirror actually exists — so an enterprise-Claude-only machine (no
    ~/.codex, no grok mirror) shows just claude, with no empty vendor slots. That is the
    vendor-neutral-out-of-the-box behavior: presence of data, not a config flag, decides
    what shows. Reads durable Projects/quota first, then /tmp; colors match MODEL_COLOR."""
    ensure_codex_weekly_fresh(now)
    #        name      durable                tmp                  name_styles          always
    slots = (
        ("claude", CLAUDE_WEEKLY_QUOTA, CLAUDE_WEEKLY_TMP, (),                    True),
        ("codex",  CODEX_WEEKLY_QUOTA,  CODEX_WEEKLY_TMP,  MODEL_COLOR["codex"],  False),
        ("grok",   GROK_WEEKLY_QUOTA,   GROK_WEEKLY_TMP,   MODEL_COLOR["grok"],   False),
    )
    parts: list[str] = []
    for name, durable, tmp, name_styles, always in slots:
        rl = read_quota(durable, tmp)
        if rl is None:
            if always:  # claude keeps a placeholder slot so the line is never empty
                parts.append(c(name, *(name_styles or ("dim",))) + c(" 7d —", "dim"))
            continue    # absent vendor -> no slot at all
        pct, reset = rl
        parts.append(format_quota_bar(f"{name} 7d", pct, reset, now, name_styles))
    return "   ".join(parts)


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
    """Whether the `r` key may remove this row's card: a terminal (merged/done) row, or a
    non-terminal one gone stale (no live pane past the threshold — the 💀 rows). A live or
    ghost row is never reapable, so `r` can't yank a card out from under a running agent."""
    return s.get("state") in TERMINAL_STATES or pane_state(s, cmux, lanes, now) == "stale"


def reap_snapshot(session: str) -> str:
    """The `r` action: remove one finished/dead row's snapshot through the single writer
    (emit-status.sh --remove). Clears a card off the board; it never touches a live run."""
    if not session:
        return "no row selected"
    try:
        subprocess.run(["bash", EMIT, "--remove", "--session", session], timeout=8)
        return f"reaped {session}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"reap failed: {e}"


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


def order_rows(snaps: list[dict], now: int) -> list[tuple[dict, bool]]:
    """The rows in the exact order they render (and the cursor walks): escalated, then
    waiting-at-gate, then active — each oldest-first — then terminal (dimmed, newest
    first). Terminal rows persist until reaped with `r`; capped to MAX_ROWS. Returns
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
    return rendered[:MAX_ROWS]


def render_frame(snaps: list[dict], cmux: set[str], lanes: set[str],
                 costs: dict[str, float], ctxs: dict[str, int], now: int,
                 term_width: int, sel_session: str | None = None,
                 status_msg: str = "") -> str:
    width = max(64, min(term_width, MAX_WIDTH))
    inner = width - 4
    w_note = max(6, inner - GUTTER - (_FIXED + _SEPS))
    bar_cells = CTX_BAR_CELLS

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

    rendered = order_rows(snaps, now)
    if not rendered:
        body.append("  " + c("no active runs — start a command or a dispatch lane", "dim"))
    for s, dim in rendered:
        add(s, dim=dim, selected=(s.get("session") == sel_session))
    overflow = max(0, len(snaps) - len(rendered))
    if overflow:
        body.append("  " + c(f"… +{overflow} more (raise AGENT_DASHBOARD_MAX_ROWS)", "dim"))

    active_n = sum(1 for s, d in rendered if not d and s.get("state") not in ("escalated", "waiting"))
    waiting_n = sum(1 for s, d in rendered if s.get("state") == "waiting")
    escal = [s for s, d in rendered if s.get("state") == "escalated"]
    done_n = sum(1 for s, d in rendered if d)
    # Total spend across the runs on screen. Cost is per Claude/Grok session, so a lane
    # that has handed off to a fresh tab contributes only its current session's spend.
    spend = sum(gauge_for(s, costs) or 0.0 for s, d in rendered)
    title = ("🎼 orchestra  " + datetime.now().strftime("%H:%M:%S")
             + f"   active {active_n}"
             + (f"   waiting {waiting_n}" if waiting_n else "")
             + (f"   🚨 escalated {len(escal)}" if escal else "   escalated 0")
             + f"   done {done_n}"
             + (f"   spend ${spend:,.2f}" if spend else ""))
    quota = quota_cell(now)
    weekly = weekly_quota_line(now)

    lines: list[str] = [c(title, "bold") + (("   " + quota) if quota else "")]
    if weekly:
        lines.append(weekly)
    if status_msg:
        lines.append(c("• " + status_msg, "cyan"))
    lines.append("")
    lines += panel("soloists", body, width)

    if PILOT_DIR is not None:
        lines.append("")
        lines += pilot_panel(width, now)

    if escal:
        ebody = []
        for s in escal:
            tkt = s.get("ticket", s.get("session", "?"))
            ebody.append(c(_cell(tkt, W_TKT + 2), "bold", "red") + " " + c(s.get("note", "(no note)"), "red"))
        lines.append("")
        lines += panel("🚨 escalations - need you", ebody, width, border=("red",))

    lines.append("")
    sysl = sys_line()
    keys = "j/k ↑↓ move · ⏎ open · p PR · t issue · r reap · q quit"
    footer = ((f"{sysl}   " if sysl else "") + keys
              + f"   state:{STATE_DIR}   quota:{QUOTA_DIR}   refresh:{REFRESH_SECS:g}s")
    lines.append(c(footer, "dim"))
    return "\033[H" + "".join(ln + "\033[K\n" for ln in lines) + "\033[J"


def start_pilot() -> "subprocess.Popen | None":
    """Co-launch pilot-light's sidecar loop, if configured. Returns the process
    (to stop on exit) or None. Never fatal: a pilot that won't start just means
    the dashboard renders its panel as idle, same as any other read failure."""
    if PILOT_DIR is None:
        return None
    loop = PILOT_DIR / "loop.sh"
    if not loop.exists():
        return None
    try:
        # Own process group so our Ctrl-C doesn't SIGINT it mid-window — we stop
        # it explicitly (SIGTERM) so an in-flight run is allowed to finish first.
        return subprocess.Popen(["bash", str(loop)], cwd=str(PILOT_DIR),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        return None


def stop_pilot(proc: "subprocess.Popen | None") -> None:
    """Signal the loop to stop after its current tick/window. loop.sh traps TERM
    and finishes any in-flight run before exiting, so the quota window isn't wasted."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
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
    w = shutil.get_terminal_size((120, 40)).columns
    snaps = load_snapshots()
    costs = read_costs()
    costs.update(read_accurate_costs(snaps, now))
    sys.stdout.write(render_frame(snaps, cmux_live(), tmux_lane_live(),
                                  costs, read_ctx(), now, w))
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
            w = shutil.get_terminal_size((120, 40)).columns
            snaps = load_snapshots()
            cmux, lanes = cmux_live(), tmux_lane_live()
            costs, ctxs = read_costs(), read_ctx()
            costs.update(read_accurate_costs(snaps, now))  # accurate overrides mirror where resolvable
            order = [s.get("session") for s, _ in order_rows(snaps, now)]
            if sel_session not in order:
                sel_session = order[0] if order else None
            sys.stdout.write(render_frame(snaps, cmux, lanes, costs, ctxs, now, w,
                                          sel_session, status_msg))
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
            elif key in ("j", "\x1b[B"):
                sel_session = move_sel(order, sel_session, +1)
            elif key in ("\r", "\n", "p", "t"):
                by_id = {s.get("session"): s for s, _ in order_rows(snaps, now)}
                snap = by_id.get(sel_session)
                act = "enter" if key in ("\r", "\n") else key
                status_msg = dispatch_action(act, snap, cmux, lanes, now)
            elif key == "r":
                by_id = {s.get("session"): s for s, _ in order_rows(snaps, now)}
                snap = by_id.get(sel_session)
                if snap is None:
                    status_msg = "no row selected"
                elif reapable(snap, cmux, lanes, now):
                    status_msg = reap_snapshot(snap.get("session", ""))
                    sel_session = None  # row is gone; next refresh snaps the cursor to the top
                else:
                    status_msg = f"won't reap a live row ({snap.get('state', '?')}) — only merged/done/stale"
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
