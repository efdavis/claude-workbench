#!/usr/bin/env bash
# Contract test for dashboard.py — the render + cursor + liveness logic.
# Unit-level (imports dashboard.py, exercises pure functions + render_frame) plus one
# CLI one-shot render. Pure stdlib python3, no deps. Exits non-zero on any failure.
# Run: ./dashboard.test.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
fails=0
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

# --- unit + render checks (python drives the width math it must get right) ---
if DASH_DIR="$here" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ["DASH_DIR"])
import dashboard as d

ok = True
def check(cond, name):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name)
    ok = ok and cond

now = 1_000_000

# 1. emoji-prefixed state column: glyph present, column stays aligned
snaps = [
    {"session": "s-imp", "role": "worker", "state": "implementing", "ticket": "PROJ-23", "epoch": now, "note": "n"},
    {"session": "s-esc", "role": "worker", "state": "escalated", "ticket": "PROJ-14", "epoch": now, "note": "n"},
    {"session": "s-mrg", "role": "finisher", "state": "merged", "ticket": "PROJ-1", "epoch": now, "note": "n"},
]
d.COLOR = False
frame = d.render_frame(snaps, set(), set(), {}, {}, now, 120)
check("🎻 implementing" in frame, "state column shows glyph-prefixed state")
# every panel body row (starts with │) is padded to the SAME display width — emoji counted as 2
body = [ln for ln in d._ANSI.sub("", frame).splitlines() if ln.startswith("│")]
widths = {d.vlen(ln) for ln in body}
check(len(widths) == 1, f"emoji rows column-aligned (widths={sorted(widths)})")

# 2. cursor: selected row gets the ▸ marker; reverse-video only when color is on
d.COLOR = False
frame = d.render_frame(snaps, set(), set(), {}, {}, now, 120, sel_session="s-esc")
sel_line = [ln for ln in frame.splitlines() if "s-esc" in ln][0]
check("▸ " in sel_line, "selected row shows ▸ marker (NO_COLOR)")
check("\x1b[7m" not in sel_line, "no reverse-video under NO_COLOR")
d.COLOR = True
frame = d.render_frame(snaps, set(), set(), {}, {}, now, 120, sel_session="s-esc")
sel_line = [ln for ln in frame.splitlines() if "s-esc" in ln][0]
check("\x1b[7m" in sel_line, "selected row is a reverse-video bar when color on")
other = [ln for ln in frame.splitlines() if "s-imp" in ln][0]
check("\x1b[7m" not in other, "non-selected rows are not reversed")
d.COLOR = False

# 3. move_sel: down / up / clamp / vanished-current / empty
order = ["a", "b", "c"]
check(d.move_sel(order, "a", +1) == "b", "move down")
check(d.move_sel(order, "b", -1) == "a", "move up")
check(d.move_sel(order, "c", +1) == "c", "clamp at bottom")
check(d.move_sel(order, "a", -1) == "a", "clamp at top")
check(d.move_sel(order, "gone", +1) == "a", "vanished current -> top")
check(d.move_sel([], "a", +1) is None, "empty order -> None")

# 4. lane_live_match: EXACT only — a sibling number must NOT cross-match
check(d.lane_live_match("PROJ-76-worker", {"PROJ-76"}) is True, "row-worker matches its lane")
check(d.lane_live_match("PROJ-76", {"PROJ-76"}) is True, "bare row matches its lane")
check(d.lane_live_match("PROJ-76-worker", {"PROJ-7"}) is False, "sibling PROJ-7 does NOT match PROJ-76-worker")
check(d.lane_live_match("PROJ-7-worker", {"PROJ-76"}) is False, "no reverse cross-match either")
check(d.lane_live_match("", {"PROJ-76"}) is False, "empty session never matches")

