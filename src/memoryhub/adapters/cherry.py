"""Cherry Studio adapter: ``Data/Agents/<agent>/`` (user-created agents only).

Format observed on a real install (``docs/04-upstream-formats.md`` §8.5).

**A trap worth naming**: this directory does hold ``SOUL.md`` / ``USER.md`` /
``FACT.md``, which looks exactly like WorkBuddy's persona directory -- but the
owner is different. ``SOUL.md`` is the *assistant's* personality and tone
(``# Personality`` / ``# Tone``, e.g. "mirror the user's terminology"), i.e. an
instruction for the agent, not a fact about the user. Importing it as
``category=profile`` would violate the rule that instructions are not memories.

So:

- ``SOUL.md`` is **skipped**. It describes how an assistant should behave.
- ``USER.md`` is the agent's accumulated model of *this user* -> ``auto_memory``,
  ``category=profile`` (ADR-003 sends it to review).
- ``memory/FACT.md`` is long-term knowledge -> ``auto_memory``, ``category=knowledge``.
- Built-in agents (``system``, ``cherry-support``) ship template content, not the
  user's own memory, so they are skipped by default. On a fresh install this
  adapter therefore discovers *nothing*, which is the honest answer.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from ..models import MemoryEntry
from .base import Adapter, SourcePath
from .persona import build_entries

#: Shipped with the product; their files are scaffolding, not user memory.
BUILTIN_AGENTS = ("system", "cherry-support", "cherry-help", "default")

#: Persona-shaped files that describe the *assistant*, never the user.
SKIPPED_FILES = ("SOUL.md", "IDENTITY.md")


def _default_home() -> Path:
    override = os.environ.get("CHERRY_STUDIO_HOME")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "CherryStudio"
    return Path.home() / ".config" / "CherryStudio"


#: filename -> (source_kind, category)
FILES = {
    "USER.md": ("auto_memory", "profile"),
    "FACT.md": ("auto_memory", "knowledge"),
}


class CherryStudioAdapter(Adapter):
    name = "cherry"
    source_kind = "auto_memory"

    def __init__(self, home: Optional[Path] = None, include_builtin: bool = False):
        self.home = Path(home) if home is not None else _default_home()
        self.include_builtin = include_builtin

    # ------------------------------------------------------------ discover

    def discover(self) -> List[SourcePath]:
        agents_dir = self.home / "Data" / "Agents"
        if not agents_dir.is_dir():
            return []
        sources: List[SourcePath] = []
        for agent in sorted(agents_dir.iterdir()):
            if not agent.is_dir() or agent.name.startswith("."):
                continue
            if agent.name in BUILTIN_AGENTS and not self.include_builtin:
                continue
            for name, (kind, _category) in FILES.items():
                path = agent / name if name == "USER.md" else agent / "memory" / name
                if path.is_file():
                    sources.append(
                        SourcePath(
                            path=path,
                            adapter=self.name,
                            source_kind=kind,
                            confidence="high" if self.include_builtin else "medium",
                            note=f"agent={agent.name}",
                        )
                    )
        return sources

    # ---------------------------------------------------------------- read

    def read(self, source: SourcePath) -> List[MemoryEntry]:
        try:
            text = Path(source.path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - depends on the install
            print(f"warning: cannot read {source.path}: {exc}", file=sys.stderr)
            return []
        _kind, category = FILES.get(source.path.name, ("auto_memory", "knowledge"))
        extra = []
        if source.note.startswith("agent="):
            extra.append(f"agent-{source.note.split('=', 1)[1]}")
        return build_entries(
            adapter=self.name,
            source=source,
            text=text,
            file_hash=source.content_hash(),
            category=category,
            extra_tags=extra,
        )
