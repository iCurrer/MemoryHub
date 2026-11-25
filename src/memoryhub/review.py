"""One-entry-at-a-time interactive review (P4-05).

Why this exists: the vault had 53 proposals and zero approvals, so MCP answered
nothing. The bottleneck was never a missing capability -- it was that ``approve``
requires you to already know an id, which means reading the list, scrolling, and
copy-pasting a 32-character identifier 53 times. Every extra step is a reason to
do it later.

Scope is deliberately one screen, one decision. The queue is *re-derived* from
the vault on every run; only the ids you skipped are remembered. That asymmetry is
intentional: a stale state file must never be able to hide a proposal or resurrect
a deleted one, and anything derivable should not be stored twice (ADR-003's
argument for keeping git history as the change log applies the same way).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, TextIO

from . import config
from .models import CATEGORIES, MemoryEntry, ValidationError
from .storage.markdown_store import StaleEntryError, VaultStore

#: Keys the loop accepts. Typing one letter plus Enter is the whole protocol --
#: raw single-key mode needs termios on POSIX and msvcrt on Windows, and a
#: half-working version of that is worse than pressing Enter.
KEYS = ("a", "r", "c", "s", "q")

HELP_LINE = "a approve   r reject   c change category   s skip   q quit"

_RULE = "-" * 68


@dataclass
class Outcome:
    """What one review session did, so the caller can print or assert on it."""

    approved: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    recategorised: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def decided(self) -> int:
        return len(self.approved) + len(self.rejected)

    def summary(self) -> str:
        return (
            f"approved {len(self.approved)}, rejected {len(self.rejected)}, "
            f"recategorised {len(self.recategorised)}, skipped {len(self.skipped)}"
        )


class ReviewState:
    """Remembers only the ids you skipped, keyed by vault path.

    Lives next to the config file rather than inside the vault: the vault holds
    memories, and a cursor file would be the first non-memory object in it. It
    also has to survive ``git commit`` in the vault without showing up as a diff.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (config.home() / "review.json")
        self._data: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt cursor must not block reviewing; worst case you see the
            # skipped entries again, which is exactly the fallback behaviour.
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, list):
                    self._data[str(key)] = [str(item) for item in value]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    @staticmethod
    def _key(vault: Path) -> str:
        return Path(vault).resolve().as_posix()

    def skipped(self, vault: Path) -> List[str]:
        return list(self._data.get(self._key(vault), []))

    def mark_skipped(self, vault: Path, entry_id: str) -> None:
        key = self._key(vault)
        current = self._data.setdefault(key, [])
        if entry_id not in current:
            current.append(entry_id)
        self._flush()

    def clear_skipped(self, vault: Path, entry_id: str) -> None:
        """Forget a skip once the entry is decided, so the file cannot grow forever."""
        key = self._key(vault)
        current = self._data.get(key)
        if not current or entry_id not in current:
            return
        current.remove(entry_id)
        if not current:
            self._data.pop(key, None)
        self._flush()

    def reset(self, vault: Path) -> int:
        """Drop the whole skip list for one vault; returns how many were forgotten."""
        key = self._key(vault)
        dropped = len(self._data.pop(key, []))
        if dropped:
            self._flush()
        return dropped


def build_queue(
    entries: Iterable[MemoryEntry], skipped: Sequence[str]
) -> List[MemoryEntry]:
    """Pending entries first, then the ones skipped earlier.

    Ordering *within* each group is by ``created_at`` then id, so the queue is
    stable across runs -- a review that reshuffles every time is unusable when
    you resume after a break.
    """
    pending = [entry for entry in entries if entry.status == "pending"]
    skip_set = set(skipped)
    fresh = [entry for entry in pending if entry.id not in skip_set]
    deferred = [entry for entry in pending if entry.id in skip_set]

    def order(entry: MemoryEntry) -> tuple:
        return (entry.created_at or "", entry.id)

    return sorted(fresh, key=order) + sorted(deferred, key=order)


def render(entry: MemoryEntry, index: int, total: int, out: TextIO) -> None:
    """One entry, one screen. Body is printed in full -- review means reading it."""
    blocked = entry.trust == "blocked"
    print(_RULE, file=out)
    print(f"[ {index} / {total} ]  {entry.id}", file=out)
    print(
        f"category {entry.category:<10} trust {entry.trust:<7} "
        f"sensitivity {entry.sensitivity}",
        file=out,
    )
    print(
        f"source   {entry.source_adapter or '?'}  {entry.source_path or '(none)'}",
        file=out,
    )
    print(f"created  {entry.created_at or '?'}   confidence {entry.confidence}", file=out)
    if entry.tags:
        print(f"tags     {', '.join(entry.tags)}", file=out)
    print(_RULE, file=out)
    print(entry.body.rstrip(), file=out)
    print(_RULE, file=out)
    if blocked:
        print(
            "blocked by the sensitivity classifier -- changing the category will not\n"
            "release it. Only r (reject) or s (skip) apply here; approving needs a\n"
            "lower sensitivity first (`memoryhub sensitivity --help`).",
            file=out,
        )
    else:
        print(HELP_LINE, file=out)


def ask(source: Iterator[str], out: TextIO) -> Optional[str]:
    """Read one decision. ``None`` means the input ended (treat as quit).

    ``source`` must be a single live iterator, not a sequence: rebuilding
    ``iter(...)`` on every call would return the first line forever.

    Taking the first character means both ``a`` and ``approve`` work, and an
    accidental blank line re-prompts instead of silently approving something.
    """
    while True:
        print("> ", end="", file=out, flush=True)
        line = next(source, None)
        if line is None:
            print("", file=out)
            return None
        text = line.strip().lower()
        if not text:
            continue
        return text[0]


