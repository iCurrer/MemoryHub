"""WorkBuddy adapter: ``~/.workbuddy/memory/<uuid>_memory.md``.

Format observed on a real machine (see ``docs/04-upstream-formats.md`` §5): plain
markdown, no YAML frontmatter, metadata in blockquotes, one bullet per memory.

This is the first real adapter on purpose. It is the cheapest source that still
produces genuine user memory (a profile), so it proves the whole path --
discover -> read -> inbox -> approve -- without pulling SQLite or zstd into the
loop.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..models import MemoryEntry
from .base import Adapter, SourcePath, bullets, strip_quoted_metadata

_STAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)
_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _iso_or_none(raw: Optional[str]) -> Optional[str]:
    """Accept the header timestamp only if it is a real ISO 8601 instant.

    Contract: every stored time carries a timezone, so a naive source timestamp
    is read as local time.
    """
    if not raw:
        return None
    match = _STAMP_RE.search(raw)
    if not match:
        return None
    try:
        moment = datetime.fromisoformat(match.group(0))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


class WorkBuddyAdapter(Adapter):
    name = "workbuddy"
    source_kind = "auto_memory"

    def __init__(self, home: Optional[Path] = None):
        if home is not None:
            self.home = Path(home)
        else:
            override = os.environ.get("WORKBUDDY_HOME")
            self.home = Path(override) if override else Path.home() / ".workbuddy"

    # ------------------------------------------------------------ discover

    def discover(self) -> List[SourcePath]:
        memory_dir = self.home / "memory"
        if not memory_dir.is_dir():
            return []
        sources: List[SourcePath] = []
        for path in sorted(memory_dir.glob("*_memory.md")):
            if not path.is_file():
                continue
            sources.append(
                SourcePath(
                    path=path,
                    adapter=self.name,
                    source_kind=self.source_kind,
                    layer="",
                    precedence=0,
                    confidence="high",
                    note="single-file user memory profile",
                )
            )
        return sources

    # ---------------------------------------------------------------- read

    def read(self, source: SourcePath) -> List[MemoryEntry]:
        text = Path(source.path).read_text(encoding="utf-8", errors="replace")
        meta, rest = strip_quoted_metadata(text)
        created_at = _iso_or_none(meta.get("last updated"))
        file_hash = source.content_hash()

        entries: List[MemoryEntry] = []
        for section, body in bullets("\n".join(rest)):
            tags = [self.name]
            slug = _slug(section)
            if slug:
                tags.append(slug)
            entries.append(
                MemoryEntry.create(
                    # A "User Memory Profile" is a profile: ADR-003 routes that to
                    # human review, which is exactly right for an untrusted import.
                    category="profile",
                    body=body + "\n",
                    tags=tags,
                    source_adapter=self.name,
                    source_path=Path(source.path).as_posix(),
                    source_kind=self.source_kind,
                    source_hash=file_hash,
                    created_at=created_at,
                    precedence=source.precedence,
                    layer=source.layer,
                )
            )
        return entries
