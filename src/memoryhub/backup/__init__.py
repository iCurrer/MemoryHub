"""Git-backed history and backup (P4-03, P5-04).

Kept together because both answer the same question from the same source: what did
the vault look like before, and can we get it back.
"""

from __future__ import annotations

from .git import (
    GITIGNORE_LINES,
    GitUnavailable,
    commit,
    ensure_repo,
    has_changes,
    push,
    remotes,
    sensitive_entries,
    write_gitignore,
)
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
    "GITIGNORE_LINES",
    "GitUnavailable",
    "commit",
    "ensure_repo",
    "has_changes",
    "push",
    "remotes",
    "sensitive_entries",
    "write_gitignore",
    "Change",
    "Commit",
    "HistoryUnavailable",
    "collect",
    "is_repo",
    "since_to_git",
    "summarise",
]
