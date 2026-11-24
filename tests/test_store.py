"""Phase 1 behaviour tests. Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import frontmatter  # noqa: E402
from memoryhub.models import MemoryEntry, ValidationError  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


class FrontmatterTests(unittest.TestCase):
    def test_roundtrip_quotes_windows_paths(self) -> None:
        meta = {
            "id": "mem_20261124T135122_a1b2",
            "source_path": "C:/Users/me/.codex/AGENTS.md",
            "tags": ["development", "python"],
            "valid_until": None,
        }
        text = frontmatter.join(meta, "body line\n")
        back, body = frontmatter.split(text)
        self.assertEqual(back["source_path"], "C:/Users/me/.codex/AGENTS.md")
        self.assertEqual(back["tags"], ["development", "python"])
        self.assertIsNone(back["valid_until"])
        self.assertEqual(body, "body line\n")

    def test_rejects_nested_yaml(self) -> None:
        text = "---\nid: x\nsource:\n  adapter: codex\n---\nbody\n"
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split(text)

    def test_unterminated_block_fails(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split("---\nid: x\nbody\n")


class ModelTests(unittest.TestCase):
    def test_create_is_pending_and_not_approved(self) -> None:
        entry = MemoryEntry.create(category="preference", body="prefers python")
        self.assertEqual(entry.status, "pending")
        self.assertEqual(entry.trust, "auto")

    def test_decision_category_needs_review(self) -> None:
        entry = MemoryEntry.create(category="decision", body="use sqlite fts5")
        self.assertEqual(entry.trust, "review")

    def test_sensitive_is_blocked(self) -> None:
        entry = MemoryEntry.create(
            category="knowledge", body="api key rotation", sensitivity="sensitive"
        )
        self.assertEqual(entry.trust, "blocked")
        entry.status = "approved"
        with self.assertRaises(ValidationError):
            entry.validate()

    def test_rejects_bad_category_and_safe_id(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryEntry.create(category="nonsense", body="x")

    def test_markdown_roundtrip_preserves_unknown_keys(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="hello")
        text = entry.to_markdown().replace("id: ", "future_field: 42\nid: ", 1)
        back = MemoryEntry.from_markdown(text)
        self.assertEqual(back.extra.get("future_field"), 42)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault-root"
        self.store = VaultStore(self.root)
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pending_lives_in_inbox_approved_in_vault(self) -> None:
        entry = MemoryEntry.create(category="preference", body="prefers python")
        path = self.store.save(entry)
        self.assertIn("inbox", path.parts)

        self.store.set_status(entry.id, "approved")
        hits = self.store.paths_for_id(entry.id)
        self.assertEqual(len(hits), 1, "promotion must not leave a duplicate behind")
        self.assertIn("vault", hits[0].parts)
        self.assertEqual(hits[0].parent.name, "preference")

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="x")
        self.store.save(entry)
        leftovers = [p for p in self.root.rglob("*.tmp*") if p.is_file()]
        self.assertEqual(leftovers, [])

    def test_blocked_entry_cannot_be_approved(self) -> None:
        entry = MemoryEntry.create(
            category="knowledge", body="secret thing", sensitivity="secret"
        )
        self.store.save(entry)
        with self.assertRaises(ValidationError):
            self.store.set_status(entry.id, "approved")

    def test_search_matches_body_and_tags(self) -> None:
        first = MemoryEntry.create(
            category="preference", body="prefers python", tags=["dev"]
        )
        second = MemoryEntry.create(category="project", body="rust rewrite", tags=["perf"])
        self.store.save(first)
        self.store.save(second)
        self.assertEqual([e.id for e in self.store.search("python")], [first.id])
        self.assertEqual([e.id for e in self.store.search("perf")], [second.id])
        self.assertEqual(self.store.search("absent"), [])

    def test_supersede_closes_validity_window(self) -> None:
        old = MemoryEntry.create(category="preference", body="prefers rust")
        new = MemoryEntry.create(
            category="preference", body="prefers python", supersedes=[old.id]
        )
        self.store.save(old)
        self.store.save(new)
        self.store.set_status(new.id, "approved")
        closed = self.store.stamp_validity(
            old.id, superseded_by=new.id, valid_until="2026-11-24T14:00:00+08:00"
        )
        self.assertEqual(closed.status, "archived")
        self.assertEqual(closed.superseded_by, new.id)
        self.assertEqual(closed.valid_until, "2026-11-24T14:00:00+08:00")

    def test_unsafe_id_is_refused(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="x")
        entry.id = "../../etc/passwd"
        with self.assertRaises(ValidationError):
            self.store.target_path(entry)

    def test_stats_counts_entries(self) -> None:
        self.store.save(MemoryEntry.create(category="preference", body="a"))
        self.store.save(MemoryEntry.create(category="knowledge", body="b"))
        stats = self.store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"].get("pending"), 2)


if __name__ == "__main__":
    unittest.main()