# 5. pane_state precedence: terminal never live; live-but-stale -> ghost; fresh match -> live
merged = {"session": "PROJ-1-worker", "state": "merged", "epoch": now}
check(d.pane_state(merged, set(), {"PROJ-1"}, now) == "-", "terminal row never live (no synth)")
fresh = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now}
check(d.pane_state(fresh, set(), {"PROJ-9"}, now) == "live", "fresh matched lane -> live")
old = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now - (20 * 60)}
check(d.pane_state(old, set(), {"PROJ-9"}, now) == "ghost", "matched-but-stale lane -> ghost")
gone = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now - (20 * 60)}
check(d.pane_state(gone, set(), set(), now) == "stale", "unmatched + past stale -> stale")

# 5b. reapable = "dead card, no kill step needed": terminal or stale. Live/ghost still
# get removed by `r`, but only after handler.sh ends the run (not card-only).
check(d.reapable({"session": "t", "state": "merged", "epoch": now}, set(), set(), now) is True,
      "terminal row is reapable (card-only)")
check(d.reapable({"session": "s", "state": "pr-open", "epoch": now - (20 * 60)}, set(), set(), now) is True,
      "stale row is reapable (card-only)")
_live = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now}
check(d.reapable(_live, set(), {"PROJ-9"}, now) is False, "live row needs end step first (not card-only)")
_ghost = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now - (20 * 60)}
check(d.reapable(_ghost, set(), {"PROJ-9"}, now) is False, "ghost row needs end step first (not card-only)")

# 5c. reap_snapshot shells emit-status --remove and deletes the on-disk card (not the run)
import os as _o, tempfile as _tf3, subprocess as _sp
_sd = _tf3.mkdtemp(); _o.environ["AGENT_DASHBOARD_STATE_DIR"] = _sd
_sp.run(["bash", d.EMIT, "--session", "reapme", "--role", "finisher", "--state", "merged"], timeout=8)
_card = _o.path.join(_sd, "reapme.json")
check(_o.path.exists(_card), "reap test: snapshot card created")
_msg = d.reap_snapshot("reapme")
check(not _o.path.exists(_card) and "reaped" in _msg, f"reap_snapshot deletes the card (msg={_msg!r})")
check(d.reap_snapshot("") == "no row selected", "reap_snapshot with no session -> friendly no-op")

# 5d. terminal rows persist in render — no auto-ageout in order_rows; a day-old merged row
# still renders (the file is cleared by `r` or the loop-level auto_prune, never by render).
_old = [{"session": "old", "role": "finisher", "state": "merged", "ticket": "PROJ-1",
         "epoch": now - (24 * 3600), "note": "n"}]
check(len(d.order_rows(_old, now)) == 1, "day-old terminal row persists (no auto-ageout)")

# 5e. auto_prune_terminal reaps only terminal rows past the cutoff — spares young terminal
# rows and any non-terminal (live/stale) row, and no-ops when disabled.
import time as _t
_now2 = int(_t.time())
_sp.run(["bash", d.EMIT, "--session", "oldterm", "--role", "finisher", "--state", "done"], timeout=8)
_sp.run(["bash", d.EMIT, "--session", "youngterm", "--role", "finisher", "--state", "done"], timeout=8)
_snaps5e = [
    {"session": "oldterm", "state": "done", "epoch": _now2 - 48 * 3600},
    {"session": "youngterm", "state": "done", "epoch": _now2 - 3600},
    {"session": "liveimpl", "state": "implementing", "epoch": _now2 - 48 * 3600},  # not terminal
]
d.REAP_TERMINAL_HOURS = 24
d._prune_checked_at = 0.0
_n = d.auto_prune_terminal(_snaps5e, _now2)
check(_n == 1, f"auto_prune reaps only the old terminal row (n={_n})")
check(not _o.path.exists(_o.path.join(_sd, "oldterm.json")), "auto_prune removed the old terminal card")
check(_o.path.exists(_o.path.join(_sd, "youngterm.json")), "auto_prune kept the young terminal card")
d.REAP_TERMINAL_HOURS = 0
d._prune_checked_at = 0.0
check(d.auto_prune_terminal(_snaps5e, _now2) == 0, "REAP_TERMINAL_HOURS<=0 disables the prune")

