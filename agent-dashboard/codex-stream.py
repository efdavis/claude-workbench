#!/usr/bin/env python3
"""Render and optionally follow a local ``codex exec --json`` activity stream."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
MAX_RESULT_LINES = 8
MAX_RESULT_CHARS = 2400


def sanitize(text: str) -> str:
    text = re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[image omitted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|authorization|token|password|secret)\b(\s*[:=]\s*)([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    return re.sub(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{512,}={0,2}", "[large payload omitted]", text)


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def one_line(value: object, limit: int = 180) -> str:
    text = " ".join(sanitize(str(value or "")).split())
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


def compact_result(value: object) -> str:
    text = sanitize(str(value or "").replace("\x00", ""))
    if len(text) > MAX_RESULT_CHARS:
        omitted = len(text) - MAX_RESULT_CHARS
        text = text[:MAX_RESULT_CHARS] + f"\n… ({omitted} characters omitted)"
    lines = text.rstrip().splitlines()
    if len(lines) > MAX_RESULT_LINES:
        omitted = len(lines) - MAX_RESULT_LINES
        lines = lines[:MAX_RESULT_LINES] + [f"… ({omitted} lines omitted)"]
    return "\n".join(f"    {line}" for line in lines)


def render(record: object) -> None:
    if not isinstance(record, dict):
        return
    kind = record.get("type")
    if kind == "thread.started":
        print(color(f"── generation started  {one_line(record.get('thread_id'))}", "1;34"), flush=True)
    elif kind == "turn.started":
        print(color("── turn started", "1;34"), flush=True)
    elif kind == "turn.completed":
        usage = record.get("usage") or {}
        hint = f"  {usage}" if usage else ""
        print(color(f"── turn completed{hint}", "1;32"), flush=True)
    elif kind in {"error", "turn.failed"}:
        print(color(f"!! {one_line(record.get('message') or record.get('error') or record)}", "1;31"), flush=True)
    elif kind in {"item.started", "item.completed"}:
        item = record.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        completed = kind == "item.completed"
        if item_type == "agent_message" and completed:
            text = str(item.get("text") or "").strip()
            if text:
                print(color("\nASSISTANT", "1;32"), flush=True)
                print(text, flush=True)
        elif item_type == "command_execution":
            command = one_line(item.get("command"), 220)
            if not completed:
                print(color(f"  ⏵ command  {command}", "36"), flush=True)
            else:
                status = item.get("status") or "completed"
                exit_code = item.get("exit_code")
                suffix = f" (exit {exit_code})" if exit_code is not None else ""
                print(color(f"  ⤷ {status}{suffix}", "2"), flush=True)
                output = compact_result(item.get("aggregated_output"))
                if output:
                    print(color(output, "2"), flush=True)
        elif item_type == "file_change":
            changes = item.get("changes") or []
            label = "changed" if completed else "changing"
            for change in changes if isinstance(changes, list) else []:
                if isinstance(change, dict):
                    print(color(f"  ⏵ {label} {change.get('kind', 'file')}  {change.get('path', '')}", "36"), flush=True)


def consume(handle, partial: bytes) -> bytes:
    data = partial + handle.read()
    lines = data.split(b"\n")
    partial = lines.pop()
    for raw in lines:
        if not raw.strip() or len(raw) > 4 * 1024 * 1024:
            continue
        try:
            render(json.loads(raw))
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
    return partial


def identity(path: Path):
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino)
    except OSError:
        return None


def run(path: Path, follow: bool, interval: float = 0.15) -> int:
    handle = None
    current = None
    partial = b""
    try:
        while True:
            found = identity(path)
            if found != current:
                if handle:
                    handle.close()
                try:
                    handle = path.open("rb")
                except OSError as exc:
                    if not follow:
                        print(f"cannot open {path}: {exc}", file=sys.stderr)
                        return 1
                    handle = None
                    time.sleep(interval)
                    continue
                current = found
                partial = b""
                print(color(f"\n── stream {path}", "1;34"), flush=True)
            if handle:
                partial = consume(handle, partial)
            if not follow:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if handle:
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    return run(args.path.expanduser(), args.follow)


if __name__ == "__main__":
    raise SystemExit(main())
