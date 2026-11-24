"""One polling pass over every adapter (P2-06).

Rediscovering on each tick is deliberate: adapters enumerate a handful of files,
and naive re-discovery cannot drift out of sync with reality the way an
incremental path set can. What we avoid is re-importing *content* -- the
:class:`~memoryhub.watcher.core.HashTracker` decides that, and it re-checks at
release time so a file edited *during* the debounce window is not missed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..adapters import ADAPTERS, build
from ..models import MemoryEntry
from ..storage.markdown_store import VaultStore
from .core import Debouncer, HashTracker, MtimeTracker, exclude_roots, is_excluded


@dataclass
class PollResult:
    checked: int = 0
    imported: int = 0
    duplicate: int = 0
    failed: int = 0
    imported_ids: List[str] = field(default_factory=list)


def fingerprints(store: VaultStore) -> set:
    """Everything already in the vault, so a restart cannot double-import."""
    entries, _errors = store.scan()
    return {(entry.source_adapter, entry.source_path, entry.body) for entry in entries}


def import_entries(
    store: VaultStore,
    entries: Sequence[MemoryEntry],
    seen: set,
    *,
    write: bool = True,
) -> Tuple[int, int]:
    """Write the fresh ones. Shared by ``scan`` and the watcher so they cannot drift.

    ``write=False`` gives ``scan --dry-run`` the same de-duplication without touching
    the vault, so a preview cannot disagree with the real thing.
    """
    fresh = duplicate = 0
    for entry in entries:
        key = (entry.source_adapter, entry.source_path, entry.body)
        if key in seen:
            duplicate += 1
            continue
        if write:
            store.create(entry)
        seen.add(key)
        fresh += 1
    return fresh, duplicate


def poll_once(
    store: VaultStore,
    *,
    tracker: HashTracker,
    debouncer: Optional[Debouncer] = None,
    mtimes: Optional[MtimeTracker] = None,
    now: Optional[float] = None,
    adapter_names: Optional[Sequence[str]] = None,
) -> PollResult:
    """Discover every source; import only files whose content actually changed."""
    moment = time.monotonic() if now is None else now
    result = PollResult()
    excluded = exclude_roots(store.root)
    seen = fingerprints(store)
    names = list(adapter_names or sorted(ADAPTERS))

    candidates = []
    for name in names:
        adapter = build(name)
        try:
            discovered = adapter.discover()
        except OSError:
            result.failed += 1
            continue
        for source in discovered:
            if is_excluded(source.path, excluded):
                continue
            candidates.append((adapter, source))

    if debouncer is None:
        ready = [(source.path, (adapter, source)) for adapter, source in candidates]
    else:
        stamps = mtimes if mtimes is not None else MtimeTracker()
        for adapter, source in candidates:
            # Only a real mtime change restarts the quiet timer; a poll that sees
            # nothing new must not push the release further out.
            if stamps.changed(source.path):
                debouncer.observe(source.path, moment, (adapter, source))
        ready = debouncer.ready(moment)

    for _path, payload in ready:
        adapter, source = payload
        result.checked += 1
        # Re-check here: the file may have been written again while it was quieting.
        if not tracker.changed(source.path):
            continue
        try:
            entries = adapter.read(source)
        except (OSError, UnicodeDecodeError):
            result.failed += 1
            continue
        fresh, duplicate = import_entries(store, entries, seen)
        result.imported += fresh
        result.duplicate += duplicate
        result.imported_ids.extend(entry.id for entry in entries)
    return result


def watch(
    store: VaultStore,
    *,
    interval: float = 2.0,
    quiet_seconds: float = 3.0,
    iterations: Optional[int] = None,
    on_change=None,
) -> int:
    """Poll forever (or ``iterations`` times). Returns the number of imports."""
    tracker = HashTracker()
    mtimes = MtimeTracker()
    debouncer = Debouncer(quiet_seconds=quiet_seconds)
    total = 0
    ticks = 0
    while iterations is None or ticks < iterations:
        result = poll_once(store, tracker=tracker, debouncer=debouncer, mtimes=mtimes)
        total += result.imported
        if result.imported and on_change is not None:
            on_change(result)
        ticks += 1
        if iterations is not None and ticks >= iterations:
            break
        time.sleep(interval)
    return total
