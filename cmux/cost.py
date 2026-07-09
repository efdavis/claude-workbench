#!/usr/bin/env python3
"""cost.py — token-dollar accounting for a Claude Code session or project.

Claude Code writes a JSONL transcript for every session under
`~/.claude/projects/<slug>/<session-id>.jsonl`, and each assistant record
carries an exact `usage` block (input / output / cache-read / cache-write, the
write split 5m-vs-1h) plus the `model` and `timestamp`. That is the billing
basis — this script sums it and applies the list-price table below. No network,
no API calls; it only reads local transcript files.

Subagents (the Task tool) and Workflow agents write their OWN transcripts,
nested under `<slug>/<session-id>/subagents/...` (Workflow agents go a level
deeper, under `subagents/workflows/wf_*/`). A naive top-level `*.jsonl` glob
misses all of them and silently under-reports fan-out-heavy sessions —
sometimes by the majority of spend. This walks every nested transcript and
breaks the spawned-agent total out on its own line so the fan-out cost is
visible.

Cache reads dominate agentic spend — that's expected, not a leak: a long
session re-reads (cache-reads) its whole growing transcript every turn, so a
cold restart / `/clear` between unrelated tasks caps that growth.

Usage:
  cost.py                       # current cwd's project dir, all sessions
  cost.py --project-dir <dir>   # an explicit ~/.claude/projects/<slug> dir
  cost.py --session <uuid>      # restrict to one session within the dir
  cost.py --by-day              # also bucket spend by UTC day
  cost.py --since <ISO>         # ignore records before this timestamp
  cost.py --json                # JSON instead of a table

Cost is Anthropic *list* price (see PRICES below) — good for relative
comparison and budgeting, not a negotiated/blended rate. Bump PRICES when the
public rates change.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

# Anthropic standard API list rates, USD per *million* tokens. Bump when rates move.
# w5m / w1h = 5-minute / 1-hour ephemeral cache-write multipliers.
PRICES = {
    "fable":  {"in": 10.0, "out": 50.0, "read": 1.00, "w5m": 12.50, "w1h": 20.0},
    "opus":   {"in": 15.0, "out": 75.0, "read": 1.50, "w5m": 18.75, "w1h": 30.0},
    "sonnet": {"in":  3.0, "out": 15.0, "read": 0.30, "w5m":  3.75, "w1h":  6.0},
    "haiku":  {"in":  1.0, "out":  5.0, "read": 0.10, "w5m":  1.25, "w1h":  2.0},
}


def family(model):
    m = model or ""
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return "opus"  # unknown new model → price as opus (conservative / loud)


def slug(path):
    """Claude Code's project-dir transform: '/' and '.' → '-'."""
    return re.sub(r"[/.]", "-", path)


def is_subagent(path, proj):
    """True if this transcript is a spawned agent (Task OR Workflow) rather than
    a main session transcript. Main transcripts sit directly in the project dir
    (`<proj>/<session-id>.jsonl`); every agent transcript is nested at least one
    level below it — `<session-id>/subagents/agent-*.jsonl`, or deeper for
    Workflow agents under `<session-id>/subagents/workflows/wf_*/`. So the test
    is simply: is the path nested below the project dir at all."""
    return os.sep in os.path.relpath(path, proj)


def session_of(path, proj):
    """The session a transcript belongs to: the file stem for a main transcript,
    or — for a nested agent transcript at ANY depth — the first path component
    under the project dir, which is always the launching session id."""
    rel = os.path.relpath(path, proj)
    parts = rel.split(os.sep)
    if len(parts) == 1:
        return parts[0][:-6] if parts[0].endswith(".jsonl") else parts[0]
    return parts[0]


def parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None


def blank():
    return {"in": 0, "out": 0, "read": 0, "w5m": 0, "w1h": 0, "msgs": 0}


_SCHEMA_WARNED = False


def _warn_schema_drift():
    global _SCHEMA_WARNED
    if not _SCHEMA_WARNED:
        _SCHEMA_WARNED = True
        print("cost.py: WARNING — assistant transcript records with no `usage` block; "
              "the internal JSONL schema may have changed. Costs may be understated.",
              file=sys.stderr)


def scan(paths, root, session=None, since=None, by_day=None):
    """Sum usage across the given JSONL files, split by model family.

    `root` is the project dir the paths live under — used to classify each
    transcript as main-vs-agent and to derive its session id. `session`
    restricts to one or more session ids (a single uuid or an iterable).
    `since` (datetime) drops earlier records. `by_day` (dict), if provided, is
    filled with per-UTC-day USD totals.
    """
    want = None
    if session:
        sids = [session] if isinstance(session, str) else list(session)
        want = set(sids)
    out = defaultdict(blank)  # grand total (main + agents), by model family
    sub = defaultdict(blank)  # spawned-agent subset (Task + Workflow), by family
    for path in paths:
        # Match on session id (file stem, or the launching session of a nested
        # agent transcript) — NOT basename, so agent transcripts at any depth
        # (`<uuid>/subagents/...`, incl. `subagents/workflows/wf_*/`) count.
        if want is not None and session_of(path, root) not in want:
            continue
        is_sub = is_subagent(path, root)
        try:
            fh = open(path)
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message") or {}
                u = msg.get("usage")
                model = msg.get("model")
                if not u or not model:
                    # The transcript usage schema is internal/unversioned and can
                    # change between Claude Code releases. Assistant turns with NO
                    # usage block mean the schema likely drifted — warn once so a
                    # parse change degrades loudly, not as a silent $0.
                    if o.get("type") == "assistant" and not u:
                        _warn_schema_drift()
                    continue
                ts = parse_iso(o.get("timestamp"))
                if since and ts and ts < since:
                    continue
                fam = family(model)
                cc = u.get("cache_creation") or {}
                w1h = cc.get("ephemeral_1h_input_tokens", 0)
                w5m = cc.get("ephemeral_5m_input_tokens", 0)
                if not cc:  # older transcripts without the split → treat as 5m
                    w5m = u.get("cache_creation_input_tokens", 0)
                # Always accrue to the grand total; also to the subagent subset
                # when this record came from a nested subagents/ transcript.
                for c in ((out[fam], sub[fam]) if is_sub else (out[fam],)):
                    c["in"] += u.get("input_tokens", 0)
                    c["out"] += u.get("output_tokens", 0)
                    c["read"] += u.get("cache_read_input_tokens", 0)
                    c["w5m"] += w5m
                    c["w1h"] += w1h
                    c["msgs"] += 1
                if by_day is not None and ts:
                    by_day[ts.strftime("%Y-%m-%d")] += dollars({fam: {
                        "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                        "read": u.get("cache_read_input_tokens", 0), "w5m": w5m, "w1h": w1h,
                        "msgs": 1}})
    return out, sub


