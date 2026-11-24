"""Adapter registry.

Adding a source means: implement :class:`~memoryhub.adapters.base.Adapter`, add it
here, and record its real paths and formats in ``docs/04-upstream-formats.md``
first. An adapter written against assumed formats is the one thing this project
cannot afford -- see ADR-011.
"""

from __future__ import annotations

from typing import Dict, List, Type

from .base import Adapter, SourcePath, bullets, sections, slug, strip_quoted_metadata
from .cherry import CherryStudioAdapter
from .claude import ClaudeAdapter
from .workbuddy import WorkBuddyAdapter

ADAPTERS: Dict[str, Type[Adapter]] = {
    WorkBuddyAdapter.name: WorkBuddyAdapter,
    ClaudeAdapter.name: ClaudeAdapter,
    CherryStudioAdapter.name: CherryStudioAdapter,
}


def names() -> List[str]:
    return sorted(ADAPTERS)


def build(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown adapter {name!r}; known: {', '.join(names())}")
    return ADAPTERS[name]()


__all__ = [
    "Adapter",
    "SourcePath",
    "ADAPTERS",
    "build",
    "names",
    "bullets",
    "sections",
    "slug",
    "strip_quoted_metadata",
    "WorkBuddyAdapter",
    "ClaudeAdapter",
    "CherryStudioAdapter",
]
