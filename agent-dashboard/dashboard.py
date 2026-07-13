#!/usr/bin/env python3
"""Agent dashboard — a live, read-only view of agent runs.

Watches the status snapshots written by emit-status.sh (see status.schema.json)
and renders a live cross-section of what every run is doing: which issue, which
role, what state, how long, and which ones need you (escalations).

Read-only and best-effort by design: it never mutates a run, and a malformed or
half-written snapshot is skipped, never fatal. Point it at the same state dir the
emitter uses (AGENT_DASHBOARD_STATE_DIR, default ~/.claude/agent-dashboard/state)
and leave it open in a pane while you drive implement / babysit-pr / code-review
in others.

Pure stdlib on purpose — no pip install. Box-drawing panels + ANSI; honors NO_COLOR.

Run:  python3 dashboard.py            (Ctrl-C to quit)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENT_DASHBOARD_STATE_DIR",
                                str(Path.home() / ".claude" / "agent-dashboard" / "state")))
# Optional pilot-light sidecar (see ../../emberfall/pilot-light). When this points
# at a configured pilot-light dir, the dashboard co-launches its loop.sh and shows
# a pilot panel. Unset -> the dashboard is exactly its old view-only self.
PILOT_DIR = Path(os.environ["PILOT_LIGHT_DIR"]).expanduser() if os.environ.get("PILOT_LIGHT_DIR") else None
REFRESH_SECS = float(os.environ.get("AGENT_DASHBOARD_REFRESH", "2"))
STALE_SECS = int(os.environ.get("AGENT_DASHBOARD_STALE_SECS", str(15 * 60)))
TERMINAL_AGEOUT_SECS = int(os.environ.get("AGENT_DASHBOARD_AGEOUT_SECS", str(10 * 60)))
MAX_WIDTH = int(os.environ.get("AGENT_DASHBOARD_MAX_WIDTH", "220"))
MAX_ROWS = int(os.environ.get("AGENT_DASHBOARD_MAX_ROWS", "30"))

TERMINAL_STATES = {"merged", "done"}
COLOR = ("NO_COLOR" not in os.environ)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_C = {"reset": "0", "bold": "1", "dim": "2", "red": "31", "green": "32",
      "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
      "orange": "38;5;208"}
STATE_COLOR = {
    "started": ("cyan",), "implementing": ("yellow",), "reviewing": ("blue",),
    "waiting": ("bold", "yellow"), "pr-open": ("magenta",), "merged": ("green",),
    "done": ("green", "dim"), "escalated": ("bold", "red"),
}
ROLE_GLYPH = {"planner": "P", "worker": "W", "reviewer": "R",
              "finisher": "F", "groomer": "G", "other": "-"}
MODEL_COLOR = {"opus": ("magenta",), "sonnet": ("cyan",), "haiku": ("green",),
               "grok": ("orange",)}


def model_cell(model: str) -> tuple[str, tuple[str, ...]]:
    """Short display name + color for a model string; matches on family substring
    so both 'opus' and 'claude-opus-4-8' render as 'opus'. Unknown -> raw + dim.
    Not all runs are Claude: Eric's Grok seats emit model=grok."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "grok"):
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


RATE_LIMIT_FILE = "/tmp/claude-rate-limit-5h.txt"
QUOTA_BAR_CELLS = 10
# The 5-hour plan usage limit ("Current session" in claude.ai settings) — this is the one
# that actually stops work when it hits 100, so RED starts well before the wall.
QUOTA_WARN, QUOTA_HOT, QUOTA_CRIT = 50, 75, 90


def read_rate_limit() -> "tuple[int, int] | None":
    """(5h usage %, reset epoch) as mirrored by statusline.sh from Claude's own
    rate_limits payload. Account-wide, so any session's mirror speaks for all of them.
    Same best-effort contract as the other readers: unreadable/absent -> None."""
    try:
        pct, reset = Path(RATE_LIMIT_FILE).read_text().split()
        return int(pct), int(reset)
    except (OSError, ValueError):
        return None


