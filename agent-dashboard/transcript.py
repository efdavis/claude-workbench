#!/usr/bin/env python3
"""Render a Claude Code session transcript (.jsonl) as readable turns, for the dashboard's
replay action (Enter on a finished run).

The raw .jsonl is one JSON object per line and is dominated by hook/attachment/mode/session
metadata — unreadable in a pager. This keeps only the user + assistant turns and shows their
text, tool calls (by name + a one-line arg hint), tool results (truncated), and a compact
one-line marker for thinking blocks. Pipe it into a pager: transcript.py <file> | less -R.

Pure stdlib on purpose — no pip install, matching the rest of the harness python.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

SEP = "─" * 78
_RESULT_HEAD = 6  # tool-result lines shown before truncating


def _blocks(content):
    """Yield (kind, text) from a message.content, which is either a plain string or a list
    of typed blocks (text / thinking / tool_use / tool_result)."""
    if isinstance(content, str):
        if content.strip():
            yield ("text", content)
        return
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            yield ("text", b.get("text", ""))
        elif t == "thinking":
            yield ("thinking", b.get("thinking", ""))
        elif t == "tool_use":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            hint = ""
            for k in ("command", "file_path", "path", "pattern", "query", "url",
                      "description", "prompt"):
                if inp.get(k):
                    hint = str(inp[k]).splitlines()[0][:100]
                    break
            yield ("tool", f"{b.get('name', '?')}  {hint}".rstrip())
        elif t == "tool_result":
            c = b.get("content", "")
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
            yield ("result", str(c))


def _color(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _emit(kind: str, text: str) -> None:
    text = (text or "").rstrip()
    if not text:
        return
    if kind == "text":
        print(text)
    elif kind == "thinking":
        lines = text.splitlines()
        head = lines[0][:100] if lines else ""
        extra = f" (+{len(lines) - 1} more)" if len(lines) > 1 else ""
        print(_color(f"  · thinking: {head}{extra}", "2"))
    elif kind == "tool":
        print(_color(f"  ⏵ {text}", "36"))
    elif kind == "result":
        lines = text.strip().splitlines()
        shown = "\n".join(lines[:_RESULT_HEAD])
        more = f"\n… (+{len(lines) - _RESULT_HEAD} more lines)" if len(lines) > _RESULT_HEAD else ""
        print(_color("  ⤷ result:", "2"))
        print(_color(textwrap.indent(shown + more, "    "), "2"))


def render(path: str, out=sys.stdout) -> int:
    try:
        fh = open(path, "r")
    except OSError as e:
        print(f"cannot open {path}: {e}", file=sys.stderr)
        return 1
    turns = 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # skip a half-written / non-JSON line, never fatal
            if not isinstance(d, dict) or d.get("type") not in ("user", "assistant"):
                continue  # drop hooks / attachments / mode / title / metadata noise
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            role = (msg.get("role") or d.get("type") or "?").upper()
            ts = (d.get("timestamp") or "")[11:19]
            printed = False
            for kind, text in _blocks(msg.get("content")):
                if not (text or "").strip():
                    continue
                if not printed:
                    code = "1;33" if role == "USER" else "1;32"
                    print(_color(f"\n{SEP}\n{role}  {ts}\n{SEP}", code), file=out)
                    printed = True
                    turns += 1
                _emit(kind, text)
    if turns == 0:
        print("(no user/assistant turns found in this transcript)", file=out)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: transcript.py <path.jsonl>   (pipe into a pager: | less -R)", file=sys.stderr)
        return 2
    try:
        return render(sys.argv[1])
    except BrokenPipeError:
        # The pager was closed before we finished writing (e.g. `q` in less, or a `head`
        # pipe). Redirect stdout to devnull so the interpreter's shutdown flush doesn't
        # print a BrokenPipeError traceback. Documented CLI-tool idiom.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
