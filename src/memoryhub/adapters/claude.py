"""Claude Code adapter: ``CLAUDE.md`` chain + per-project auto memory.

Format source: ``docs/04-upstream-formats.md`` §3 (official docs).

**Not verified against a real installation.** Claude Code is not installed on the
machine this was written on, so every path and every field below comes from the
official documentation rather than from a live sample. The adapter therefore
degrades to "no sources" instead of guessing, and the tests build fixtures from
the documented shape. Re-check §9 of the format baseline before trusting it on a
new version.

Two products, deliberately kept apart:

- instruction files (``CLAUDE.md`` / ``CLAUDE.local.md`` / ``.claude/rules``) are
  ``source_kind="instruction"`` -- they are rules for the AI, not facts about the
  user (AGENTS.md 明确禁止把指令当人格记忆)
- auto memory files (``~/.claude/projects/<project>/memory/*.md``) are
  ``source_kind="auto_memory"`` and carry their own ``type`` / ``modified``
  frontmatter, which maps onto ``category`` / ``created_at``
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from .. import frontmatter
from ..models import MemoryEntry
from .base import Adapter, SourcePath, sections

#: Claude Code loads a CLAUDE.md of 4 MiB or smaller and skips a larger one
#: entirely, so importing more than that would be a lie.
MAX_INSTRUCTION_BYTES = 4 * 1024 * 1024

#: Claude's auto memory frontmatter ``type`` -> MemoryHub ``category``.
#: ``user`` becomes ``profile`` (which ADR-003 routes to human review).
TYPE_TO_CATEGORY = {
    "user": "profile",
    "feedback": "preference",
    "project": "project",
    "reference": "knowledge",
}

_SLUG_RE = re.compile(r"[^\w]+", re.UNICODE)


def _slug(text: str) -> str:
    """Lowercase slug that keeps CJK: ``## 编码约定`` must not become ""."""
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _looks_like_frontmatter(text: str) -> bool:
    return text.startswith("---\n") or text.startswith("---\r\n")


class ClaudeAdapter(Adapter):
    name = "claude"
    source_kind = "instruction"

    def __init__(
        self,
        home: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ):
        if home is not None:
            self.home = Path(home)
        else:
            override = os.environ.get("CLAUDE_HOME")
            self.home = Path(override) if override else Path.home() / ".claude"

        if project_root is not None:
            self.project_root = Path(project_root)
        else:
            # Project instructions are cwd-relative in Claude Code, so scanning
            # from a repository imports that repository's CLAUDE.md. The env
            # override exists so `scan` can be aimed somewhere else on purpose.
            project_override = os.environ.get("CLAUDE_PROJECT_ROOT")
            self.project_root = (
                Path(project_override) if project_override else Path.cwd()
            )

    # ------------------------------------------------------------ discover

    def _instruction_source(
        self, path: Path, layer: str, precedence: int
    ) -> Optional[SourcePath]:
        if not path.is_file():
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_INSTRUCTION_BYTES:
            print(
                f"warning: {path} is over the 4 MiB load limit and was skipped "
                "(Claude Code skips it too)",
                file=sys.stderr,
            )
            return None
        return SourcePath(
            path=path,
            adapter=self.name,
            source_kind="instruction",
            layer=layer,
            precedence=precedence,
            confidence="high",
        )

    def discover(self) -> List[SourcePath]:
        sources: List[SourcePath] = []

        # 1. user scope
        user_md = self._instruction_source(self.home / "CLAUDE.md", "global", 0)
        if user_md:
            sources.append(user_md)
        for rule in sorted((self.home / "rules").rglob("*.md")):
            found = self._instruction_source(rule, "global", 1)
            if found:
                sources.append(found)

        # 2. project scope: ./CLAUDE.md wins over ./.claude/CLAUDE.md
        project_md = self._instruction_source(self.project_root / "CLAUDE.md", "project", 10)
        if project_md is None:
            project_md = self._instruction_source(
                self.project_root / ".claude" / "CLAUDE.md", "project", 10
            )
        if project_md:
            sources.append(project_md)
        for rule in sorted((self.project_root / ".claude" / "rules").rglob("*.md")):
            found = self._instruction_source(rule, "project", 11)
            if found:
                sources.append(found)

        # 3. local scope loads after CLAUDE.md in the same directory
        local_md = self._instruction_source(
            self.project_root / "CLAUDE.local.md", "local", 20
        )
        if local_md:
            sources.append(local_md)

        # 4. auto memory: one directory per project, MEMORY.md is an index
        for memory_dir in sorted((self.home / "projects").glob("*/memory")):
            if not memory_dir.is_dir():
                continue
            for path in sorted(memory_dir.glob("*.md")):
                if path.name == "MEMORY.md":
                    continue  # index of one-line pointers; the topic files hold the detail
                sources.append(
                    SourcePath(
                        path=path,
                        adapter=self.name,
                        source_kind="auto_memory",
                        layer="",
                        precedence=0,
                        confidence="high",
                        note=f"project={memory_dir.parent.name}",
                    )
                )
        return sources

    # ---------------------------------------------------------------- read

    def read(self, source: SourcePath) -> List[MemoryEntry]:
        path = Path(source.path)
        text = path.read_text(encoding="utf-8", errors="replace")
        file_hash = source.content_hash()
        if source.source_kind == "auto_memory":
            return [self._memory_entry(source, text, file_hash)]
        return self._instruction_entries(source, text, file_hash)

    # ------------------------------------------------------------ builders

    def _instruction_entries(
        self, source: SourcePath, text: str, file_hash: str
    ) -> List[MemoryEntry]:
        body_text = text
        if _looks_like_frontmatter(text):
            try:
                _meta, body_text = frontmatter.split(text)
            except frontmatter.FrontmatterError:
                body_text = text  # not frontmatter after all; keep the text intact

        entries: List[MemoryEntry] = []
        for heading, body in sections(body_text):
            tags = [self.name]
            slug = _slug(heading)
            if slug:
                tags.append(slug)
            entries.append(
                MemoryEntry.create(
                    # An instruction section is a standing decision about how the
                    # AI must behave, so it is filed as `decision` -- which
                    # ADR-003 routes to human review rather than auto-approval.
                    category="decision",
                    body=body + "\n",
                    tags=tags,
                    source_adapter=self.name,
                    source_path=source.path.as_posix(),
                    source_kind="instruction",
                    source_hash=file_hash,
                    precedence=source.precedence,
                    layer=source.layer,
                )
            )
        return entries

    def _memory_entry(
        self, source: SourcePath, text: str, file_hash: str
    ) -> MemoryEntry:
        meta = {}
        body_text = text
        if _looks_like_frontmatter(text):
            try:
                meta, body_text = frontmatter.split(text)
            except frontmatter.FrontmatterError:
                meta, body_text = {}, text

        kind = str(meta.get("type") or "").strip().lower()
        category = TYPE_TO_CATEGORY.get(kind, "knowledge")
        modified = meta.get("modified")
        created_at = modified if isinstance(modified, str) else None

        tags = [self.name]
        if kind:
            tags.append(kind)
        if source.note.startswith("project="):
            project_slug = _slug(source.note.split("=", 1)[1])[:40]
            if project_slug:
                tags.append(f"claude-project-{project_slug}")

        return MemoryEntry.create(
            category=category,
            body=body_text.strip() + "\n",
            tags=tags,
            source_adapter=self.name,
            source_path=source.path.as_posix(),
            source_kind="auto_memory",
            source_hash=file_hash,
            created_at=created_at,
            precedence=source.precedence,
            layer=source.layer,
        )
