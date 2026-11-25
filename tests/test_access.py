"""Access log: the vault learns who has read it (P5-07)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import access  # noqa: E402
from memoryhub.cli import build_parser  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


class AccessModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_appends_one_line_per_call(self) -> None:
        self.assertTrue(access.record(self.root, "memory_search", ["a", "b"], "propose", "claude-code"))
        self.assertTrue(access.record(self.root, "memory_get", ["a"], "read", ""))
        rows = access.lines(self.root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tool"], "memory_search")
        self.assertEqual(rows[0]["ids"], ["a", "b"])
        self.assertEqual(rows[1]["label"], "unlabeled")

    def test_the_query_text_is_never_stored(self) -> None:
        # A search string can be more revealing than the answer it found; the log
        # answers "who read this", not "what did they ask".
        access.record(self.root, "memory_search", ["a"], "propose", "claude-code")
        raw = access.path_for(self.root).read_text(encoding="utf-8")
        self.assertNotIn("query", raw)
        self.assertEqual(set(json.loads(raw)["ids"]), {"a"})

    def test_label_is_normalised_and_lowercased(self) -> None:
        access.record(self.root, "memory_search", ["a"], "propose", "Claude Code 2")
        self.assertEqual(access.lines(self.root)[0]["label"], "claude-code-2")
        self.assertEqual(access.label_from("  "), "")

    def test_ids_per_line_are_capped(self) -> None:
        access.record(self.root, "memory_list", [f"mem_{i}" for i in range(500)], "propose")
        row = access.lines(self.root)[0]
        self.assertEqual(row["count"], access.MAX_IDS_PER_LINE)
        self.assertEqual(len(row["ids"]), access.MAX_IDS_PER_LINE)

    def test_a_garbage_line_does_not_poison_the_log(self) -> None:
        target = access.path_for(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{"tool": "memory_get", "ids": ["a"]}\nnot json\n\n', encoding="utf-8")
        self.assertEqual(len(access.lines(self.root)), 1)

    def test_recording_never_raises_when_the_log_is_unusable(self) -> None:
        # A directory where the file should be: the read that just succeeded must
        # not fail because an observation about it could not be written.
        target = access.path_for(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        self.assertFalse(access.record(self.root, "memory_search", ["a"], "propose"))

    def test_tally_merges_both_generations(self) -> None:
        access.record(self.root, "memory_search", ["a"], "propose", "claude-code")
        self._rotate()
        access.record(self.root, "memory_get", ["a"], "read", "codex")
        slot = access.for_entry(self.root, "a")
        self.assertEqual(slot["reads"], 2)
        self.assertEqual(sorted(slot["labels"]), ["claude-code", "codex"])
        self.assertEqual(sorted(slot["tools"]), ["memory_get", "memory_search"])

    def _rotate(self) -> None:
        previous_size = access.ROTATE_AT
        try:
            access.ROTATE_AT = 1
            access.record(self.root, "memory_list", ["b"], "propose", "codex")
        finally:
            access.ROTATE_AT = previous_size
        self.assertTrue(access.path_for(self.root).with_suffix(".jsonl.1").is_file())

    def test_summary_counts_calls_and_labels(self) -> None:
        self.assertEqual(access.summary(self.root)["calls"], 0)
        access.record(self.root, "memory_search", ["a"], "propose", "claude-code")
        access.record(self.root, "memory_search", ["a"], "propose", "claude-code")
        summary = access.summary(self.root)
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["labels"], ["claude-code"])
        self.assertEqual(summary["read_entries"], 1)

    def test_for_entry_of_an_unread_entry_is_zero_not_an_error(self) -> None:
        self.assertEqual(access.for_entry(self.root, "mem_nope")["reads"], 0)


class AccessCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault-root"
        self.store = VaultStore(self.vault)
        self.store.init_layout()
        entry = MemoryEntry.create(category="preference", body="prefers python\n")
        self.store.create(entry)
        self.store.set_status(entry.id, "approved")
        self.entry_id = entry.id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str):
        parser = build_parser()
        args = parser.parse_args(["access", "--vault", str(self.vault), *argv])
        out = io.StringIO()
        with redirect_stdout(out):
            code = args.func(args)
        return code, out.getvalue()

    def test_no_reads_explains_how_to_start_logging(self) -> None:
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("no reads recorded", out)
        self.assertIn("--label", out)

    def test_tally_lists_the_most_read_entries_first(self) -> None:
        access.record(self.vault, "memory_search", [self.entry_id], "propose", "claude-code")
        access.record(self.vault, "memory_get", [self.entry_id], "propose", "codex")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("calls    2", out)
        self.assertIn("claude-code, codex", out)
        self.assertIn(self.entry_id, out)
        self.assertIn("2x", out)
        self.assertIn("prefers python", out)

    def test_entry_view_reports_who_and_how(self) -> None:
        access.record(self.vault, "memory_get", [self.entry_id], "read", "codex")
        code, out = self.run_cli("--entry", self.entry_id)
        self.assertEqual(code, 0)
        self.assertIn("read 1x", out)
        self.assertIn("codex", out)
        self.assertIn("memory_get", out)

    def test_entry_view_says_so_when_there_is_nothing(self) -> None:
        code, out = self.run_cli("--entry", "mem_unknown")
        self.assertEqual(code, 0)
        self.assertIn("no reads recorded", out)

    def test_a_deleted_entry_still_appears_in_the_tally(self) -> None:
        # The log outlives the entries it points at; that is the point of keeping
        # the id rather than a copy of the body.
        access.record(self.vault, "memory_get", ["mem_gone"], "propose", "codex")
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("no longer in the vault", out)


if __name__ == "__main__":
    unittest.main()