def _pick_category(
    entry: MemoryEntry, source: Iterator[str], out: TextIO
) -> Optional[str]:
    """Ask for a new category; return ``None`` if the answer was unusable."""
    print("\nnew category:", file=out)
    for position, name in enumerate(CATEGORIES, start=1):
        marker = "  <- current" if name == entry.category else ""
        print(f"  {position}. {name}{marker}", file=out)
    print("> ", end="", file=out, flush=True)
    line = next(source, None)
    if line is None:
        return None
    answer = line.strip().lower()
    if not answer:
        return None
    if answer.isdigit():
        index = int(answer)
        if 1 <= index <= len(CATEGORIES):
            return CATEGORIES[index - 1]
        return None
    return answer if answer in CATEGORIES else None


def run(
    store: VaultStore,
    source: Iterable[str],
    out: TextIO,
    err: TextIO,
    state: Optional[ReviewState] = None,
    limit: Optional[int] = None,
    reset_skips: bool = False,
) -> Outcome:
    """Drive the review loop. Pure enough to test: no globals, no TTY needed."""
    state = state or ReviewState()
    # One iterator for the whole session; rebuilding it would replay line one.
    stream: Iterator[str] = iter(source)
    outcome = Outcome()

    if reset_skips:
        dropped = state.reset(store.root)
        if dropped:
            print(f"forgot {dropped} skipped id(s)", file=out)

    entries, unreadable = store.scan()
    if unreadable:
        print(f"{len(unreadable)} file(s) unreadable; they stay pending", file=err)

    skip_set = set(state.skipped(store.root))
    queue = build_queue(entries, skip_set)
    if limit is not None:
        queue = queue[:limit]

    if not queue:
        if any(entry.status == "pending" for entry in entries):
            print("nothing to review -- every pending entry is already skipped?", file=out)
        else:
            print("nothing pending -- run `memoryhub import` first", file=out)
        return outcome

    print(f"{len(queue)} pending in {store.root}", file=out)
    print("one letter per entry, then Enter.", file=out)
    deferred_only = all(entry.id in skip_set for entry in queue)
    if deferred_only:
        # Every remaining entry was skipped earlier. Say so: otherwise the loop
        # looks like it never remembers anything, which is the opposite of true.
        print("these were all skipped before; `review --reset` clears that list", file=out)

    try:
        _walk(store, queue, stream, out, err, state, outcome)
    except KeyboardInterrupt:
        # Every decision is already on disk (one write per keypress), so Ctrl-C
        # only stops the loop -- it cannot half-apply anything.
        print("\ninterrupted -- decisions already made are saved.", file=out)

    print("", file=out)
    _print_summary(outcome, out)
    return outcome


def _walk(
    store: VaultStore,
    queue: Sequence[MemoryEntry],
    stream: Iterator[str],
    out: TextIO,
    err: TextIO,
    state: ReviewState,
    outcome: Outcome,
) -> None:
    """Ask about each entry in turn. Returns when the queue ends or the user quits."""
    for position, entry in enumerate(queue, start=1):
        # Re-read so a category edit applies to the version on disk, not to the
        # snapshot the scan produced (save() derives the path from status).
        fresh = store.get(entry.id)
        if fresh is None or fresh.status != "pending":
            continue
        entry = fresh

        while True:
            render(entry, position, len(queue), out)
            key = ask(stream, out)
            if key is None or key == "q":
                print("\nstopped. the next run resumes at the first undecided entry.", file=out)
                return

            if key not in KEYS:
                print(f"unknown key {key!r}; use: {HELP_LINE}", file=out)
                continue

            if key == "s":
                state.mark_skipped(store.root, entry.id)
                outcome.skipped.append(entry.id)
                break

            if key == "c":
                chosen = _pick_category(entry, stream, out)
                if chosen is None:
                    print("not changed: give a number or a full category name", file=out)
                    continue
                if chosen == entry.category:
                    print("category unchanged", file=out)
                    continue
                previous = entry.category
                entry.category = chosen
                try:
                    store.save(entry)
                except (StaleEntryError, ValidationError) as exc:
                    entry.category = previous
                    print(f"could not change category: {exc}", file=err)
                    outcome.errors.append(entry.id)
                    continue
                if entry.id not in outcome.recategorised:
                    outcome.recategorised.append(entry.id)
                print(f"category: {previous} -> {chosen}", file=out)
                continue  # same entry again: see the new state, then decide

            # a / r
            if key == "a" and entry.trust == "blocked":
                print("blocked: approving is refused; press r or s.", file=out)
                continue
            status = "approved" if key == "a" else "rejected"
            try:
                store.set_status(entry.id, status)
            except (StaleEntryError, ValidationError, KeyError) as exc:
                print(f"{status} failed: {exc}", file=err)
                outcome.errors.append(entry.id)
                break
            state.clear_skipped(store.root, entry.id)
            if key == "a":
                outcome.approved.append(entry.id)
            else:
                outcome.rejected.append(entry.id)
            print(f"{status}  {entry.id}", file=out)
            break


def _print_summary(outcome: Outcome, out: TextIO) -> None:
    print(outcome.summary(), file=out)
    # Ids are printed so a mis-keypress is undoable: `memoryhub reject <id>`.
    for label, ids in (("approved", outcome.approved), ("rejected", outcome.rejected)):
        if ids:
            print(f"  {label}: {' '.join(ids)}", file=out)
    if outcome.errors:
        print(f"  {len(outcome.errors)} write(s) failed; still pending", file=out)
