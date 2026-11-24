"""The only component allowed to write into a vault.

Guarantees provided here:

- every write is atomic (temp file in the same directory + ``os.replace``)
- every write holds a cross-process lock
- a memory id maps to exactly one file; moving between ``inbox`` and ``vault``
  leaves no duplicate behind
- reading tolerates a partially written vault; writing never does
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import frontmatter
from ..config import LAYOUT
from ..models import MemoryEntry, ValidationError, now_iso

_SAFE_ID_RE = re.compile(r"^mem_[0-9A-Za-z_]+$")


class LockTimeout(RuntimeError):
    """Raised when the vault lock cannot be acquired in time."""


class IdCollisionError(ValidationError):
    """Raised when a write would reuse an id that already has an owner.

    Phase 1 used a 4-hex id suffix, so bulk import could mint the same id twice
    and ``save`` would silently overwrite the first entry. Creating an entry now
    refuses instead.
    """


class VaultLock:
    """Advisory cross-process lock based on O_CREAT|O_EXCL.

    A lock file older than ``stale_after`` seconds is treated as garbage from a
    crashed process and removed. This is coarse but adequate for a single-user
    tool; a real lock library replaces it in phase 2.
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 120.0):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: Optional[int] = None

    def __enter__(self) -> "VaultLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode("ascii", "ignore"))
                return self
            except FileExistsError:
                self._evict_if_stale()
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire vault lock within {self.timeout}s: {self.path}"
                    )
                time.sleep(0.05)

    def _evict_if_stale(self) -> None:
        try:
            age = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if age > self.stale_after:
            try:
                self.path.unlink()
            except (FileNotFoundError, PermissionError):
                pass

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class VaultStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ------------------------------------------------------------- layout

    @property
    def vault_dir(self) -> Path:
        return self.root / "vault"

    @property
    def lock_path(self) -> Path:
        return self.root / ".memoryhub.lock"

    def init_layout(self) -> List[Path]:
        created: List[Path] = []
        for relative in LAYOUT:
            target = self.root / relative
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created.append(target)
        return created

    def _readable_dirs(self) -> Iterator[Path]:
        yield self.vault_dir
        inbox = self.root / "inbox"
        yield inbox / "pending"
        yield inbox / "rejected"
        yield inbox / "conflicts"

    # -------------------------------------------------------------- hashes

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    @classmethod
    def sha256_of_file(cls, path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    @classmethod
    def sha256_of_text(cls, text: str) -> str:
        return cls.sha256_bytes(text.encode("utf-8"))

    # --------------------------------------------------------------- paths

    def target_path(self, entry: MemoryEntry) -> Path:
        if not _SAFE_ID_RE.match(entry.id):
            raise ValidationError(f"refusing to build a path from unsafe id: {entry.id!r}")
        if entry.status == "pending":
            return self.root / "inbox" / "pending" / f"{entry.id}.md"
        if entry.status == "rejected":
            return self.root / "inbox" / "rejected" / f"{entry.id}.md"
        return self.vault_dir / entry.category / f"{entry.id}.md"

    def paths_for_id(self, entry_id: str) -> List[Path]:
        if not _SAFE_ID_RE.match(entry_id):
            return []
        found: List[Path] = []
        for directory in self._readable_dirs():
            candidate = directory / f"{entry_id}.md"
            if candidate.is_file():
                found.append(candidate)
            elif directory.is_dir():
                found.extend(directory.rglob(f"{entry_id}.md"))
        return sorted(set(found))

    def entry_paths(self) -> Iterator[Path]:
        seen: set[Path] = set()
        for directory in self._readable_dirs():
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*.md")):
                if path in seen:
                    continue
                seen.add(path)
                yield path

    # ---------------------------------------------------------------- read

    def load(self, path: Path) -> MemoryEntry:
        return MemoryEntry.from_markdown(Path(path).read_text(encoding="utf-8"))

    def iter_entries(
        self,
        strict: bool = False,
        errors: Optional[List[Tuple[Path, str]]] = None,
    ) -> Iterator[MemoryEntry]:
        """Yield entries; unreadable files are reported, never silently dropped.

        ``errors`` collects ``(path, message)`` pairs for the caller to surface.
        When it is ``None`` a warning goes to stderr, which keeps the old
        behaviour for callers that just want entries.
        """
        for path in self.entry_paths():
            try:
                yield self.load(path)
            except (
                ValidationError,
                frontmatter.FrontmatterError,
                OSError,
                UnicodeDecodeError,
                TypeError,
            ) as exc:
                # TypeError is a last resort: a single hand-broken file must not
                # abort the whole scan, and ``scan``/``doctor`` still report it.
                if strict:
                    raise
                if errors is not None:
                    errors.append((path, str(exc)))
                else:
                    print(
                        f"warning: skipped unreadable entry {path}: {exc}",
                        file=sys.stderr,
                    )

    def scan(
        self, strict: bool = False
    ) -> Tuple[List[MemoryEntry], List[Tuple[Path, str]]]:
        """Load the whole vault once: ``(entries, unreadable)``."""
        errors: List[Tuple[Path, str]] = []
        entries = list(self.iter_entries(strict=strict, errors=errors))
        return entries, errors

    def get(self, entry_id: str) -> Optional[MemoryEntry]:
        for path in self.paths_for_id(entry_id):
            return self.load(path)
        return None

    def search(self, query: str, include_expired: bool = False) -> List[MemoryEntry]:
        needle = query.strip().lower()
        if not needle:
            return []
        hits: List[MemoryEntry] = []
        for entry in self.iter_entries():
            if not include_expired and entry.is_expired():
                continue
            haystack = [entry.body.lower(), entry.id.lower()]
            haystack.extend(tag.lower() for tag in entry.tags)
            if any(needle in field for field in haystack):
                hits.append(entry)
        return hits

    # --------------------------------------------------------------- write

    def create(self, entry: MemoryEntry) -> Path:
        """Write a brand-new entry; refuse if its id is already taken.

        This is the only entry point new content should use (CLI ``new``, and
        later the adapters). ``save`` stays the in-place update used by status
        transitions, where overwriting the same id is expected.
        """
        entry.validate()
        with VaultLock(self.lock_path):
            occupied = self.paths_for_id(entry.id)
            if occupied:
                raise IdCollisionError(
                    f"id {entry.id} already belongs to {occupied[0]}; "
                    "refusing to overwrite it"
                )
            target = self.target_path(entry)
            atomic_write_text(target, entry.to_markdown())
        return target

    def save(self, entry: MemoryEntry) -> Path:
        """Upsert an entry that already exists, or one that is about to.

        **Reload before editing.** The target path is derived from ``entry.status``,
        so saving a stale snapshot silently reverts the entry's location -- a
        pending copy of an entry that has since been approved will be written back
        into ``inbox/pending`` and the approved file deleted. Use
        ``store.get(id)`` to obtain the current object first. (Tracked as the
        "status and location are two sources of truth" debt in HANDOFF.)
        """
        entry.validate()
        with VaultLock(self.lock_path):
            target = self.target_path(entry)
            self._refuse_foreign_owner(entry)
            atomic_write_text(target, entry.to_markdown())
            for other in self.paths_for_id(entry.id):
                if other != target:
                    other.unlink(missing_ok=True)
        return target

    def _refuse_foreign_owner(self, entry: MemoryEntry) -> None:
        """Best-effort net under ``save``: never clobber a different entry.

        ``created_at`` is second-granular, so two entries born in the same
        second are indistinguishable here; ``create`` is the real guarantee.
        """
        for path in self.paths_for_id(entry.id):
            try:
                existing = self.load(path)
            except (
                ValidationError,
                frontmatter.FrontmatterError,
                OSError,
                UnicodeDecodeError,
            ) as exc:
                raise IdCollisionError(
                    f"refusing to overwrite an unreadable file at {path}: {exc}"
                ) from exc
            if existing.created_at != entry.created_at:
                raise IdCollisionError(
                    f"id {entry.id} belongs to an entry created at "
                    f"{existing.created_at}, not {entry.created_at}"
                )

    def set_status(self, entry_id: str, status: str) -> MemoryEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(f"no such memory: {entry_id}")
        if status == "approved" and entry.trust == "blocked":
            raise ValidationError(
                f"{entry_id} is blocked (sensitivity={entry.sensitivity}); "
                "lower the sensitivity or change trust before approving"
            )
        entry.status = status
        entry.updated_at = now_iso()
        self.save(entry)
        return entry

    def stamp_validity(
        self, entry_id: str, *, superseded_by: str, valid_until: str
    ) -> MemoryEntry:
        """Close the validity window of a superseded entry (ADR-006)."""
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(f"no such memory: {entry_id}")
        entry.valid_until = valid_until
        entry.superseded_by = superseded_by
        entry.status = "archived"
        entry.updated_at = now_iso()
        self.save(entry)
        return entry

    # --------------------------------------------------------------- stats

    def stats(self) -> Dict[str, Any]:
        entries, errors = self.scan()
        by_status: Dict[str, int] = {}
        by_category: Dict[str, int] = {}
        total = 0
        for entry in entries:
            total += 1
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            by_category[entry.category] = by_category.get(entry.category, 0) + 1
        return {
            "total": total,
            "by_status": by_status,
            "by_category": by_category,
            "unreadable": len(errors),
        }
