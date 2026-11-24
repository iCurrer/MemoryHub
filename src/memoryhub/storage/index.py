"""SQLite FTS5 index over the vault (P2-02).

The index is a cache, never a source of truth: delete ``database/index.db`` and
``search`` must still answer, identically, after rebuilding from Markdown. Two
rules follow from that and are enforced by tests:

- nothing may be stored here that a ``.md`` file does not already contain;
- a rebuild must not change the answer to a query.

Tokenizer choice: FTS5's default ``unicode61`` cannot match Chinese (it treats a
run of CJK as one token), so ``trigram`` is used. Trigram needs SQLite >= 3.34 and
only matches queries of 3+ characters; shorter queries fall back to ``LIKE``
rather than silently returning nothing.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import MemoryEntry
from .markdown_store import VaultStore

#: Trigram matching needs at least this many characters.
MIN_TRIGRAM = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id           TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    status       TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    body         TEXT NOT NULL,
    tags         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    id UNINDEXED, body, tags, tokenize='{tokenizer}'
);
"""


class IndexUnavailable(RuntimeError):
    """Raised when the index cannot be built or read."""


class Index:
    def __init__(self, vault_root: Path):
        self.root = Path(vault_root)
        self.path = self.root / "database" / "index.db"

    # ------------------------------------------------------------- plumbing

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> str:
        connection.executescript(SCHEMA)
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'tokenizer'"
        ).fetchone()
        tokenizer = row["value"] if row else ""
        if not tokenizer:
            for candidate in ("trigram", "unicode61"):
                try:
                    connection.execute(FTS_SCHEMA.format(tokenizer=candidate))
                    tokenizer = candidate
                    break
                except sqlite3.OperationalError:
                    continue
            else:
                raise IndexUnavailable("this SQLite build has no usable FTS5 tokenizer")
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('tokenizer', ?)",
                (tokenizer,),
            )
            connection.commit()
        else:
            connection.execute(FTS_SCHEMA.format(tokenizer=tokenizer))
        return tokenizer

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    # --------------------------------------------------------------- rebuild

    def rebuild(self, store: VaultStore) -> int:
        """Drop every row and re-derive them from Markdown."""
        connection = self._connect()
        count = 0
        try:
            tokenizer = self._ensure_schema(connection)
            connection.execute("DELETE FROM entries")
            connection.execute("DELETE FROM entries_fts")
            connection.execute("DELETE FROM files")
            for path in store.entry_paths():
                try:
                    entry = store.load(path)
                    stat = path.stat()
                except Exception:
                    continue  # unreadable files are reported by scan/doctor, not here
                self._insert(connection, entry, path, stat.st_mtime_ns, stat.st_size)
                count += 1
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('built_at', ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%S%z"),),
            )
            connection.commit()
            connection.execute(FTS_SCHEMA.format(tokenizer=tokenizer))
            return count
        finally:
            connection.close()

    def _insert(
        self,
        connection: sqlite3.Connection,
        entry: MemoryEntry,
        path: Path,
        mtime_ns: int,
        size: int,
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO files(path, mtime_ns, size) VALUES (?, ?, ?)",
            (str(path), mtime_ns, size),
        )
        connection.execute(
            "INSERT OR REPLACE INTO entries"
            "(id, category, status, updated_at, path, content_hash, body, tags)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.category,
                entry.status,
                entry.updated_at,
                path.as_posix(),
                VaultStore.sha256_of_text(entry.to_markdown()),
                entry.body,
                " ".join(entry.tags),
            ),
        )
        connection.execute(
            "INSERT INTO entries_fts(id, body, tags) VALUES (?, ?, ?)",
            (entry.id, entry.body, " ".join(entry.tags)),
        )

    # ------------------------------------------------------------- freshness

    def _file_stamps(self, store: VaultStore) -> Dict[str, tuple]:
        stamps: Dict[str, tuple] = {}
        for path in store.entry_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            stamps[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return stamps

    def is_fresh(self, store: VaultStore) -> bool:
        """Cheap check: stat every entry file, compare against the recorded stamps.

        Statting is far cheaper than parsing, so this is affordable on every search
        while still catching any edit made outside MemoryHub.
        """
        if not self.exists:
            return False
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            recorded = {
                row["path"]: (row["mtime_ns"], row["size"])
                for row in connection.execute("SELECT path, mtime_ns, size FROM files")
            }
        except sqlite3.DatabaseError:
            return False
        finally:
            connection.close()
        return recorded == self._file_stamps(store)

    def ensure_fresh(self, store: VaultStore) -> bool:
        """Returns True when a rebuild happened."""
        if self.is_fresh(store):
            return False
        self.rebuild(store)
        return True

    # ---------------------------------------------------------------- query

    def search_ids(self, query: str, limit: int = 200) -> List[str]:
        needle = query.strip()
        if not needle:
            return []
        connection = self._connect()
        try:
            tokenizer = self._ensure_schema(connection)
            if tokenizer == "trigram" and len(needle) < MIN_TRIGRAM:
                # Trigram cannot match a 1-2 character query; LIKE still can.
                rows = connection.execute(
                    "SELECT id FROM entries WHERE body LIKE ? OR tags LIKE ? LIMIT ?",
                    (f"%{needle}%", f"%{needle}%", limit),
                ).fetchall()
            else:
                phrase = '"' + needle.replace('"', '""') + '"'
                rows = connection.execute(
                    "SELECT id FROM entries_fts WHERE entries_fts MATCH ? LIMIT ?",
                    (phrase, limit),
                ).fetchall()
            return [row["id"] for row in rows]
        except sqlite3.DatabaseError as exc:
            raise IndexUnavailable(f"index query failed: {exc}") from exc
        finally:
            connection.close()

    # ---------------------------------------------------------------- health

    def check(self, store: VaultStore) -> Dict[str, Any]:
        """Doctor-facing summary. Never raises."""
        if not self.exists:
            return {"state": "absent", "detail": "not built yet (search will build it)"}
        try:
            connection = self._connect()
            try:
                tokenizer = self._ensure_schema(connection)
                indexed = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            finally:
                connection.close()
        except (sqlite3.DatabaseError, IndexUnavailable) as exc:
            return {"state": "broken", "detail": str(exc)}
        entries, _errors = store.scan()
        fresh = self.is_fresh(store)
        state = "ok" if fresh and indexed == len(entries) else "stale"
        return {
            "state": state,
            "detail": f"{indexed} indexed, {len(entries)} in vault, tokenizer={tokenizer}"
            + ("" if fresh else ", rebuild needed"),
        }
