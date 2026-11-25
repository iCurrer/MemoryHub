"""Interactive review (P4-05). Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import review  # noqa: E402
from memoryhub.cli import main  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


class FakeTTY(io.StringIO):
    """StringIO that claims to be a terminal, so the CLI guard lets it through."""

    def isatty(self) -> bool:  # noqa: D401 - test double
        return True


class ReviewTests(unittest.TestCase):
    """Each test gets a vault, a config home, and its own skip list."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.home.mkdir()
        self.store = VaultStore(self.vault)
        self.store.init_layout()
        self.state_path = self.home / "review.json"
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def make(self, body: str, category: str = "preference", **kwargs) -> MemoryEntry:
        entry = MemoryEntry.create(category=category, body=body, **kwargs)
        self.store.create(entry)
        return entry

    def run_review(self, keys, **kwargs) -> review.Outcome:
        state = kwargs.pop("state", None) or review.ReviewState(self.state_path)
        return review.run(
            self.store,
            keys,
            self.out,
            self.err,
            state=state,
            **kwargs,
        )

    def status_of(self, entry: MemoryEntry) -> str:
        fresh = self.store.get(entry.id)
        self.assertIsNotNone(fresh)
        return fresh.status

    # ------------------------------------------------------------------ queue

    def test_skipped_entries_move_to_the_back_not_out(self) -> None:
        first = self.make("first", created_at="2026-01-01T00:00:00+08:00")
        second = self.make("second", created_at="2026-01-02T00:00:00+08:00")
        state = review.ReviewState(self.state_path)
        state.mark_skipped(self.vault, first.id)

        queue = review.build_queue(
            list(self.store.iter_entries()), state.skipped(self.vault)
        )
        self.assertEqual([entry.id for entry in queue], [second.id, first.id])

    def test_decided_entries_leave_the_queue(self) -> None:
        entry = self.make("decided already")
        self.store.set_status(entry.id, "approved")
        queue = review.build_queue(list(self.store.iter_entries()), [])
        self.assertEqual(queue, [])

    # ------------------------------------------------------------------- ask

    def test_blank_line_reprompts_and_case_is_ignored(self) -> None:
        source = iter(["", "   ", "A\n"])
        self.assertEqual(review.ask(source, self.out), "a")

    def test_end_of_input_is_quit_not_a_crash(self) -> None:
        self.assertIsNone(review.ask(iter([]), self.out))

    # -------------------------------------------------------------- decisions

    def test_approve_writes_the_status(self) -> None:
        entry = self.make("prefers pnpm over npm")
        outcome = self.run_review(["a\n"])
        self.assertEqual(self.status_of(entry), "approved")
        self.assertEqual(outcome.approved, [entry.id])
        self.assertIn("approved 1", self.out.getvalue())

    def test_reject_writes_the_status(self) -> None:
        entry = self.make("a stale preference")
        outcome = self.run_review(["r\n"])
        self.assertEqual(self.status_of(entry), "rejected")
        self.assertEqual(outcome.rejected, [entry.id])

    def test_quit_leaves_the_rest_pending(self) -> None:
        # Explicit timestamps: same-second entries tie on created_at and fall
        # back to id order, which is random per run.
        first = self.make("one", created_at="2026-01-01T00:00:00+08:00")
        second = self.make("two", created_at="2026-01-02T00:00:00+08:00")
        outcome = self.run_review(["a\n", "q\n"])
        self.assertEqual(self.status_of(first), "approved")
        self.assertEqual(self.status_of(second), "pending")
        self.assertEqual(outcome.approved, [first.id])

    def test_empty_input_decides_nothing(self) -> None:
        entry = self.make("untouched")
        outcome = self.run_review([])
        self.assertEqual(self.status_of(entry), "pending")
        self.assertEqual(outcome.decided, 0)

    def test_unknown_key_reprompts_instead_of_approving(self) -> None:
        entry = self.make("careful now")
        outcome = self.run_review(["x\n", "a\n"])
        self.assertEqual(self.status_of(entry), "approved")
        self.assertEqual(outcome.approved, [entry.id])
        self.assertIn("unknown key", self.out.getvalue())

    def test_ctrl_c_keeps_what_was_already_decided(self) -> None:
        first = self.make("one", created_at="2026-01-01T00:00:00+08:00")
        second = self.make("two", created_at="2026-01-02T00:00:00+08:00")

        def keys():
            yield "a\n"
            raise KeyboardInterrupt

        outcome = self.run_review(keys())
        self.assertEqual(self.status_of(first), "approved")
        self.assertEqual(self.status_of(second), "pending")
        self.assertEqual(outcome.approved, [first.id])
        self.assertIn("interrupted", self.out.getvalue())

    # -------------------------------------------------------------- skipping

    def test_skip_keeps_pending_and_is_remembered_across_runs(self) -> None:
        entry = self.make("hard to decide")
        self.run_review(["s\n"])
        self.assertEqual(self.status_of(entry), "pending")
        self.assertTrue(self.state_path.is_file())
        self.assertEqual(
            json.loads(self.state_path.read_text(encoding="utf-8"))[
                self.vault.resolve().as_posix()
            ],
            [entry.id],
        )
        # Second run: it is still there (it was only deferred).
        self.out = io.StringIO()
        self.run_review([])
        self.assertIn("all skipped before", self.out.getvalue())

    def test_reset_forgets_the_skip_list(self) -> None:
        entry = self.make("deferred once")
        self.run_review(["s\n"])
        self.out = io.StringIO()
        outcome = self.run_review(["a\n"], reset_skips=True)
        self.assertEqual(self.status_of(entry), "approved")
        self.assertEqual(outcome.approved, [entry.id])

    def test_deciding_clears_the_skip_entry(self) -> None:
        entry = self.make("skipped then approved")
        self.run_review(["s\n"])
        self.out = io.StringIO()
        self.run_review(["a\n"], reset_skips=True)
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn(self.vault.resolve().as_posix(), stored)

    def test_skip_state_lives_outside_the_vault(self) -> None:
        # A cursor inside the vault would show up as a diff and be the first
        # non-memory object in it; state belongs with the config instead.
        self.make("anything")
        self.run_review(["s\n"])
        self.assertFalse((self.vault / "review.json").exists())

    def test_corrupt_state_file_does_not_block_review(self) -> None:
        self.state_path.write_text("{not json", encoding="utf-8")
        entry = self.make("still reviewable")
        outcome = self.run_review(["a\n"])
        self.assertEqual(self.status_of(entry), "approved")
        self.assertEqual(outcome.approved, [entry.id])

    # ------------------------------------------------------ change category

    def test_change_category_keeps_pending_and_asks_again(self) -> None:
        entry = self.make("switched", category="preference")
        # c -> "4" (knowledge), then the same entry is shown again -> approve.
        outcome = self.run_review(["c\n", "4\n", "a\n"])
        fresh = self.store.get(entry.id)
        self.assertEqual(fresh.category, "knowledge")
        self.assertEqual(fresh.status, "approved")
        self.assertEqual(outcome.recategorised, [entry.id])
        self.assertEqual(outcome.approved, [entry.id])

    def test_change_category_accepts_a_name_and_rejects_nonsense(self) -> None:
        entry = self.make("by name", category="preference")
        outcome = self.run_review(["c\n", "bogus\n", "c\n", "decision\n", "q\n"])
        self.assertEqual(self.store.get(entry.id).category, "decision")
        self.assertEqual(outcome.recategorised, [entry.id])
        self.assertIn("not changed", self.out.getvalue())

    # ---------------------------------------------------------------- blocked

    def test_blocked_entry_cannot_be_approved_in_the_loop(self) -> None:
        entry = self.make("secret-ish", category="profile", sensitivity="sensitive")
        self.assertEqual(self.store.get(entry.id).trust, "blocked")
        outcome = self.run_review(["a\n", "r\n"])
        self.assertEqual(self.status_of(entry), "rejected")
        self.assertEqual(outcome.approved, [])
        self.assertIn("blocked", self.out.getvalue())

    # ------------------------------------------------------------------ empty

    def test_empty_vault_explains_where_to_start(self) -> None:
        outcome = self.run_review([])
        self.assertEqual(outcome.decided, 0)
        self.assertIn("nothing pending", self.out.getvalue())

    def test_limit_stops_early(self) -> None:
        # Explicit timestamps: two entries created in the same second would tie
        # on created_at and fall back to id order, which is random per run.
        first = self.make("one", created_at="2026-01-01T00:00:00+08:00")
        second = self.make("two", created_at="2026-01-02T00:00:00+08:00")
        outcome = self.run_review(["a\n"], limit=1)
        self.assertEqual(outcome.approved, [first.id])
        self.assertEqual(self.status_of(second), "pending")


class ReviewCliTests(unittest.TestCase):
    """The wiring: argument parsing, the TTY guard, exit codes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.store = VaultStore(self.vault)
        self.store.init_layout()
        self.env = {
            "MEMORYHUB_HOME": str(self.home),
            "MEMORYHUB_VAULT": str(self.vault),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, *argv: str, stdin=None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        patches = [mock.patch.dict(os.environ, self.env, clear=False)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", stdin))
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        finally:
            for patch in reversed(patches):
                patch.stop()
        return code, out.getvalue(), err.getvalue()

    def test_non_tty_is_refused_rather_than_silently_doing_nothing(self) -> None:
        code, _out, err = self.call("review")
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err)
        self.assertIn("approve", err)  # points scripts at the non-interactive path

    def test_review_is_on_the_first_screen(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertIn("review", out.getvalue())

    def test_pty_style_stdin_drives_a_real_approval(self) -> None:
        entry = MemoryEntry.create(category="preference", body="approved via CLI")
        self.store.create(entry)
        code, out, err = self.call("review", stdin=FakeTTY("a\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.get(entry.id).status, "approved")
        self.assertIn("approved 1", out)


if __name__ == "__main__":
    unittest.main()
