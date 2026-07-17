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
~/.claude/agent-dashboard/state) and leave it open in a pane while dispatch spawns lanes
on the private `tmux -L <socket>` socket (AGENT_DASHBOARD_TMUX_SOCKET).

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

STATE_DIR = Path(os.environ.get("AGENT_DASHBOARD_STATE_DIR",
                                str(Path.home() / ".claude" / "agent-dashboard" / "state")))
REFRESH_SECS = float(os.environ.get("AGENT_DASHBOARD_REFRESH", "2"))
STALE_SECS = int(os.environ.get("AGENT_DASHBOARD_STALE_SECS", str(15 * 60)))
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
      "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37"}
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
MODEL_COLOR = {"opus": ("magenta",), "sonnet": ("cyan",), "haiku": ("green",)}
PANE_COLOR = {"live": ("green",), "ghost": ("yellow",), "stale": ("red",)}


def model_cell(model: str) -> tuple[str, tuple[str, ...]]:
    """Short display name + color for a model string; matches on family substring
    so both 'opus' and 'claude-opus-4-8' render as 'opus'. Unknown -> raw + dim."""
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam, MODEL_COLOR[fam]
    return (model or "-"), ("dim",)


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


# Column geometry. GUTTER is the 2-col cursor marker ("▸ " / "  "); W_ST is wide enough
# for the longest glyph-prefixed state ("🎻 implementing" = 2+1+12 = 15 display cols).
GUTTER = 2
W_RUN, W_ROLE, W_TKT, W_ST, W_MODEL, W_AGE, W_PANE = 16, 4, 9, 15, 7, 7, 5
_FIXED = W_RUN + W_ROLE + W_TKT + W_ST + W_MODEL + W_AGE + W_PANE
_SEPS = 7  # single spaces between the 8 columns


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


def render_frame(snaps: list[dict], cmux: set[str], lanes: set[str], now: int,
                 term_width: int, sel_session: str | None = None,
                 status_msg: str = "") -> str:
    width = max(64, min(term_width, MAX_WIDTH))
    inner = width - 4
    w_note = max(6, inner - GUTTER - (_FIXED + _SEPS))

    def rowcells(run, role, tkt, st, model, age, pane, note):
        return " ".join([_cell(run, W_RUN), _cell(role, W_ROLE), _cell(tkt, W_TKT),
                         _cell(st, W_ST), _cell(model, W_MODEL), _cell(age, W_AGE),
                         _cell(pane, W_PANE), _cell(note, w_note)])

    header = "  " + rowcells("run", "role", "issue", "state", "model", "age", "pane", "note")
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
        marker = "▸ " if selected else "  "
        if selected:
            # A clean reverse-video bar (+ ▸). Plain cells so the row's inner ANSI resets
            # don't punch holes in the reverse run; the ▸ carries selection under NO_COLOR.
            line = marker + rowcells(
                s.get("session", "?"), ROLE_GLYPH.get(s.get("role", "other"), "-"),
                s.get("ticket", "-"), st_disp, mdisp, humanize_age(epoch, now), pane,
                s.get("note", ""))
            body.append(f"\033[7m{line}\033[0m" if COLOR else line)
            return
        base = ("dim",) if dim else ()
        line = marker + " ".join([
            c(_cell(s.get("session", "?"), W_RUN), *base),
            c(_cell(ROLE_GLYPH.get(s.get("role", "other"), "-"), W_ROLE), *base),
            c(_cell(s.get("ticket", "-"), W_TKT), *base),
            c(_cell(st_disp, W_ST), *STATE_COLOR.get(st, ("white",))),
            c(_cell(mdisp, W_MODEL), *(("dim",) if dim else mcolor)),
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
    title = ("🎼 orchestra  " + datetime.now().strftime("%H:%M:%S")
             + f"   active {active_n}"
             + (f"   waiting {waiting_n}" if waiting_n else "")
             + (f"   🚨 escalated {len(escal)}" if escal else "   escalated 0")
             + f"   done {done_n}")

    lines: list[str] = [c(title, "bold")]
    if status_msg:
        lines.append(c("• " + status_msg, "cyan"))
    lines.append("")
    lines += panel("soloists", body, width)

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
    footer = (f"{sysl}   " if sysl else "") + f"{keys}   state:{STATE_DIR}   refresh:{REFRESH_SECS:g}s"
    lines.append(c(footer, "dim"))
    return "\033[H" + "".join(ln + "\033[K\n" for ln in lines) + "\033[J"


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
    sys.stdout.write(render_frame(load_snapshots(), cmux_live(), tmux_lane_live(), now, w))
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
            order = [s.get("session") for s, _ in order_rows(snaps, now)]
            if sel_session not in order:
                sel_session = order[0] if order else None
            sys.stdout.write(render_frame(snaps, cmux, lanes, now, w, sel_session, status_msg))
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
