"""Codex adapter: the ``AGENTS.md`` instruction chain (P2-04).

Verified on a real install (codex-cli 0.147.0, ``docs/04-upstream-formats.md`` §2.1):
a root ``AGENTS.md`` and a nested ``AGENTS.override.md`` both load, while the
``AGENTS.md`` sitting next to the override does **not** -- one file per directory,
override wins, no merging. That is why shadowed files are never discovered here:
importing them would invent a rule the agent never saw.

Only the instruction layer is in scope. Codex's *memory* lives in
``~/.codex/memories_1.sqlite`` and ``sessions/*.jsonl``, which is a separate ticket.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import List, Optional, Tuple

from .. import boundary, managed
from ..models import MemoryEntry
from .base import (
    Adapter,
    SourcePath,
    drop_heading_only,
    sections,
    slug,
    split_oversized,
    text_hash,
)

#: Same-directory precedence order. ``AGENTS.override.md`` shadows ``AGENTS.md``.
PRIMARY_NAMES = ("AGENTS.override.md", "AGENTS.md")

#: Codex's default cap on the *combined* instruction text.
DEFAULT_MAX_BYTES = 32 * 1024


def _looks_like_binary(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return True


class CodexAdapter(Adapter):
    name = "codex"
    source_kind = "instruction"

    def __init__(
        self,
        home: Optional[Path] = None,
        project_root: Optional[Path] = None,
        cwd: Optional[Path] = None,
    ):
        if home is not None:
            self.home = Path(home)
        else:
            override = os.environ.get("CODEX_HOME")
            self.home = Path(override) if override else Path.home() / ".codex"

        if project_root is not None:
            self.project_root = Path(project_root)
        else:
            override = os.environ.get("CODEX_PROJECT_ROOT")
            self.project_root = Path(override) if override else Path.cwd()

        self.cwd = Path(cwd) if cwd is not None else self.project_root

    # ------------------------------------------------------------- config

    def _config(self) -> Tuple[int, Tuple[str, ...]]:
        """``(project_doc_max_bytes, fallback_filenames)`` from ``config.toml``."""
        path = self.home / "config.toml"
        if not path.is_file():
            return DEFAULT_MAX_BYTES, ()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
            return DEFAULT_MAX_BYTES, ()
        limit = data.get("project_doc_max_bytes", DEFAULT_MAX_BYTES)
        if not isinstance(limit, int) or limit <= 0:
            limit = DEFAULT_MAX_BYTES
        fallback = data.get("project_doc_fallback_filenames", ())
        if not isinstance(fallback, list) or not all(isinstance(x, str) for x in fallback):
            fallback = ()
        return limit, tuple(fallback)

    # ------------------------------------------------------------ discover

    def _pick(self, directory: Path, fallback: Tuple[str, ...]) -> Optional[Path]:
        """One file per directory, in Codex's own order."""
        for name in (*PRIMARY_NAMES, *fallback):
            candidate = directory / name
            if candidate.is_file() and not _looks_like_binary(candidate):
                return candidate
        return None

    def _chain(self) -> List[Tuple[Path, str, int]]:
        """Directories Codex walks: global home, then project root down to cwd."""
        chain: List[Tuple[Path, str, int]] = []
        if (self.home / "AGENTS.override.md").is_file() or (
            self.home / "AGENTS.md"
        ).is_file():
            chain.append((self.home, "global", 0))

        try:
            relative = self.cwd.resolve().relative_to(self.project_root.resolve())
            parts = relative.parts
        except ValueError:
            parts = ()
        directories = [self.project_root]
        for part in parts:
            directories.append(directories[-1] / part)
        for index, directory in enumerate(directories):
            layer = "project" if index == 0 else "nested"
            chain.append((directory, layer, index + 1))
        return chain

    def discover(self) -> List[SourcePath]:
        limit, fallback = self._config()
        sources: List[SourcePath] = []
        total = 0
        for directory, layer, precedence in self._chain():
            path = self._pick(directory, fallback)
            if path is None:
                continue
            if boundary.is_inside_code_repo(path):
                print(
                    f"skipped {path}: it is the MemoryHub engineering contract, "
                    "not a memory of the user",
                    file=sys.stderr,
                )
                continue
            size = path.stat().st_size
            if total + size > limit:
                # Codex stops adding files once the combined text hits the cap,
                # so importing the rest would claim instructions it never reads.
                print(
                    f"warning: {path} left out: combined instruction text would exceed "
                    f"{limit} bytes (project_doc_max_bytes)",
                    file=sys.stderr,
                )
                break
            total += size
            sources.append(
                SourcePath(
                    path=path,
                    adapter=self.name,
                    source_kind="instruction",
                    layer=layer,
                    precedence=precedence,
                    confidence="high",
                )
            )
        return sources

    # ---------------------------------------------------------------- read

    def read(self, source: SourcePath) -> List[MemoryEntry]:
        text = managed.strip_block(
            Path(source.path).read_text(encoding="utf-8", errors="replace")
        )
        file_hash = text_hash(text)
        entries: List[MemoryEntry] = []
        for heading, body in drop_heading_only(split_oversized(sections(text))):
            tags = [self.name]
            heading_slug = slug(heading)
            if heading_slug:
                tags.append(heading_slug)
            entries.append(
                MemoryEntry.create(
                    # A standing rule for the agent -> ADR-003 sends it to review.
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
