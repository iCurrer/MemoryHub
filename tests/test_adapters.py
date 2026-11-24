"""Adapter protocol and first real adapter (P2-03).

Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.adapters import build, names  # noqa: E402
from memoryhub.adapters.base import Adapter, SourcePath, bullets  # noqa: E402
from memoryhub.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

SAMPLE = """\
# User Memory Profile
> Last updated: 2026-11-15T07:52:40+08:00
> Version: 23

## Memory Block

- Prefers Python for tooling unless there is a measured performance need.
- Dislikes tooling that writes into other tools' config files without asking.

## Working Style

- Asks for the reasoning first, the code second.
"""


class DummyAdapter(Adapter):
    """Smallest possible adapter: two ordered sources, one memory each."""

    name = "dummy"
    source_kind = "doc"

    def __init__(self, paths: list[Path]):
        self._paths = paths

    def discover(self) -> list[SourcePath]:
        return [
            SourcePath(
                path=path,
                adapter=self.name,
                source_kind=self.source_kind,
                precedence=index,
                layer="project",
            )
            for index, path in enumerate(self._paths)
        ]

    def read(self, source: SourcePath) -> list[MemoryEntry]:
        return [
            MemoryEntry.create(
                category="knowledge",
                body=source.path.read_text(encoding="utf-8"),
                source_adapter=self.name,
                source_path=source.path.as_posix(),
                source_kind=self.source_kind,
                precedence=source.precedence,
                layer=source.layer,
            )
        ]


class JsonlDummyAdapter(Adapter):
    """Proves the protocol is not tied to markdown (P2-03 criterion 2)."""

    name = "dummy-jsonl"
    source_kind = "session_distill"

    def __init__(self, path: Path):
        self._path = path

    def discover(self) -> list[SourcePath]:
        return [SourcePath(path=self._path, adapter=self.name, source_kind=self.source_kind)]

    def read(self, source: SourcePath) -> list[MemoryEntry]:
        import json

        entries = []
        for line in source.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            entries.append(
                MemoryEntry.create(
                    category="knowledge",
                    body=record["learned"],
                    source_adapter=self.name,
                    source_path=source.path.as_posix(),
                    source_kind=self.source_kind,
                )
            )
        return entries


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root / "vault-root")
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_can_write_is_always_false(self) -> None:
        # ADR-002: no adapter may ever write back.
        for name in names():
            adapter = build(name)
            self.assertFalse(adapter.capabilities()["can_write"])

    def test_dummy_adapter_completes_discover_read_inbox(self) -> None:
        first = self.root / "a.md"
        second = self.root / "b.md"
        first.write_text("alpha", encoding="utf-8")
        second.write_text("beta", encoding="utf-8")
        adapter = DummyAdapter([first, second])

        discovered = adapter.discover()
        self.assertEqual([s.precedence for s in discovered], [0, 1])

        for source in discovered:
            for entry in adapter.read(source):
                self.store.create(entry)

        entries, errors = self.store.scan()
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.status == "pending" for e in entries))
        self.assertTrue(all("inbox" in p.parts for p in self.store.entry_paths()))

    def test_source_order_is_preserved_into_entries(self) -> None:
        first = self.root / "global.md"
        second = self.root / "project.md"
        first.write_text("global rule", encoding="utf-8")
        second.write_text("project rule", encoding="utf-8")
        adapter = DummyAdapter([first, second])
        entries = [adapter.read(s)[0] for s in adapter.discover()]
        self.assertEqual([e.precedence for e in entries], [0, 1])
        self.assertEqual([e.layer for e in entries], ["project", "project"])

    def test_jsonl_adapter_is_not_tied_to_markdown(self) -> None:
        path = self.root / "session.jsonl"
        path.write_text(
            '{"learned": "redis needed for tests"}\n'
            '{"learned": "pnpm preferred over npm"}\n',
            encoding="utf-8",
        )
        adapter = JsonlDummyAdapter(path)
        entries = adapter.read(adapter.discover()[0])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1].body.strip(), "pnpm preferred over npm")
        self.assertEqual(entries[0].source_kind, "session_distill")

    def test_split_quoted_metadata_skips_the_title(self) -> None:
        from memoryhub.adapters.base import strip_quoted_metadata

        meta, rest = strip_quoted_metadata(SAMPLE)
        self.assertEqual(meta["last updated"], "2026-11-15T07:52:40+08:00")
        self.assertFalse(any(line.startswith(">") for line in rest))

    def test_bullets_fold_continuation_lines(self) -> None:
        text = "## S\n\n- first line\n  continued here\n- second\n"
        self.assertEqual(bullets(text), [("S", "first line continued here"), ("S", "second")])


class WorkBuddyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "memory").mkdir(parents=True)
        self.source = self.home / "memory" / "demo_memory.md"
        self.source.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discover_finds_the_memory_file(self) -> None:
        sources = WorkBuddyAdapter(self.home).discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_kind, "auto_memory")
        self.assertEqual(sources[0].confidence, "high")

    def test_missing_directory_discovers_nothing(self) -> None:
        self.assertEqual(WorkBuddyAdapter(self.home / "absent").discover(), [])

    def test_read_produces_one_entry_per_bullet(self) -> None:
        adapter = WorkBuddyAdapter(self.home)
        entries = adapter.read(adapter.discover()[0])
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].body.strip(), "Prefers Python for tooling unless there is a measured performance need.")
        self.assertIn("working-style", entries[2].tags)

    def test_imported_entries_are_pending_review_with_provenance(self) -> None:
        adapter = WorkBuddyAdapter(self.home)
        source = adapter.discover()[0]
        entry = adapter.read(source)[0]
        self.assertEqual(entry.status, "pending")
        self.assertEqual(entry.trust, "review")  # profile -> review, ADR-003
        self.assertEqual(entry.source_kind, "auto_memory")
        self.assertEqual(entry.source_adapter, "workbuddy")
        self.assertTrue(entry.source_hash.startswith("sha256:"))
        self.assertEqual(entry.source_path, source.path.as_posix())

    def test_created_at_comes_from_the_source_header(self) -> None:
        from datetime import datetime

        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("2026-11-15T07:52:40"))
        # Contract: stored times always carry a timezone.
        self.assertIsNotNone(datetime.fromisoformat(entry.created_at).tzinfo)

    def test_invalid_header_timestamp_falls_back_to_now(self) -> None:
        self.source.write_text(
            "# User Memory Profile\n> Last updated: not-a-date\n\n## Memory Block\n\n- one\n",
            encoding="utf-8",
        )
        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("20"))

    def test_bom_does_not_hide_the_header(self) -> None:
        # Windows editors write a BOM; without stripping it the title line does
        # not match and the `> Last updated:` header is lost.
        self.source.write_text("\ufeff" + SAMPLE, encoding="utf-8")
        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("2026-11-15T07:52:40"))


if __name__ == "__main__":
    unittest.main()
