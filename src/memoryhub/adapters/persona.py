"""Shared builder for "persona directories" (P2-03c).

WorkBuddy (``user-*-personal/``), Cherry Studio (``Agents/<agent>/``) and Claude's
auto memory have converged on the same shape without anybody declaring a standard:

    persona files (IDENTITY / SOUL / USER) + a memory file (MEMORY / FACT) + an index

So the parsing lives here and each tool only has to describe *where* its files are.
Splitting is data-driven rather than per-tool: a file whose top level is bullets
becomes one memory per bullet; otherwise it is split at level 1-2 headings.
Cherry's ``FACT.md`` is prose, WorkBuddy's ``MEMORY.md`` is bullets -- same builder,
no per-tool parser, so a new tool cannot drift.

Everything imported here is filed as ``category="profile"``, which ADR-003 routes to
human review. A tool's belief about the user is exactly the kind of thing a human
should confirm before it becomes truth in the shared vault.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from ..models import MemoryEntry
from .base import SourcePath, bullets, sections, slug

#: Filenames that make up a persona directory, in display order.
PERSONA_FILENAMES = ("IDENTITY.md", "SOUL.md", "USER.md", "MEMORY.md", "FACT.md")


def has_top_level_bullets(text: str) -> bool:
    for line in text.splitlines():
        if line[:1] in ("-", "*") and line[1:2] == " ":
            return True
    return False


def chunks(text: str) -> List[Tuple[str, str]]:
    """Split a persona file into ``(heading, body)`` pairs, format-driven."""
    if has_top_level_bullets(text):
        return bullets(text)
    return sections(text)


def build_entries(
    *,
    adapter: str,
    source: SourcePath,
    text: str,
    file_hash: str,
    category: str = "profile",
    extra_tags: Iterable[str] = (),
) -> List[MemoryEntry]:
    tags_head = [adapter, *extra_tags]
    entries: List[MemoryEntry] = []
    for heading, body in chunks(text):
        tags = list(tags_head)
        heading_slug = slug(heading)
        if heading_slug:
            tags.append(heading_slug)
        entries.append(
            MemoryEntry.create(
                category=category,
                body=body + "\n",
                tags=tags,
                source_adapter=adapter,
                source_path=source.path.as_posix(),
                source_kind=source.source_kind,
                source_hash=file_hash,
                precedence=source.precedence,
                layer=source.layer,
            )
        )
    return entries
