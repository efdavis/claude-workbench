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

# 5b. reapable (the `r` guard): terminal or stale is reapable; a live/ghost row is not,
# so `r` can never yank a card out from under a running agent.
check(d.reapable({"session": "t", "state": "merged", "epoch": now}, set(), set(), now) is True,
      "terminal row is reapable")
check(d.reapable({"session": "s", "state": "pr-open", "epoch": now - (20 * 60)}, set(), set(), now) is True,
      "stale row is reapable")
_live = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now}
check(d.reapable(_live, set(), {"PROJ-9"}, now) is False, "live row is NOT reapable")
_ghost = {"session": "PROJ-9-worker", "state": "implementing", "epoch": now - (20 * 60)}
check(d.reapable(_ghost, set(), {"PROJ-9"}, now) is False, "ghost row is NOT reapable")

# 5c. reap_snapshot shells emit-status --remove and deletes the on-disk card (not the run)
import os as _o, tempfile as _tf3, subprocess as _sp
_sd = _tf3.mkdtemp(); _o.environ["AGENT_DASHBOARD_STATE_DIR"] = _sd
_sp.run(["bash", d.EMIT, "--session", "reapme", "--role", "finisher", "--state", "merged"], timeout=8)
_card = _o.path.join(_sd, "reapme.json")
check(_o.path.exists(_card), "reap test: snapshot card created")
_msg = d.reap_snapshot("reapme")
check(not _o.path.exists(_card) and "reaped" in _msg, f"reap_snapshot deletes the card (msg={_msg!r})")
check(d.reap_snapshot("") == "no row selected", "reap_snapshot with no session -> friendly no-op")

# 5d. terminal rows persist — no auto-ageout; a day-old merged row still renders (cleared only by r)
_old = [{"session": "old", "role": "finisher", "state": "merged", "ticket": "PROJ-1",
         "epoch": now - (24 * 3600), "note": "n"}]
check(len(d.order_rows(_old, now)) == 1, "day-old terminal row persists (no auto-ageout)")

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

# --- CLI one-shot render (non-tty branch) over a seeded state dir ---
D="$(mktemp -d)/state"; export AGENT_DASHBOARD_STATE_DIR="$D"
env -u CMUX_SURFACE_ID "$here/emit-status.sh" --session cli-imp --role worker --state implementing --ticket PROJ-23 --note hi
out="$(NO_COLOR=1 python3 "$here/dashboard.py")"; rc=$?
{ [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "🎻 implementing"; } \
  && pass "CLI one-shot renders emoji state" || fail "CLI one-shot (rc=$rc)"
printf '%s' "$out" | grep -q "🎼 orchestra" && pass "orchestra header present" || fail "orchestra header"

echo
if [ "$fails" -eq 0 ]; then echo "all dashboard tests passed"; exit 0; else echo "$fails test(s) failed"; exit 1; fi
