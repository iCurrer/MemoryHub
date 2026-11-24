"""SQLite FTS5 index (P2-02).

The index is a cache. These tests exist to keep it that way: they assert that a
deleted index rebuilds to the *same answers*, and that Chinese text is actually
searchable (the reason FTS5's default tokenizer is not used).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.index import Index  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VaultStore(Path(self._tmp.name) / "vault-root")
        self.store.init_layout()
        self.index = Index(self.store.root)

        self.chinese = MemoryEntry.create(
            category="preference", body="用户开发项目时优先考虑 Python", tags=["开发"]
        )
        self.store.create(self.chinese)
        self.store.set_status(self.chinese.id, "approved")

        self.english = MemoryEntry.create(
            category="knowledge", body="prefers pnpm over npm", tags=["tooling"]
        )
        self.store.create(self.english)
        self.store.set_status(self.english.id, "approved")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ids(self, query: str):
        return sorted(self.index.search_ids(query))

    def test_first_search_builds_the_index(self) -> None:
        self.assertFalse(self.index.exists)
        self.assertTrue(self.index.ensure_fresh(self.store))
        self.assertTrue(self.index.exists)
        self.assertEqual(self.ids("pnpm"), [self.english.id])

    def test_chinese_phrase_of_three_or_more_characters_matches(self) -> None:
        self.index.ensure_fresh(self.store)
        self.assertEqual(self.ids("开发项目"), [self.chinese.id])
        self.assertEqual(self.ids("优先考虑"), [self.chinese.id])

    def test_short_chinese_query_falls_back_to_like(self) -> None:
        # Trigram cannot match fewer than 3 characters; returning nothing would
        # look like "the memory is gone".
        self.index.ensure_fresh(self.store)
        self.assertEqual(self.ids("开发"), [self.chinese.id])

    def test_deleting_the_index_rebuilds_to_the_same_answers(self) -> None:
        self.index.ensure_fresh(self.store)
        before = {query: self.ids(query) for query in ("pnpm", "开发项目", "开发")}

        self.index.path.unlink()
        self.assertFalse(self.index.exists)
        self.assertTrue(self.index.ensure_fresh(self.store))

        after = {query: self.ids(query) for query in ("pnpm", "开发项目", "开发")}
        self.assertEqual(before, after)

    def test_edit_outside_memoryhub_makes_the_index_stale(self) -> None:
        self.index.ensure_fresh(self.store)
        self.assertFalse(self.index.ensure_fresh(self.store))

        victim = next(
            path for path in self.store.entry_paths() if "knowledge" in path.parts
        )
        victim.write_text(
            victim.read_text(encoding="utf-8").replace("pnpm over npm", "yarn over npm"),
            encoding="utf-8",
        )
        self.assertTrue(self.index.ensure_fresh(self.store), "stale index must rebuild")
        self.assertEqual(self.ids("yarn"), [self.english.id])
        self.assertEqual(self.ids("pnpm"), [])

    def test_new_entry_is_picked_up(self) -> None:
        self.index.ensure_fresh(self.store)
        fresh = MemoryEntry.create(category="decision", body="use sqlite fts5 trigram")
        self.store.create(fresh)
        self.store.set_status(fresh.id, "approved")
        self.assertTrue(self.index.ensure_fresh(self.store))
        self.assertEqual(self.ids("fts5"), [fresh.id])

    def test_markdown_stays_the_only_source_of_truth(self) -> None:
        # Everything the index knows must be derivable from the .md files.
        self.index.ensure_fresh(self.store)
        self.index.path.unlink()
        rebuilt = Index(self.store.root)
        self.assertEqual(rebuilt.rebuild(self.store), 2)
        self.assertEqual(rebuilt.search_ids("pnpm"), [self.english.id])

    def test_check_reports_state(self) -> None:
        self.assertEqual(self.index.check(self.store)["state"], "absent")
        self.index.ensure_fresh(self.store)
        report = self.index.check(self.store)
        self.assertEqual(report["state"], "ok")
        self.assertIn("tokenizer=", report["detail"])

    def test_empty_query_returns_nothing(self) -> None:
        self.index.ensure_fresh(self.store)
        self.assertEqual(self.index.search_ids("   "), [])


if __name__ == "__main__":
    unittest.main()
