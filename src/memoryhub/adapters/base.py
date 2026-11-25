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
import re
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

    def label(self) -> str:
        """Short, disambiguated name for CLI output (``CLAUDE.md [project]``)."""
        return f"{Path(self.path).name} [{self.layer}]" if self.layer else Path(self.path).name


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


def sections(text: str) -> List[Tuple[str, str]]:
    """Split markdown into ``(heading, body)`` pairs at level 1-2 headings.

    Headings inside fenced code blocks are ignored: instruction files routinely
    contain ```bash blocks whose `#` lines would otherwise shred the document.
    The heading is kept as the first line of the body so a memory reads as
    "## Non-goals / 不做 X" instead of a context-free fragment.
    """
    found: List[Tuple[str, str]] = []
    heading = ""
    buffer: List[str] = []
    in_fence = False

    def flush() -> None:
        nonlocal buffer
        chunk = "\n".join(buffer).strip()
        if chunk:
            found.append((heading, chunk))
        buffer = []

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if buffer or heading:
                buffer.append(line)
            continue
        if not in_fence and line.startswith("#") and len(line) - len(line.lstrip("#")) <= 2:
            flush()
            heading = line.lstrip("#").strip()
            buffer = [line]
            continue
        if buffer or heading:
            buffer.append(line)
    flush()
    return found


# ------------------------------------------------------------------ helpers


def _heading_level(line: str) -> int:
    match = re.match(r"^(#{1,6})\s", line)
    return len(match.group(1)) if match else 0


def _has_prose(part: str) -> bool:
    """True when *part* carries text besides its headings."""
    body = [line for line in part.splitlines() if not line.lstrip().startswith("#")]
    return sum(len(line.strip()) for line in body) >= 2


def _split_at_subheadings(body: str) -> List[str]:
    """Split a body at headings deeper than its own first heading."""
    lines = body.splitlines()
    base = 0
    for line in lines:
        level = _heading_level(line)
        if level:
            base = level
            break
    if not base:
        return [body]

    parts: List[str] = []
    current: List[str] = []
    for line in lines:
        level = _heading_level(line)
        has_content = any(part.strip() for part in current[1:])
        if level > base and current and has_content:
            parts.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("\n".join(current).strip())

    # A parent heading with nothing under it (``## 七、其他重要项目`` followed
    # straight by ``### Pocket CCD``) would otherwise become an entry with no
    # memory in it. Carry it down as context for the first child instead: the
    # child reads as "## 七、其他重要项目 / ### Pocket CCD / ..." and nothing is lost.
    merged: List[str] = []
    carry = ""
    for part in parts:
        if not part:
            continue
        if not _has_prose(part):
            carry = f"{carry}\n{part}".strip() if carry else part
            continue
        merged.append(f"{carry}\n{part}".strip() if carry else part)
        carry = ""
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]}\n{carry}".strip()
        else:
            merged.append(carry)
    return merged


def _split_at_blocks(body: str, max_chars: int) -> List[str]:
    """Group blank-line-separated blocks into chunks under *max_chars*."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    if len(blocks) <= 1:
        return [body]
    chunks: List[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _split_at_sentences(body: str, max_chars: int) -> List[str]:
    """Last resort: a single block with no blank lines still has to be cut."""
    pieces = re.split(r"(?<=[。！？!?；;])\s*", body)
    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        candidate = current + piece
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [body]


#: Target upper bound for one memory. Chosen from the real vault: the median
#: entry is ~90 characters, and the worst offenders were 1000+, which means one
#: "memory" held twenty unrelated facts and review became meaningless.
DEFAULT_MAX_ENTRY_CHARS = 400


def drop_heading_only(
    pairs: List[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """Remove entries that are a bare heading.

    A section like ``## 三、沟通与协作偏好（重要）`` whose content all lives in
    deeper headings produces an entry with a title and no memory. Importing it
    gives review a queue of empty rows, which is how a review queue gets
    abandoned. The heading still reaches the vault as a tag on its children.
    """
    return [(heading, body) for heading, body in pairs if _has_prose(body)]


def split_oversized(
    pairs: List[Tuple[str, str]], max_chars: int = DEFAULT_MAX_ENTRY_CHARS
) -> List[Tuple[str, str]]:
    """Break entries that exceed *max_chars* into reviewable pieces.

    Three passes, cheapest first: subheadings, then blank-line blocks, then
    sentences. The parent heading is reused for every piece so tags stay stable
    and a fragment can always be traced back to the section it came from.

    A body already under the limit is returned untouched — this must not rewrite
    content that was fine, because every split changes an entry id.
    """
    result: List[Tuple[str, str]] = []
    for heading, body in pairs:
        if len(body) <= max_chars:
            result.append((heading, body))
            continue

        pieces = _split_at_subheadings(body)
        if len(pieces) <= 1:
            pieces = _split_at_blocks(body, max_chars)
        expanded: List[str] = []
        for piece in pieces:
            if len(piece) > max_chars:
                expanded.extend(_split_at_blocks(piece, max_chars))
            else:
                expanded.append(piece)
        final: List[str] = []
        for piece in expanded:
            if len(piece) > max_chars:
                final.extend(_split_at_sentences(piece, max_chars))
            else:
                final.append(piece)
        result.extend((heading, piece) for piece in final if piece.strip())
    return result



#: Persona filenames that belong to the **assistant**, not to the user.
#:
#: Learned the hard way, twice: Cherry Studio's ``SOUL.md`` ("# Personality" /
#: "# Tone") and WorkBuddy's ``IDENTITY.md`` ("Who Am I?") / ``SOUL.md``
#: ("Who You Are") are the assistant describing itself. Importing them as
#: user profile pollutes the vault with rules like "mirror the user's terminology".
#: ``USER.md`` / ``MEMORY.md`` / ``FACT.md`` are the opposite: they are what the
#: assistant believes *about the user*, which is memory.
ASSISTANT_PERSONA_FILES = ("IDENTITY.md", "SOUL.md")


def slug(text: str) -> str:
    """Lowercase tag slug that keeps CJK (``## 编码约定`` must not become "")."""
    return re.sub(r"[^\w]+", "-", text.lower(), flags=re.UNICODE).strip("-")


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