# 5f. new_escalations: silent on the first frame (None), fires on the transition into
# escalated, and does not re-fire while it stands or after it clears.
_esc = {"session": "e1", "state": "escalated", "epoch": _now2}
_new, _seen = d.new_escalations([_esc], None)
check(_new == [] and _seen == {"e1"}, "first frame seeds the seen-set without alerting")
_new, _seen = d.new_escalations([_esc], set())
check(len(_new) == 1 and _new[0]["session"] == "e1", "a newly-escalated row is reported as new")
_new, _seen = d.new_escalations([_esc], {"e1"})
check(_new == [], "an already-seen escalation does not re-alert")
_new, _seen = d.new_escalations([], {"e1"})
check(_new == [] and _seen == set(), "a cleared escalation empties the seen-set")

# 5g. quota window/lines readability: absent mirror -> labeled em-dash (not a vanished
# slot); present -> explicit "% used"; the claude line always exists with 5h before 7d.
check("—" in d._quota_win("5h", None, _now2), "absent quota window shows a labeled em-dash")
_qw = d._ANSI.sub("", d._quota_win("5h", (62, _now2 + 3600), _now2))
check("5h" in _qw and "62% used" in _qw, f"present quota window labels + '% used' ({_qw!r})")
_ql = d.quota_lines(_now2)
_c0 = d._ANSI.sub("", _ql[0])
check(len(_ql) >= 1 and _c0.startswith("claude"), "quota_lines always leads with the claude line")
check(_c0.index("5h") < _c0.index("7d"), "claude line puts the 5h wall before 7d")

# 5h. Grok SuperGrok weekly helpers: proto parse + weekly mirror paints as 7d.
check(d._grok_money_val({"val": 12119}) == 12119.0, "grok money unwraps {val: N}")
check(d._grok_money_val(42) == 42.0, "grok money accepts plain numbers")
check(d._grok_money_val(None) is None, "grok money None stays None")
_ge = d._parse_iso_epoch("2026-08-01T00:00:00+00:00")
check(isinstance(_ge, int) and _ge > 1_700_000_000, f"ISO epoch parses billingPeriodEnd ({_ge!r})")
check(d._parse_iso_epoch("2026-08-01T00:00:00Z") == _ge, "ISO epoch accepts trailing Z")
# Synthetic Connect envelope: config field1 = float 66.0 + period end timestamp.
import struct as _struct, tempfile as _tf2
_pct_bytes = _struct.pack("<f", 66.0)
# Timestamp seconds = field 1 varint. Encode a known epoch ~2026-07-22.
_reset = 1784746210
def _varint(n):
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80); n >>= 7
    out.append(n & 0x7F); return bytes(out)
_ts = b"\x08" + _varint(_reset)  # field 1 = seconds
_cfg = (b"\x0d" + _pct_bytes  # field 1 wire5 float
        + b"\x2a" + bytes([len(_ts)]) + _ts)  # field 5 = period end
_msg = b"\x0a" + bytes([len(_cfg)]) + _cfg  # outer field 1 = config
_env = b"\x00" + _struct.pack(">I", len(_msg)) + _msg
_parsed = d._parse_grok_credits_proto(_env)
check(_parsed is not None and _parsed[0] == 66 and _parsed[1] == _reset,
      f"parse GetGrokCreditsConfig envelope → 66% / reset ({_parsed!r})")