def dollars(by_fam):
    total = 0.0
    for fam, c in by_fam.items():
        p = PRICES[fam]
        total += (c["in"] * p["in"] + c["out"] * p["out"] + c["read"] * p["read"]
                  + c["w5m"] * p["w5m"] + c["w1h"] * p["w1h"]) / 1e6
    return total


def fam_breakdown(by_fam):
    return {fam: {**c, "usd": round(dollars({fam: c}), 4)}
            for fam, c in by_fam.items() if c["msgs"]}


def projects_root():
    # $CLAUDE_PROJECTS_DIR is a test-only seam; production uses ~/.claude/projects.
    env = os.environ.get("CLAUDE_PROJECTS_DIR", "")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def project_mode(args):
    since = parse_iso(args.since) if args.since else None
    by_day = defaultdict(float) if args.by_day else None
    # Default to the current cwd's transcript dir if --project-dir not given.
    proj = os.path.abspath(args.project_dir or os.path.join(projects_root(), slug(os.getcwd())))
    # One recursive glob catches main transcripts (`<proj>/*.jsonl`) AND every
    # nested agent transcript at any depth. A non-recursive `*.jsonl` misses
    # agents entirely; a `**/subagents/*.jsonl` glob still misses Workflow agents
    # nested a further level under `subagents/workflows/wf_*/`. `**` gets them all.
    files = sorted(set(glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True)))
    if not files:
        # Degrade loudly, not as a silent $0 (matches the schema-drift warning's intent).
        print(f"cost.py: WARNING — no transcript (*.jsonl) files under {proj!r}. "
              "Pass a full path to --project-dir (or omit it to use the current cwd); "
              "a bare slug name resolves relative to the cwd, not ~/.claude/projects.",
              file=sys.stderr)
    by_fam, sub_fam = scan(files, proj, session=args.session, since=since, by_day=by_day)
    result = {
        "project_dir": proj,
        "session": args.session,
        "total_usd": round(dollars(by_fam), 4),
        "by_model": fam_breakdown(by_fam),
    }
    sub_usd = round(dollars(sub_fam), 4)
    if sub_usd:
        # Count agent transcripts within the same session scope as the sum.
        n_agents = sum(1 for f in files if is_subagent(f, proj)
                       and (not args.session or session_of(f, proj) == args.session))
        result["subagent_usd"] = sub_usd
        result["subagent_agents"] = n_agents
        result["subagent_by_model"] = fam_breakdown(sub_fam)
    if by_day is not None:
        result["by_day_usd"] = {d: round(v, 4) for d, v in sorted(by_day.items())}
    return result


def render_human(r):
    lines = [f"{r['project_dir']} — total ${r['total_usd']:,.2f}  (list price)"]
    if r.get("session"):
        lines.append(f"  session {r['session']}")
    if r.get("subagent_usd"):
        pct = 100 * r["subagent_usd"] / r["total_usd"] if r["total_usd"] else 0
        lines.append(f"  of which subagents: ${r['subagent_usd']:,.2f}  "
                     f"({pct:.0f}% of total, {r['subagent_agents']} agent transcripts)")
    if r.get("by_model"):
        lines.append("  by model:")
        for fam, b in r["by_model"].items():
            lines.append(f"    {fam:<7} {b['msgs']:>4} msgs  "
                         f"in={b['in']:>9,} out={b['out']:>8,} "
                         f"cache_read={b['read']:>11,} cache_write={b['w5m']+b['w1h']:>10,}  "
                         f"${b['usd']:,.2f}")
    if r.get("by_day_usd"):
        lines.append("  by day:")
        for d, v in r["by_day_usd"].items():
            lines.append(f"    {d}  ${v:,.2f}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Token-dollar accounting for a Claude Code session/project.")
    ap.add_argument("--project-dir", help="a ~/.claude/projects/<slug> dir (default: current cwd's)")
    ap.add_argument("--session", help="restrict to one session uuid")
    ap.add_argument("--since", help="ignore records before this ISO timestamp")
    ap.add_argument("--by-day", action="store_true", help="also bucket by UTC day")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    result = project_mode(args)
    print(json.dumps(result, indent=2) if args.json else render_human(result))


if __name__ == "__main__":
    main()
