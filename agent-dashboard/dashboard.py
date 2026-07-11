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
REFRESH_SECS = float(os.environ.get("AGENT_DASHBOARD_REFRESH", "2"))
STALE_SECS = int(os.environ.get("AGENT_DASHBOARD_STALE_SECS", str(15 * 60)))
TERMINAL_AGEOUT_SECS = int(os.environ.get("AGENT_DASHBOARD_AGEOUT_SECS", str(10 * 60)))
MAX_WIDTH = int(os.environ.get("AGENT_DASHBOARD_MAX_WIDTH", "140"))
MAX_ROWS = int(os.environ.get("AGENT_DASHBOARD_MAX_ROWS", "30"))

TERMINAL_STATES = {"merged", "done"}
COLOR = ("NO_COLOR" not in os.environ)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_C = {"reset": "0", "bold": "1", "dim": "2", "red": "31", "green": "32",
      "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37"}
STATE_COLOR = {
    "started": ("cyan",), "implementing": ("yellow",), "reviewing": ("blue",),
    "waiting": ("bold", "yellow"), "pr-open": ("magenta",), "merged": ("green",),
    "done": ("green", "dim"), "escalated": ("bold", "red"),
}
ROLE_GLYPH = {"planner": "P", "worker": "W", "reviewer": "R",
              "finisher": "F", "groomer": "G", "other": "-"}
MODEL_COLOR = {"opus": ("magenta",), "sonnet": ("cyan",), "haiku": ("green",)}


def model_cell(model: str) -> tuple[str, tuple[str, ...]]:
    """Short display name + color for a model string; matches on family substring
    so both 'opus' and 'claude-opus-4-8' render as 'opus'. Unknown -> raw + dim."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam, MODEL_COLOR[fam]
    return (model or "-"), ("dim",)


COST_GLOB = "/tmp/claude-cost-usd-surface-*.txt"
_COST_PREFIX = "claude-cost-usd-surface-"
# $ thresholds for the cost column; RED tracks the auto-lane's $100 handoff cap, so a
# red cell means "this lane is about to hand off", not just "expensive".
COST_WARN, COST_HOT = 25.0, 75.0


def read_costs() -> dict[str, float]:
    """Cumulative USD keyed by cmux surface id, as mirrored by the statusline on every
    render (Claude's cost.total_cost_usd — subagent spend included). Snapshots carry the
    same $CMUX_SURFACE_ID, so this is the join. Same best-effort contract as the
    snapshots: an unreadable or half-written mirror is skipped, and a run with no mirror
    (not launched under cmux, or statusline never rendered) just shows '-'."""
    costs: dict[str, float] = {}
    for path in glob.glob(COST_GLOB):
        surface = os.path.basename(path)[len(_COST_PREFIX):-len(".txt")]
        try:
            costs[surface.upper()] = float(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
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


def render_frame(snaps: list[dict], live: set[str], cmux: set[str], costs: dict[str, float],
                 now: int, term_width: int) -> str:
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

    # column widths sized to the inner panel width
    inner = width - 4
    W_ROLE, W_TKT, W_ST, W_MODEL, W_COST, W_AGE, W_PANE = 4, 9, 12, 7, 9, 7, 5
    W_RUN = 16
    seps = 8  # single spaces between 9 columns
    W_NOTE = max(6, inner - (W_RUN + W_ROLE + W_TKT + W_ST + W_MODEL + W_COST + W_AGE + W_PANE + seps))

    def rowcells(run, role, tkt, st, model, cost, age, pane, note):
        return " ".join([_cell(run, W_RUN), _cell(role, W_ROLE), _cell(tkt, W_TKT),
                         _cell(st, W_ST), _cell(model, W_MODEL), _cell(cost, W_COST),
                         _cell(age, W_AGE), _cell(pane, W_PANE), _cell(note, W_NOTE)])

    body = [c(rowcells("run", "role", "issue", "state", "model", "cost", "age", "pane", "note"), "dim")]

    def add(s: dict, dim: bool):
        st = s.get("state", "?")
        epoch = int(s.get("epoch", 0))
        stale = st not in TERMINAL_STATES and (now - epoch) > STALE_SECS
        pane = ("live" if (str(s.get("cmux_surface", "")).upper() in cmux
                           or is_live(s.get("session", ""), live))
                else ("stale" if stale else "-"))
        base = ("dim",) if dim else ()
        mdisp, mcolor = model_cell(s.get("model", ""))
        cdisp, ccolor = cost_cell(costs.get(str(s.get("cmux_surface", "")).upper()))
        line = " ".join([
            c(_cell(s.get("session", "?"), W_RUN), *base),
            c(_cell(ROLE_GLYPH.get(s.get("role", "other"), "-"), W_ROLE), *base),
            c(_cell(s.get("ticket", "-"), W_TKT), *base),
            c(_cell(st, W_ST), *STATE_COLOR.get(st, ("white",))),
            c(_cell(mdisp, W_MODEL), *(("dim",) if dim else mcolor)),
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

    # Total spend across the runs on screen. Cost is per Claude session, so a lane that
    # has handed off to a fresh tab contributes only its current session's spend.
    spend = sum(costs.get(str(s.get("cmux_surface", "")).upper()) or 0.0 for s in visible)

    title = ("agent dashboard  " + datetime.now().strftime("%H:%M:%S")
             + f"   active {len(active)}"
             + (f"   waiting {len(waiting)}" if waiting else "")
             + (f"   ! escalated {len(escalated)}" if escalated else "   escalated 0")
             + f"   done {len(terminal)}"
             + (f"   spend ${spend:,.2f}" if spend else ""))

    lines: list[str] = [c(title, "bold"), ""]
    lines += panel("runs", body, width)

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


def main() -> int:
    if not sys.stdout.isatty():
        now = int(time.time())
        w = shutil.get_terminal_size((120, 40)).columns
        sys.stdout.write(render_frame(load_snapshots(), tmux_live(), cmux_live(), read_costs(), now, w))
        return 0
    if not STATE_DIR.exists():
        sys.stderr.write(f"state dir {STATE_DIR} does not exist yet — waiting for the first snapshot…\n")
    sys.stdout.write("\033[?1049h\033[?25l")  # alt screen + hide cursor
    try:
        while True:
            now = int(time.time())
            w = shutil.get_terminal_size((120, 40)).columns
            sys.stdout.write(render_frame(load_snapshots(), tmux_live(), cmux_live(), read_costs(), now, w))
            sys.stdout.flush()
            time.sleep(REFRESH_SECS)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?25h\033[?1049l")  # restore
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
