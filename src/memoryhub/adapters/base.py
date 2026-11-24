"""Adapter protocol for external memory sources (P2-03).

Two constraints come from the contract, not from taste:

- ``can_write`` is always ``False``. MemoryHub never writes back into another
  tool's native memory files (ADR-002).
- ``read`` takes a :class:`SourcePath`, never raw text, so an adapter is free to
  parse markdown, JSONL or SQLite and the core never learns which (ADR-011).

Provenance is not decoration: ``layer`` and ``precedence`` carry "where this
source sits in its tool's instruction chain", because Codex and Claude both give
nearer files priority and that ordering is lost the moment you flatten sources
into a list of strings.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import MemoryEntry


@dataclass(frozen=True)
class SourcePath:
    """A discovered source file plus the provenance needed to interpret it."""

    path: Path
    adapter: str
    source_kind: str
    layer: str = ""
    precedence: int = 0
    #: How sure the adapter is that this really is a source of memories.
    confidence: str = "medium"
    note: str = ""

    def content_hash(self) -> str:
        digest = hashlib.sha256()
        with Path(self.path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()


class Adapter(ABC):
    """Base class: subclasses declare a name, a default source_kind, and two methods."""

    name: str = "base"
    source_kind: str = "doc"
    can_read: bool = True
    #: Hard-wired by ADR-002. Do not override.
    can_write: bool = False

    @abstractmethod
    def discover(self) -> List[SourcePath]:
        """Return every source this adapter can see, ordered by precedence."""

    @abstractmethod
    def read(self, source: SourcePath) -> List[MemoryEntry]:
        """Parse one source into pending entries. Never writes anything."""

    def capabilities(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "source_kind": self.source_kind,
            "can_read": self.can_read,
            "can_write": self.can_write,
        }


# ------------------------------------------------------------------ helpers


def strip_quoted_metadata(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Pull ``> key: value`` lines off the header of a markdown file.

    Some tools (WorkBuddy) record metadata as blockquotes instead of frontmatter,
    for example ``> Last updated: 2026-11-15T07:52:40+08:00``, and they put the
    document title above it. So the header region is scanned until the first
    bullet or second-level heading; blank lines and ``#`` titles are skipped.
    """
    meta: Dict[str, str] = {}
    if text.startswith("\ufeff"):
        # A BOM would hide the first line, and with it the whole header.
        text = text[1:]
    lines = text.splitlines()
    consumed = 0
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("# ") or line == "#":
            consumed = index + 1
            continue
        if not line.startswith(">"):
            break
        body = line.lstrip(">").strip()
        if ":" in body:
            key, _, value = body.partition(":")
            meta[key.strip().lower()] = value.strip()
        consumed = index + 1
    return meta, lines[consumed:]


def bullets(text: str) -> List[Tuple[str, str]]:
    """Split markdown into ``(section_title, bullet_text)`` pairs.

    Only top-level bullets (``- `` / ``* ``) become memories; indented
    continuation lines are folded into the bullet they belong to.
    """
    found: List[Tuple[str, str]] = []
    section = ""
    current: Optional[List[str]] = None

    def flush() -> None:
        nonlocal current
        if current:
            joined = " ".join(part.strip() for part in current if part.strip())
            if joined:
                found.append((section, joined))
        current = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            section = stripped.lstrip("#").strip()
            continue
        if stripped in ("---", "***"):
            flush()
            continue
        if stripped.startswith(("- ", "* ")) and not line[:1].isspace():
            flush()
            current = [stripped[2:]]
            continue
        if current is not None and stripped:
            current.append(stripped)
        elif not stripped:
            flush()
    flush()
    return found
