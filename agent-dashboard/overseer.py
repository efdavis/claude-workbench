#!/usr/bin/env python3
"""Reconcile dashboard rows against GitHub reality - the no-LLM overseer.

A row carrying a pr_number can outlive its emitting session: the run ends, the
PR lives on, and the dashboard shows pr-open forever. This loop asks GitHub what
actually happened and writes the truth back through emit-status.sh (the single
state writer), flipping rows whose PR merged (-> merged) or closed unmerged
(-> done). Open PRs are left alone - babysit owns their live status, and the
overseer never overwrites a live session's note.

Zero tokens: no agent, no LLM anywhere. One `gh pr view` call per reconcilable
row per tick. Stdlib only, like dashboard.py. Opt-in and human-started (a pane,
not a daemon - dies with the pane); Ctrl-C to quit.

Run it from inside the git repo whose PRs you want reconciled (it resolves the
repo from the current directory via `gh`).

Usage:
  python3 overseer.py                # loop, one pass per 60s
  python3 overseer.py --once         # single pass, then exit
  python3 overseer.py --interval 120
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMIT = HERE / "emit-status.sh"
_DASHBOARD_HOME = Path(os.environ.get(
    "AGENT_DASHBOARD_HOME",
    str(Path.home() / "Projects" / "claude-workbench" / "agent-dashboard"),
)).expanduser()
STATE_DIR = Path(os.environ.get("AGENT_DASHBOARD_STATE_DIR",
                                str(_DASHBOARD_HOME / "state"))).expanduser()
TERMINAL_STATES = {"merged", "done"}


def detect_repo() -> str | None:
    """"owner/repo" for the repo in the current directory, via gh. None on failure."""
    try:
        out = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                              "-q", ".nameWithOwner"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    repo = out.stdout.strip()
    return repo if out.returncode == 0 and repo else None


def pr_state(repo: str, number: str) -> str | None:
    """GitHub PR state: open | merged | closed (lower-cased). None on any failure."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(number), "-R", repo,
                              "--json", "state", "-q", ".state"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        return out.stdout.strip().lower() or None
    except (OSError, subprocess.SubprocessError):
        return None


def emit(snap: dict, state: str, note: str) -> None:
    """Write the reconciled state through the single writer. Best-effort."""
    args = [str(EMIT), "--session", snap["session"], "--role", snap.get("role", "other"),
            "--state", state, "--note", note]
    if snap.get("ticket"):
        args += ["--ticket", snap["ticket"]]
    if snap.get("pr_number"):
        args += ["--pr", str(snap["pr_number"])]
    if snap.get("worktree_path"):
        args += ["--worktree", snap["worktree_path"]]
    env = {k: v for k, v in os.environ.items() if k != "CMUX_SURFACE_ID"}
    try:
        # strip the overseer's own surface id: a reconciled row has no live pane
        subprocess.run(args, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        pass


def tick(repo: str) -> int:
    flipped = 0
    attempted = failed = 0
    for path in glob.glob(str(STATE_DIR / "*.json")):
        try:
            with open(path, "r") as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            continue  # half-written / unreadable - skip, never fatal
        if not isinstance(snap, dict) or not snap.get("session"):
            continue
        if snap.get("state") in TERMINAL_STATES or not snap.get("pr_number"):
            continue
        attempted += 1
        state = pr_state(repo, str(snap["pr_number"]))
        if state is None:
            failed += 1
            continue
        if state == "merged":
            emit(snap, "merged", f"PR #{snap['pr_number']} merged (overseer)")
            flipped += 1
        elif state == "closed":
            emit(snap, "done", f"PR #{snap['pr_number']} closed unmerged (overseer)")
            flipped += 1
    if attempted and failed == attempted:
        # every lookup failed: a healthy "0 flipped" and a dead gh must not look alike
        print("overseer: all GitHub lookups failed - gh auth expired? try: gh auth status",
              file=sys.stderr)
    return flipped


def main() -> int:
    ap = argparse.ArgumentParser(description="Flip dashboard rows whose PR merged/closed. No LLM, no tokens.")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between passes (default 60)")
    ns = ap.parse_args()

    if not shutil.which("gh"):
        print("gh not found - overseer needs it; nothing reconciled.", file=sys.stderr)
        return 0  # best-effort ethos: never a hard failure
    repo = detect_repo()
    if not repo:
        print("could not derive the GitHub repo from the current directory - run the overseer "
              "from inside your project repo (gh repo view must succeed).", file=sys.stderr)
        return 0

    try:
        while True:
            n = tick(repo)
            print(f"[{time.strftime('%H:%M:%S')}] overseer: {n} row(s) flipped")
            if ns.once:
                return 0
            time.sleep(max(1.0, ns.interval))  # clamp: negative would raise, 0 would hot-poll gh
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