def quota_cell(now: int) -> str:
    """Compact '5h ███░░░░░░░ 80% · 28m' for the title line. '' when unknown, so the
    title reads exactly as it used to on a client that doesn't report rate limits."""
    rl = read_rate_limit()
    if rl is None:
        return ""
    pct, reset = rl
    pct = max(0, min(100, pct))
    if pct > QUOTA_CRIT:
        color = ("bold", "red")
    elif pct > QUOTA_HOT:
        color = ("orange",)
    elif pct > QUOTA_WARN:
        color = ("yellow",)
    else:
        color = ("green",)
    filled = round(pct / 100 * QUOTA_BAR_CELLS)
    bar = c("█" * filled, *color) + c("░" * (QUOTA_BAR_CELLS - filled), "dim")
    left = max(0, reset - now)
    resets = f"{left // 3600}h{(left % 3600) // 60:02d}m" if left >= 3600 else f"{left // 60}m"
    return (c("5h ", "dim") + bar + " " + c(f"{pct}% used", *color)
            + c(f" · resets {resets}", "dim"))


def c(text: str, *styles: str) -> str:
    if not COLOR or not styles:
        return text
    codes = ";".join(_C[s] for s in styles if s in _C)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def vpad(s: str, width: int) -> str:
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


def tmux_live() -> set[str]:
    live: set[str] = set()
    for args in (["tmux", "list-sessions", "-F", "#{session_name}"],
                 ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{pane_id}"]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=2)
            if out.returncode == 0:
                live.update(x for x in out.stdout.split() if x)
        except (OSError, subprocess.SubprocessError):
            pass
    return live


_UUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")


def cmux_live() -> set[str]:
    """UUIDs of live cmux nodes (surfaces included); empty when cmux is absent.
    Snapshots carry their emitter's $CMUX_SURFACE_ID (see emit-status.sh); set
    membership here is the anchored match — no title/substring matching, same
    false-positive discipline as is_live below."""
    try:
        out = subprocess.run(["cmux", "tree", "--all", "--id-format", "uuids"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode == 0:
            return {m.group(0).upper() for m in _UUID_RE.finditer(out.stdout)}
    except (OSError, subprocess.SubprocessError):
        pass
    return set()


def is_live(session: str, live: set[str]) -> bool:
    """Anchored matching only. Bare substring matching false-positives on default
    tmux session names ('0', '1', ...), which are substrings of most issue ids."""
    if not session:
        return False
    for t in live:
        if session == t:
            return True
        if len(t) >= 4 and (t.startswith(session) or session.startswith(t)):
            return True
    return False


def humanize_age(epoch: int, now: int) -> str:
    d = max(0, now - int(epoch or 0))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    return f"{d // 3600}h{(d % 3600) // 60}m"


def _cell(text, width: int) -> str:
    s = str(text)
    if len(s) > width:
        s = s[: max(0, width - 1)] + "…"
    return s.ljust(width)


def panel(title: str, body: list[str], width: int, border: tuple[str, ...] = ()) -> list[str]:
    """Box-drawing panel. body lines are visible-width-padded to the inner width."""
    inner = width - 4  # "│ " + inner + " │"
    seg = f"─ {title} " if title else "──"
    top = c("┌" + seg + "─" * max(0, width - 2 - vlen(seg)) + "┐", *border)
    bot = c("└" + "─" * (width - 2) + "┘", *border)
    out = [top]
    for ln in body:
        out.append(c("│", *border) + " " + vpad(ln, inner) + " " + c("│", *border))
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


def render_frame(snaps: list[dict], live: set[str], cmux: set[str], costs: dict[str, float],
                 ctxs: dict[str, int], now: int, term_width: int) -> str:
    width = max(64, min(term_width, MAX_WIDTH))
    visible = [s for s in snaps
               if not (s.get("state") in TERMINAL_STATES
                       and (now - int(s.get("epoch", 0))) > TERMINAL_AGEOUT_SECS)]
    escalated = [s for s in visible if s.get("state") == "escalated"]
    waiting = [s for s in visible if s.get("state") == "waiting"]
    active = [s for s in visible
              if s.get("state") not in TERMINAL_STATES and s.get("state") not in ("escalated", "waiting")]
    terminal = [s for s in visible if s.get("state") in TERMINAL_STATES]
    escalated.sort(key=lambda s: int(s.get("epoch", 0)))
    waiting.sort(key=lambda s: int(s.get("epoch", 0)))
    active.sort(key=lambda s: int(s.get("epoch", 0)))
    terminal.sort(key=lambda s: int(s.get("epoch", 0)), reverse=True)

    # column widths sized to the inner panel width — real session ids run ~20-26
    # chars (e.g. "ember-auto-sonnet-g2-511f44") so RUN gets real headroom; NOTE
    # (the description) absorbs whatever width remains, which grows with the window.
    inner = width - 4
    W_ROLE, W_ST, W_MODEL, W_COST, W_AGE, W_PANE = 4, 12, 7, 9, 7, 5
    W_RUN = 24
    # `issue` is sized to the longest ticket actually on screen, not a flat 12 — a board of
    # "#20"s shouldn't reserve slug-width. Floor of 5 keeps the header legible; the ceiling
    # keeps one long slug from eating `note`.
    W_TKT = max(5, min(14, max((len(str(s.get("ticket") or "-")) for s in visible), default=5)))
    SEP = " │ "  # dim vertical rule between columns, easier to scan than a bare space
    seps_width = 9 * len(SEP)  # 9 gaps between 10 columns

    # The context bar is the first thing to go when the pane is narrow: `note` carries the
    # escalation text and must stay legible, so we only spend width on the bar if `note`
    # still clears NOTE_MIN afterwards. Below that we fall back to a bare "65%" (4 cols).
    NOTE_MIN = 24

    def _layout(bar_cells: int) -> tuple[int, int]:
        w_ctx = (bar_cells + 5) if bar_cells else 4  # bar + " " + "100%"
        fixed = W_RUN + W_ROLE + W_TKT + W_ST + W_MODEL + w_ctx + W_COST + W_AGE + W_PANE
        return w_ctx, inner - (fixed + seps_width)

    bar_cells = CTX_BAR_CELLS
    W_CTX, W_NOTE = _layout(bar_cells)
    if W_NOTE < NOTE_MIN:
        bar_cells = 0
        W_CTX, W_NOTE = _layout(bar_cells)
    W_NOTE = max(6, W_NOTE)

    def header_row(*labels):
        widths = (W_RUN, W_ROLE, W_TKT, W_ST, W_MODEL, W_CTX, W_COST, W_AGE, W_PANE, W_NOTE)
        cells = [c(_cell(lbl, w), "bold") for lbl, w in zip(labels, widths)]
        return c(SEP, "dim").join(cells)

    header = header_row("run", "role", "issue", "state", "model",
                        "context" if bar_cells else "ctx", "cost", "age", "pane", "note")
    row_width = (W_RUN + W_ROLE + W_TKT + W_ST + W_MODEL + W_CTX + W_COST + W_AGE + W_PANE
                 + W_NOTE + seps_width)
    rule = c("─" * row_width, "dim")
    body = [header, rule]

    def add(s: dict, dim: bool):
        st = s.get("state", "?")
        epoch = int(s.get("epoch", 0))
        stale = st not in TERMINAL_STATES and (now - epoch) > STALE_SECS
        pane = ("live" if (str(s.get("cmux_surface", "")).upper() in cmux
                           or is_live(s.get("session", ""), live))
                else ("stale" if stale else "-"))
        base = ("dim",) if dim else ()
        sep = c(SEP, "dim")
        mdisp, mcolor = model_cell(s.get("model", ""))
        xdisp, xcolor = ctx_cell(gauge_for(s, ctxs), bar_cells)
        cdisp, ccolor = cost_cell(gauge_for(s, costs))
        line = sep.join([
            c(_cell(s.get("session", "?"), W_RUN), *base),
            c(_cell(ROLE_GLYPH.get(s.get("role", "other"), "-"), W_ROLE), *base),
            c(_cell(s.get("ticket", "-"), W_TKT), *base),
            c(_cell(st, W_ST), *STATE_COLOR.get(st, ("white",))),
            c(_cell(mdisp, W_MODEL), *(("dim",) if dim else mcolor)),
            c(_cell(xdisp, W_CTX), *(("dim",) if dim else xcolor)),
            c(_cell(cdisp, W_COST), *(("dim",) if dim else ccolor)),
            c(_cell(humanize_age(epoch, now), W_AGE), *(("red",) if stale else base)),
            c(_cell(pane, W_PANE), *(("green",) if pane == "live" else ("red",) if pane == "stale" else ("dim",))),
            c(_cell(s.get("note", ""), W_NOTE), *base),
        ])
        body.append(line)

    if not visible:
        body.append(c("no active runs — start a command in another pane", "dim"))
    # escalated first (they appear here too — the summary panel below lacks age/pane),
    # then waiting-at-gate, active, and dimmed terminal rows; capped to MAX_ROWS
    rendered = ([(s, False) for s in escalated + waiting + active]
                + [(s, True) for s in terminal])
    overflow = max(0, len(rendered) - MAX_ROWS)
    for s, dim in rendered[:MAX_ROWS]:
        add(s, dim=dim)
    if overflow:
        body.append(c(f"… +{overflow} more (raise AGENT_DASHBOARD_MAX_ROWS)", "dim"))

    # Total spend across the runs on screen. Cost is per Claude/Grok session, so a lane that
    # has handed off to a fresh tab contributes only its current session's spend.
    spend = sum(gauge_for(s, costs) or 0.0 for s in visible)

    title = ("agent dashboard  " + datetime.now().strftime("%H:%M:%S")
             + f"   active {len(active)}"
             + (f"   waiting {len(waiting)}" if waiting else "")
             + (f"   ! escalated {len(escalated)}" if escalated else "   escalated 0")
             + f"   done {len(terminal)}"
             + (f"   spend ${spend:,.2f}" if spend else ""))
    quota = quota_cell(now)

    lines: list[str] = [c(title, "bold") + (("   " + quota) if quota else ""), ""]
    lines += panel("runs", body, width)

    if PILOT_DIR is not None:
        lines.append("")
        lines += pilot_panel(width, now)

    if escalated:
        ebody = []
        for s in escalated:
            tkt = s.get("ticket", s.get("session", "?"))
            ebody.append(c(_cell(tkt, W_TKT + 2), "bold", "red") + " " + c(s.get("note", "(no note)"), "red"))
        lines.append("")
        lines += panel("! escalations - need you", ebody, width, border=("red",))

    lines.append("")
    sysl = sys_line()
    footer = (f"{sysl}   " if sysl else "") + f"state:{STATE_DIR}   refresh:{REFRESH_SECS:g}s   Ctrl-C to quit"
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


def main() -> int:
    if not sys.stdout.isatty():
        now = int(time.time())
        w = shutil.get_terminal_size((120, 40)).columns
        sys.stdout.write(render_frame(load_snapshots(), tmux_live(), cmux_live(), read_costs(),
                                      read_ctx(), now, w))
        return 0
    if not STATE_DIR.exists():
        sys.stderr.write(f"state dir {STATE_DIR} does not exist yet — waiting for the first snapshot…\n")
    pilot = start_pilot()
    sys.stdout.write("\033[?1049h\033[?25l")  # alt screen + hide cursor
    try:
        while True:
            now = int(time.time())
            w = shutil.get_terminal_size((120, 40)).columns
            sys.stdout.write(render_frame(load_snapshots(), tmux_live(), cmux_live(), read_costs(),
                                          read_ctx(), now, w))
            sys.stdout.flush()
            time.sleep(REFRESH_SECS)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_pilot(pilot)
        sys.stdout.write("\033[?25h\033[?1049l")  # restore
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
