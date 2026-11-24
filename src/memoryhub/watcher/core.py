"""Decide *when* a changed file has settled enough to import (P2-06).

Everything here is pure logic with an injected clock, so the three behaviours the
ticket actually cares about can be tested without waiting on real time:

- rapid successive writes to one file collapse into a single import
- a rewrite with identical content produces nothing
- our own ``exports/`` output never re-enters the vault (ADR-002's only loop risk)

There is deliberately **no third-party file-watching dependency**. `watchdog`
would buy lower latency, but memory files change on the order of minutes, and a
2-second poll keeps the whole feature testable on any machine with no observer
implementation to mock. Adding `watchdog` later only replaces the *source* of
events; the rules below stay put.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def is_excluded(path: Path, excluded_roots: Iterable[Path]) -> bool:
    """True when the path lives under a directory we must never re-import.

    ``exports/`` is the important one: it is MemoryHub's own output, so reading it
    back would create the sync loop ADR-002 was written to avoid.
    """
    resolved = Path(path).resolve()
    for root in excluded_roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


@dataclass
class Debouncer:
    """Coalesce repeated changes to one key and release it once it is quiet.

    ``payload`` is whatever the caller wants back at release time (the watcher
    parks a ``SourcePath`` there). Keeping it generic stops this module from
    depending on the adapter layer.
    """

    quiet_seconds: float = 3.0
    _pending: Dict[Path, Tuple[float, object]] = field(default_factory=dict)

    def observe(self, key: Path, now: float, payload: object = None) -> None:
        """Record a change. Repeated calls for the same key just reset the timer."""
        self._pending[Path(key)] = (now, payload)

    def ready(self, now: float) -> List[Tuple[Path, object]]:
        """Entries quiet for at least ``quiet_seconds``, oldest first."""
        due = [
            (key, changed_at, payload)
            for key, (changed_at, payload) in self._pending.items()
            if now - changed_at >= self.quiet_seconds
        ]
        for key, _changed_at, _payload in due:
            del self._pending[key]
        due.sort(key=lambda item: (item[1], str(item[0])))
        return [(key, payload) for key, _changed_at, payload in due]

    def pending(self) -> int:
        return len(self._pending)


class MtimeTracker:
    """Cheap "did this file change at all" check, used to drive the debouncer.

    Without it every poll would re-observe every file and reset the quiet timer
    forever, so nothing would ever be released. Hashing stays a separate concern
    (:class:`HashTracker`) because it is the expensive one.
    """

    def __init__(self, initial: Optional[Dict[Path, Tuple[int, int]]] = None):
        self._stamps: Dict[Path, Tuple[int, int]] = dict(initial or {})

    def changed(self, path: Path) -> bool:
        path = Path(path)
        try:
            stat = path.stat()
        except OSError:
            return False
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._stamps.get(path) == stamp:
            return False
        self._stamps[path] = stamp
        return True


class HashTracker:
    """Remember the last imported content hash per path."""

    def __init__(self, initial: Optional[Dict[Path, str]] = None):
        self._hashes: Dict[Path, str] = dict(initial or {})

    def changed(self, path: Path) -> bool:
        """True when the file's content differs from what we last imported."""
        path = Path(path)
        try:
            current = sha256_of_file(path)
        except OSError:
            return False
        if self._hashes.get(path) == current:
            return False
        self._hashes[path] = current
        return True

    def forget(self, path: Path) -> None:
        self._hashes.pop(Path(path), None)

    def snapshot(self) -> Dict[Path, str]:
        return dict(self._hashes)


def exclude_roots(vault_root: Path) -> Set[Path]:
    """Directories inside a vault that must never be re-imported."""
    return {
        Path(vault_root) / "exports",
        Path(vault_root) / "logs",
    }