# Force a known weekly mirror and confirm quota_lines labels it `7d`.
_qdir = _tf2.mkdtemp(prefix="ad-quota-")
_prev_qdir = d.QUOTA_DIR
d.QUOTA_DIR = __import__("pathlib").Path(_qdir)
d._grok_weekly_checked_at = 1e18  # pin throttle so ensure_* never hits the network
d._codex_weekly_checked_at = 1e18
try:
    (d.QUOTA_DIR / "grok-7d.txt").write_text(f"66 {_now2 + 86400}\n")
    _ql2 = [d._ANSI.sub("", ln) for ln in d.quota_lines(_now2)]
    _grok_lines = [ln for ln in _ql2 if ln.strip().startswith("grok")]
    check(len(_grok_lines) == 1, f"grok weekly mirror yields one grok line ({_grok_lines!r})")
    check(_grok_lines[0].split()[1] == "7d",
          f"grok line uses 7d window label ({_grok_lines[0]!r})")
    check("66% used" in _grok_lines[0], f"grok line shows pct used ({_grok_lines[0]!r})")
finally:
    d.QUOTA_DIR = _prev_qdir
    import shutil as _shutil
    _shutil.rmtree(_qdir, ignore_errors=True)

# 6. no row overruns the panel border at a narrow width (80-col default). The cursor
# gutter + glyph-widened state column push the fixed geometry past a narrow inner width;
# every rendered body row must still clip to exactly the panel width, never spill it.
d.COLOR = True
frame80 = d.render_frame(snaps, set(), set(), {}, {}, now, 80)
body80 = [ln for ln in d._ANSI.sub("", frame80).splitlines() if ln.startswith("│")]
w80 = {d.vlen(ln) for ln in body80}
check(w80 == {80}, f"80-col rows clip to the border, none spill (widths={sorted(w80)})")
d.COLOR = False

# 7. char_width: VS16 (U+FE0F) and ZWJ (U+200D) are zero display columns
check(d.char_width("\ufe0f") == 0, "VS16 counts as zero width (category Mn)")
check(d.char_width("\u200d") == 0, "ZWJ counts as zero width (category Cf)")

# 7b. `n` note panel: wrap_display + show_note expands the full note the table truncates
check(d.wrap_display("", 10) == [""], "wrap empty -> one blank line")
check(d.wrap_display("hello world", 20) == ["hello world"], "wrap fits on one line")
_wrapped = d.wrap_display("one two three four five", 10)
check(_wrapped == ["one two", "three four", "five"] or all(d.vlen(x) <= 10 for x in _wrapped),
      f"wrap_display respects width (got {_wrapped!r})")
_long = "queue empty: only #137 [Sonnet] open, strictly blocked (#135->#136->#137)"
_snaps_note = [{"session": "s-long", "role": "worker", "state": "implementing",
                "ticket": "#137", "epoch": now, "note": _long}]
frame_note = d.render_frame(_snaps_note, set(), set(), {}, {}, now, 100,
                            sel_session="s-long", show_note=True)
check("note · #137" in d._ANSI.sub("", frame_note), "show_note panel title includes ticket")
check(_long in d._ANSI.sub("", frame_note) or "strictly blocked" in d._ANSI.sub("", frame_note),
      "show_note panel includes full note text (not truncated by the table cell)")
check("n note" in d._ANSI.sub("", frame_note), "footer legend lists n note")
frame_off = d.render_frame(_snaps_note, set(), set(), {}, {}, now, 100,
                           sel_session="s-long", show_note=False)
# Without show_note the panel title must not appear (table may still hold a truncated note).
check("note · #137" not in d._ANSI.sub("", frame_off), "show_note=False omits the note panel")
_empty_note = [{"session": "s-empty", "role": "worker", "state": "done",
                "ticket": "X-1", "epoch": now, "note": ""}]
frame_empty = d.render_frame(_empty_note, set(), set(), {}, {}, now, 100,
                             sel_session="s-empty", show_note=True)
check("(no note)" in d._ANSI.sub("", frame_empty), "empty note shows (no note) placeholder")

