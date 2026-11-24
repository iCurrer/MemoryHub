"""Phase 1 behaviour tests. Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import frontmatter  # noqa: E402
from memoryhub.models import MemoryEntry, ValidationError  # noqa: E402
from memoryhub.storage.markdown_store import IdCollisionError, VaultStore  # noqa: E402


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

    def test_nested_yaml_is_parsed(self) -> None:
        # Phase 1 rejected nested YAML; phase 2 (P2-01) must understand it,
        # because the planning book's own schema uses a nested `source:` list.
        text = "---\nid: x\nsource:\n  - adapter: codex\n    path: AGENTS.md\n---\nbody\n"
        meta, body = frontmatter.split(text)
        self.assertEqual(meta["source"], [{"adapter": "codex", "path": "AGENTS.md"}])
        self.assertEqual(body, "body\n")

    def test_obsidian_block_list_is_readable(self) -> None:
        entry = MemoryEntry.create(category="preference", body="prefers python")
        text = entry.to_markdown().replace("tags: []", "tags:\n  - dev\n  - python")
        back = MemoryEntry.from_markdown(text)
        self.assertEqual(back.tags, ["dev", "python"])

    def test_multiline_and_nested_roundtrip(self) -> None:
        meta = {
            "id": "mem_20261124T000000_abcdef01",
            "note": "line one\nline two",
            "source": [{"adapter": "codex", "path": "AGENTS.md"}],
        }
        text = frontmatter.join(meta, "body\n")
        back, body = frontmatter.split(text)
        self.assertEqual(back, meta)
        self.assertEqual(body, "body\n")

    def test_dumped_markdown_is_valid_yaml(self) -> None:
        entry = MemoryEntry.create(
            category="knowledge", body="x", tags=["development", "a b", "123"]
        )
        block = entry.to_markdown().split("---\n")[1]
        loaded = yaml.safe_load(block)  # independent of our loader
        self.assertEqual(loaded["id"], entry.id)
        self.assertEqual(loaded["tags"], ["development", "a b", "123"])

    def test_timestamps_come_back_as_strings(self) -> None:
        meta, _body = frontmatter.split("---\ncreated_at: 2026-11-24T13:51:22+08:00\n---\n")
        self.assertIsInstance(meta["created_at"], str)
        self.assertEqual(meta["created_at"], "2026-11-24T13:51:22+08:00")

    def test_yaml11_bool_words_stay_strings(self) -> None:
        # PyYAML's default resolver turns on/off/yes/no into booleans (YAML 1.1);
        # a tag named `on` would then fail validation.
        meta, _body = frontmatter.split("---\ntags: [on, yes, true, no]\n---\n")
        self.assertEqual(meta["tags"], ["on", "yes", True, "no"])

    def test_duplicate_key_is_rejected(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split("---\nid: one\nid: two\n---\n")

    def test_non_mapping_frontmatter_is_rejected(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split("---\n- a\n- b\n---\n")

    def test_unterminated_block_fails(self) -> None:
        with self.assertRaises(frontmatter.FrontmatterError):
            frontmatter.split("---\nid: x\nbody\n")

    def test_utf8_bom_does_not_hide_the_frontmatter(self) -> None:
        # Windows editors write a BOM; without stripping it the whole block
        # used to be treated as body text and the entry vanished.
        entry = MemoryEntry.create(category="knowledge", body="from notepad")
        text = "\ufeff" + entry.to_markdown()
        back = MemoryEntry.from_markdown(text)
        self.assertEqual(back.id, entry.id)
        self.assertEqual(back.body.strip(), "from notepad")


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

    def test_ids_are_unique_under_bulk_creation(self) -> None:
        # 4 hex chars produced ~32 duplicates per 2000 ids; 8 must not.
        ids = {MemoryEntry.new_id() for _ in range(5000)}
        self.assertEqual(len(ids), 5000)

    def test_legacy_short_id_still_parses(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="legacy")
        text = entry.to_markdown().replace(entry.id, "mem_20261124T135122_a1b2")
        back = MemoryEntry.from_markdown(text)
        self.assertEqual(back.id, "mem_20261124T135122_a1b2")

    def test_tags_and_supersedes_must_be_string_lists(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="x")
        entry.tags = "not-a-list"
        with self.assertRaises(ValidationError):
            entry.validate()
        entry.tags = [1]
        with self.assertRaises(ValidationError):
            entry.validate()
        entry.tags = []
        entry.supersedes = "mem_20261124T135122_a1b2"
        with self.assertRaises(ValidationError):
            entry.validate()

    def test_low_confidence_requires_review(self) -> None:
        low = MemoryEntry.create(category="knowledge", body="x", confidence="low")
        high = MemoryEntry.create(category="knowledge", body="x", confidence="high")
        self.assertEqual(low.trust, "review")
        self.assertEqual(high.trust, "auto")

    def test_is_expired(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="x")
        self.assertFalse(entry.is_expired())
        entry.valid_until = "2020-01-01T00:00:00+08:00"
        self.assertTrue(entry.is_expired())
        entry.valid_until = "2999-01-01T00:00:00+08:00"
        self.assertFalse(entry.is_expired())


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
        self.assertEqual(stats["unreadable"], 0)

    def test_create_refuses_duplicate_id(self) -> None:
        first = MemoryEntry.create(category="knowledge", body="first")
        self.store.create(first)
        second = MemoryEntry.create(category="knowledge", body="second")
        second.id = first.id
        with self.assertRaises(IdCollisionError):
            self.store.create(second)
        self.assertEqual(self.store.get(first.id).body.strip(), "first")

    def test_save_refuses_to_clobber_a_different_entry(self) -> None:
        first = MemoryEntry.create(category="knowledge", body="first")
        self.store.create(first)
        intruder = MemoryEntry.create(category="knowledge", body="intruder")
        intruder.id = first.id
        intruder.created_at = "2020-01-01T00:00:00+08:00"
        intruder.valid_from = "2020-01-01T00:00:00+08:00"
        with self.assertRaises(IdCollisionError):
            self.store.save(intruder)
        self.assertEqual(self.store.get(first.id).body.strip(), "first")

    def test_scan_surfaces_unreadable_files(self) -> None:
        entry = MemoryEntry.create(category="preference", body="prefers python")
        path = self.store.save(entry)
        # Broken YAML: a human typo, not a format we should ever write.
        path.write_text(
            path.read_text(encoding="utf-8").replace("tags: []", "tags: [unclosed"),
            encoding="utf-8",
        )
        entries, errors = self.store.scan()
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("invalid YAML", errors[0][1])
        self.assertEqual(self.store.stats()["unreadable"], 1)

    def test_scan_keeps_readable_entries_next_to_broken_ones(self) -> None:
        good = MemoryEntry.create(category="knowledge", body="good")
        bad = MemoryEntry.create(category="knowledge", body="bad")
        self.store.save(good)
        bad_path = self.store.save(bad)
        bad_path.write_text(
            bad_path.read_text(encoding="utf-8").replace(
                "category: knowledge", "category: nonsense"
            ),
            encoding="utf-8",
        )
        entries, errors = self.store.scan()
        self.assertEqual([e.id for e in entries], [good.id])
        self.assertEqual(len(errors), 1)

    def test_search_hides_expired_entries(self) -> None:
        old = MemoryEntry.create(category="preference", body="prefers rust")
        self.store.save(old)
        self.store.set_status(old.id, "approved")
        self.store.stamp_validity(
            old.id,
            superseded_by=MemoryEntry.new_id(),
            valid_until="2020-01-01T00:00:00+08:00",
        )
        self.assertEqual(self.store.search("rust"), [])
        self.assertEqual(len(self.store.search("rust", include_expired=True)), 1)


if __name__ == "__main__":
    unittest.main()
