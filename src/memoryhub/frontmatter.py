"""Minimal, dependency-free frontmatter reader/writer.

Phase 1 deliberately supports a restricted subset of YAML:

- flat ``key: value`` pairs only (no nesting, no multiline strings)
- scalars: str / int / float / bool / null
- inline lists: ``[a, b, "c d"]``

This keeps the storage layer zero-dependency and therefore testable on a clean
machine. Phase 2 migrates to a real YAML library (see docs/01-decisions.md
ADR-005) together with Obsidian interoperability tests.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

DELIM = "---"
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


class FrontmatterError(ValueError):
    """Raised when a frontmatter block cannot be parsed."""


def _split_inline(inner: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if s == "" or s in ("null", "~", "None"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [parse_scalar(p) for p in _split_inline(inner)]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(dump_scalar(v) for v in value) + "]"
    text = str(value)
    needs_quote = (
        text == ""
        or text != text.strip()
        or any(c in text for c in ':#[]{}"\'')
    )
    if needs_quote:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def split(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a document into (metadata, body)."""
    if not text.startswith(DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIM:
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == DELIM:
            end = index
            break
    if end is None:
        raise FrontmatterError("frontmatter block is not terminated by '---'")

    meta: Dict[str, Any] = {}
    for line in lines[1:end]:
        raw = line.rstrip("\r\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _KEY_RE.match(raw)
        if not match:
            raise FrontmatterError(
                f"unsupported frontmatter line (only flat 'key: value' is allowed): {raw!r}"
            )
        meta[match.group(1)] = parse_scalar(match.group(2))
    body = "".join(lines[end + 1 :])
    return meta, body


def join(meta: Dict[str, Any], body: str) -> str:
    """Render metadata + body back into a frontmatter document."""
    lines = [DELIM]
    for key, value in meta.items():
        lines.append(f"{key}: {dump_scalar(value)}")
    lines.append(DELIM)
    head = "\n".join(lines) + "\n"
    return head + body