# 8. dispatch_action seam: a non-string snapshot field (e.g. int pr_number) must NOT raise —
# it coerces and shells out, honoring the module's "malformed snapshot never fatal" rule.
import os as _os, tempfile as _tf
_sh = _tf.NamedTemporaryFile("w", suffix=".sh", delete=False)
_sh.write('#!/usr/bin/env bash\necho "shim: $*"\n'); _sh.close(); _os.chmod(_sh.name, 0o755)
d.HANDLER = _sh.name
_snap = {"session": "x", "ticket": "PROJ-9", "state": "pr-open", "pr_number": 51, "worktree_path": None}
try:
    _res = d.dispatch_action("p", _snap, set(), set(), now)
    check(isinstance(_res, str) and "shim:" in _res, f"dispatch_action coerces non-str fields, no crash (got {_res!r})")
except Exception as e:
    check(False, f"dispatch_action raised on a non-string field: {e!r}")
finally:
    _os.unlink(_sh.name)

# 9. accurate-cost path: best-effort, never fatal, and overlays the mirror where resolvable.
import time as _time
d._accurate_cost_cache.clear(); d._accurate_cost_checked_at = 0.0
check(d.read_accurate_costs([], _time.time()) == {}, "no snaps -> no accurate costs")
d._accurate_cost_cache.clear(); d._accurate_cost_checked_at = 0.0
# a row with no cmux_surface can't be bridged to a session -> skipped, no crash
check(d.read_accurate_costs([{"session": "s", "state": "implementing"}], _time.time()) == {},
      "unbridgeable row -> skipped")
# a bogus UUID has no transcript under ~/.claude/projects -> None (caller keeps the mirror)
check(d._cost_for_session("00000000-0000-0000-0000-000000000000") is None,
      "unknown session uuid -> None (mirror fallback)")
# throttle: a warm cache paints between refreshes without re-shelling
d._accurate_cost_cache.clear(); d._accurate_cost_cache["ABC"] = 42.0
d._accurate_cost_checked_at = _time.time()
check(d.read_accurate_costs([], _time.time()) == {"ABC": 42.0}, "warm cache paints under throttle")
# throttle-bypass fix: an EMPTY-but-fresh cache (a window that resolved nothing) must STILL
# short-circuit — gating on emptiness re-shelled the glob+subprocess every frame. Detect by
# counting _surface_to_sessionid calls: a short-circuit never reaches it.
d._accurate_cost_cache.clear()
d._accurate_cost_checked_at = _time.time()
_orig_s2s = d._surface_to_sessionid
_s2s_calls = {"n": 0}
d._surface_to_sessionid = lambda: (_s2s_calls.__setitem__("n", _s2s_calls["n"] + 1) or {})
try:
    _r = d.read_accurate_costs([{"session": "s", "state": "implementing", "cmux_surface": "X"}], _time.time())
    check(_r == {} and _s2s_calls["n"] == 0, "empty+fresh cache short-circuits, no re-shell (throttle-bypass fix)")
finally:
    d._surface_to_sessionid = _orig_s2s

sys.exit(0 if ok else 1)
PY
then pass "unit/render checks passed"; else fail "unit/render checks"; fi

# --- pilot-light lifecycle: reference-counted co-launch (start_pilot/stop_pilot) ---
# The pilot must run iff >=1 dashboard is open: first-open spawns it (if not already
# running), last-close stops it, in any order — and a durable launchd/manual pilot
# (no dashboard-written loop.pid) is NEVER killed by refcount teardown. These drive
# real loop.sh subprocesses against a throwaway PILOT_LIGHT_DIR.
if DASH_DIR="$here" python3 - <<'PY'
import os, sys, time, subprocess, tempfile, signal
from pathlib import Path
sys.path.insert(0, os.environ["DASH_DIR"])
import dashboard as d

ok = True
def check(cond, name):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name)
    ok = ok and cond

tmp = Path(tempfile.mkdtemp())
d.PILOT_DIR = tmp                      # helpers read this global at call time
loop = tmp / "loop.sh"
# A stand-in loop.sh: named loop.sh so _pilot_already_running's ps/identity check
# recognizes it, exits promptly on SIGTERM like the real one's TERM trap.
loop.write_text("#!/usr/bin/env bash\ntrap 'exit 0' TERM\nwhile true; do sleep 0.2; done\n")
loop.chmod(0o755)

