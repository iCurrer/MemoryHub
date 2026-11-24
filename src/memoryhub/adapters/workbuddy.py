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
from .base import (
    ASSISTANT_PERSONA_FILES,
    Adapter,
    SourcePath,
    bullets,
    sections,
    strip_quoted_metadata,
)

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
        sources: List[SourcePath] = []
        sources.extend(self._profile_memory())
        sources.extend(self._persona_files())
        return sources

    def _profile_memory(self) -> List[SourcePath]:
        """``memory/<uuid>_memory.md``: one bullet per memory, blockquote header."""
        memory_dir = self.home / "memory"
        if not memory_dir.is_dir():
            return []
        found: List[SourcePath] = []
        for path in sorted(memory_dir.glob("*_memory.md")):
            if not path.is_file():
                continue
            found.append(
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
        return found

    def _persona_files(self) -> List[SourcePath]:
        """``user-<uuid>-personal/``: USER + MEMORY only.

        ``IDENTITY.md`` / ``SOUL.md`` sit in the same directory but describe the
        assistant ("Who Am I?", "Core Truths", "Boundaries"), so they are skipped --
        see :data:`ASSISTANT_PERSONA_FILES`. ``MEMORY.md`` here is a bullet list of
        facts about the person, not a link index.
        """
        found: List[SourcePath] = []
        for persona_dir in sorted(self.home.glob("user-*-personal")):
            if not persona_dir.is_dir():
                continue
            # IDENTITY / SOUL describe the assistant, not the user: see
            # ASSISTANT_PERSONA_FILES for why this is not a judgement call.
            for name in ("USER.md",):
                path = persona_dir / name
                if path.is_file():
                    found.append(
                        SourcePath(
                            path=path,
                            adapter=self.name,
                            source_kind="profile",
                            layer="",
                            precedence=0,
                            confidence="high",
                            note=f"persona:{name}",
                        )
                    )
            index = persona_dir / "MEMORY.md"
            if index.is_file():
                found.append(
                    SourcePath(
                        path=index,
                        adapter=self.name,
                        source_kind="auto_memory",
                        layer="",
                        precedence=0,
                        confidence="high",
                        note="persona-index",
                    )
                )
        return found

    # ---------------------------------------------------------------- read

    def read(self, source: SourcePath) -> List[MemoryEntry]:
        text = Path(source.path).read_text(encoding="utf-8", errors="replace")
        if source.note.startswith("persona:") or source.note == "persona-index":
            return self._persona_entries(source, text)
        return self._bullets_entries(source, text)

    # ------------------------------------------------------------ builders

    def _persona_entries(self, source: SourcePath, text: str) -> List[MemoryEntry]:
        meta, _rest = strip_quoted_metadata(text)
        created_at = _iso_or_none(meta.get("last updated"))
        file_hash = source.content_hash()
        if source.note == "persona-index":
            pairs = [(section, body) for section, body in bullets(text)]
        else:
            pairs = sections(text)
        entries: List[MemoryEntry] = []
        for heading, body in pairs:
            tags = [self.name]
            slug = _slug(heading)
            if slug:
                tags.append(slug)
            entries.append(
                MemoryEntry.create(
                    # Persona facts route to human review via ADR-003.
                    category="profile",
                    body=body + "\n",
                    tags=tags,
                    source_adapter=self.name,
                    source_path=source.path.as_posix(),
                    source_kind=source.source_kind,
                    source_hash=file_hash,
                    created_at=created_at,
                    precedence=source.precedence,
                    layer=source.layer,
                )
            )
        return entries

    def _bullets_entries(self, source: SourcePath, text: str) -> List[MemoryEntry]:
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
