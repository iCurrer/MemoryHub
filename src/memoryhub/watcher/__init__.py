"""File watching for memory sources (P2-06).

Design note: this is a polling watcher, not a ``watchdog`` observer. Memory files
change on the order of minutes, so 2 seconds of polling is plenty, and it keeps
the debounce / dedupe rules testable without mocking a platform observer.
See :mod:`memoryhub.watcher.core` for the reasoning.
"""

from __future__ import annotations

from .core import Debouncer, HashTracker, MtimeTracker, exclude_roots, is_excluded, sha256_of_file
from .poll import PollResult, fingerprints, import_entries, poll_once, watch

__all__ = [
    "Debouncer",
    "HashTracker",
    "MtimeTracker",
    "PollResult",
    "exclude_roots",
    "fingerprints",
    "import_entries",
    "is_excluded",
    "poll_once",
    "sha256_of_file",
    "watch",
]