spawned = []  # everything we launch, killed in the finally
def raw_loop():
    """Start a loop.sh WITHOUT going through start_pilot (no loop.pid) — a stand-in
    for a hand-started `bash loop.sh &` / launchd-style durable pilot."""
    p = subprocess.Popen(["bash", str(loop)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    spawned.append(p)
    return p

def loop_pids():
    r = subprocess.run(["pgrep", "-f", str(loop)], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []

def wait_gone(pid, t=6.0):
    end = time.time() + t
    while time.time() < end:
        try: os.kill(pid, 0)
        except ProcessLookupError: return True
        time.sleep(0.05)
    return False

def reaped(p, t=6.0):
    # Death check for a pilot we spawned in-process: os.kill(pid,0) still sees a killed
    # child as alive until it's reaped, so poll() (which reaps) is the honest probe. In
    # real use the adopted pilot isn't the closer's child, so init reaps it — this is a
    # test-harness artifact of us being both spawner and closer here.
    end = time.time() + t
    while time.time() < end:
        if p.poll() is not None: return True
        time.sleep(0.05)
    return False

def reg_entries():
    reg = tmp / "state" / "dashboards"
    return sorted(p.name for p in reg.iterdir()) if reg.exists() else []

def clean():
    # Full reset between scenarios: kill any pilot, wipe pidfile + registry.
    for p in loop_pids():
        try: os.kill(p, signal.SIGKILL)
        except OSError: pass
    for sub in (tmp / "state" / "loop.pid",):
        sub.unlink(missing_ok=True)
    reg = tmp / "state" / "dashboards"
    if reg.exists():
        for e in reg.iterdir(): e.unlink()

try:
    me = os.getpid()

    # 1. First dashboard opens: spawns exactly one pilot, registers itself as a consumer.
    clean()
    proc = d.start_pilot()
    time.sleep(0.3)
    check(proc is not None and len(loop_pids()) == 1, "first start_pilot spawns exactly one pilot")
    check(str(me) in reg_entries(), "start_pilot registers this dashboard as a consumer")
    check(d._pilot_already_running() == proc.pid, "loop.pid names the live loop.sh")

    # 2. Second dashboard opens (re-entrant start): adopts, does NOT double-spawn.
    proc2 = d.start_pilot()
    time.sleep(0.2)
    check(proc2 is None and len(loop_pids()) == 1, "second start_pilot adopts — no double-spawn")

    # 3. Not the last to close: a sibling is still open -> pilot keeps running.
    sib = subprocess.Popen(["sleep", "30"]); spawned.append(sib)   # a live 'other dashboard'
    (tmp / "state" / "dashboards" / str(sib.pid)).touch()
    d.stop_pilot(proc)                                             # this dashboard closes first
    time.sleep(0.2)
    check(len(loop_pids()) == 1, "non-last close leaves the pilot running")
    check(str(me) not in reg_entries(), "closing unregisters this dashboard")
    check(str(sib.pid) in reg_entries(), "the still-open sibling stays registered")

    # 4. Last to close stops it — even as an ADOPTER holding no Popen (proc is None).
    #    Emulate the sibling being the final closer: its entry is the only one left, and
    #    the closer only has the bare pid from loop.pid to signal.
    pilot_pid = loop_pids()[0]
    (tmp / "state" / "dashboards" / str(sib.pid)).unlink()        # sibling unregisters itself
    sib.terminate()                                               # (and its process exits)
    d.stop_pilot(None)                                            # adopter, last out, no Popen
    check(reaped(proc), "last close stops the pilot even with no owned Popen")
    check(reg_entries() == [], "registry empty after the last dashboard closes")
    check(not (tmp / "state" / "loop.pid").exists(), "loop.pid cleared after teardown")

    # 5. Order independence: whoever is last stops it. Spawner-closes-first was covered in
    #    (3)+(4); here the spawner is ALSO the last out -> it stops its own pilot directly.
    clean()
    proc = d.start_pilot(); time.sleep(0.3)
    pilot_pid = loop_pids()[0]
    d.stop_pilot(proc)                                            # sole consumer closes
    check(wait_gone(pilot_pid), "sole dashboard closing stops its own pilot")

    # 6. Crash resilience: a consumer force-killed without stop_pilot leaves a stale entry;
    #    the next start_pilot prunes it rather than counting a phantom forever.
    clean()
    dead = subprocess.Popen(["sleep", "30"]); dead_pid = dead.pid
    dead.kill(); dead.wait()                                      # pid now dead
    reg = tmp / "state" / "dashboards"; reg.mkdir(parents=True, exist_ok=True)
    (reg / str(dead_pid)).touch()
    proc = d.start_pilot(); time.sleep(0.3)
    check(str(dead_pid) not in reg_entries(), "start_pilot prunes a crashed sibling's stale entry")
    # ...and that self-heal makes the correct last-close call: with only THIS dashboard
    # really live, closing it stops the pilot (the phantom doesn't wedge it open).
    pilot_pid = loop_pids()[0]
    d.stop_pilot(proc)
    check(wait_gone(pilot_pid), "pruned phantom doesn't keep the pilot alive on last close")

    # 7. DURABILITY (the OQ#1 safety property): a live loop.sh with NO dashboard-written
    #    loop.pid (a hand-started / launchd-style durable pilot) is never adopted as
    #    kill-eligible — closing the last dashboard must leave it running.
    clean()
    durable = raw_loop(); time.sleep(0.3)                         # running, but no loop.pid
    check(not (tmp / "state" / "loop.pid").exists(), "durable pilot left no loop.pid")
    (tmp / "state" / "dashboards").mkdir(parents=True, exist_ok=True)
    (tmp / "state" / "dashboards" / str(me)).touch()             # a dashboard opens alongside it
    d.stop_pilot(None)                                            # ...and closes, last out
    time.sleep(0.3)
    check(durable.poll() is None, "last close never kills a durable pilot it didn't spawn")

    # 8. Stale loop.pid from a killed pilot (single-owner case) still recovers under the
    #    registry layer: a bogus pidfile doesn't wedge the next spawn.
    clean()
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    (tmp / "state" / "loop.pid").write_text("999999")            # a pid that isn't alive
    check(d._pilot_already_running() is None, "stale loop.pid (dead pid) reads as not-running")
    proc = d.start_pilot(); time.sleep(0.3)
    check(len(loop_pids()) == 1, "start_pilot recovers from a stale pidfile and spawns")
    d.stop_pilot(proc); wait_gone(loop_pids()[0] if loop_pids() else -1)

finally:
    for p in loop_pids():
        try: os.kill(p, signal.SIGKILL)
        except OSError: pass
    for p in spawned:
        try: p.kill()
        except OSError: pass

sys.exit(0 if ok else 1)
PY
then pass "pilot lifecycle checks passed"; else fail "pilot lifecycle checks"; fi

# --- CLI one-shot render (non-tty branch) over a seeded state dir ---
D="$(mktemp -d)/state"; export AGENT_DASHBOARD_STATE_DIR="$D"
env -u CMUX_SURFACE_ID "$here/emit-status.sh" --session cli-imp --role worker --state implementing --ticket PROJ-23 --note hi
out="$(NO_COLOR=1 python3 "$here/dashboard.py")"; rc=$?
{ [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "🎻 implementing"; } \
  && pass "CLI one-shot renders emoji state" || fail "CLI one-shot (rc=$rc)"
printf '%s' "$out" | grep -q "🎼 orchestra" && pass "orchestra header present" || fail "orchestra header"

echo
if [ "$fails" -eq 0 ]; then echo "all dashboard tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
