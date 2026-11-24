"""Git-backed history and backup (P4-03, P5-04).

Kept together because both answer the same question from the same source: what did
the vault look like before, and can we get it back.
"""

from __future__ import annotations

from .history import (
    Change,
    Commit,
    HistoryUnavailable,
    collect,
    is_repo,
    since_to_git,
    summarise,
)

__all__ = [
    "Change",
    "Commit",
    "HistoryUnavailable",
    "collect",
    "is_repo",
    "since_to_git",
    "summarise",
]
