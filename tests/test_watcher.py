"""Watcher rules (P2-06).

The three acceptance criteria are all clock-driven, so the clock is injected and
these tests never wait on real time.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.storage.markdown_store import VaultStore  # noqa: E402
from memoryhub.watcher import (  # noqa: E402
    Debouncer,
    HashTracker,
    MtimeTracker,
    exclude_roots,
    is_excluded,
    poll_once,
)

SAMPLE = """\
# User Memory Profile
> Last updated: 2026-11-15T07:52:40+08:00

## Memory Block

- prefers python
"""


class DebouncerTests(unittest.TestCase):
    def test_rapid_writes_collapse_into_one_release(self) -> None:
        debounce = Debouncer(quiet_seconds=3.0)
        path = Path("a.md")
        for moment in (0.0, 1.0, 2.0):
            debounce.observe(path, moment, "payload-1")
        # Still inside the quiet window measured from the *last* write.
        self.assertEqual(debounce.ready(4.9), [])
        released = debounce.ready(5.0)
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0][1], "payload-1")
        self.assertEqual(debounce.ready(6.0), [], "a released key must not fire twice")

    def test_different_paths_release_independently(self) -> None:
        debounce = Debouncer(quiet_seconds=2.0)
        debounce.observe(Path("a"), 0.0)
        debounce.observe(Path("b"), 10.0)
        self.assertEqual([p.name for p, _ in debounce.ready(2.0)], ["a"])
        self.assertEqual([p.name for p, _ in debounce.ready(12.0)], ["b"])


class HashTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "a.md"
        self.path.write_text("one", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_sight_is_a_change(self) -> None:
        self.assertTrue(HashTracker().changed(self.path))

    def test_identical_rewrite_is_not_a_change(self) -> None:
        tracker = HashTracker()
        self.assertTrue(tracker.changed(self.path))
        self.path.write_text("one", encoding="utf-8")
        self.assertFalse(tracker.changed(self.path))

    def test_modified_content_is_a_change(self) -> None:
        tracker = HashTracker()
        tracker.changed(self.path)
        self.path.write_text("one\ntwo", encoding="utf-8")
        self.assertTrue(tracker.changed(self.path))

    def test_missing_file_is_not_a_change(self) -> None:
        self.assertFalse(HashTracker().changed(Path(self._tmp.name) / "gone.md"))


class ExclusionTests(unittest.TestCase):
    def test_exports_and_logs_are_excluded(self) -> None:
        root = Path("D:/vault")
        roots = exclude_roots(root)
        self.assertTrue(is_excluded(root / "exports" / "cursor" / "x.md", roots))
        self.assertTrue(is_excluded(root / "logs" / "run.log", roots))
        self.assertFalse(is_excluded(root / "vault" / "preference" / "x.md", roots))


class PollOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = VaultStore(self.base / "vault-root")
        self.store.init_layout()
        self.home = self.base / "workbuddy"
        (self.home / "memory").mkdir(parents=True)
        self.source = self.home / "memory" / "demo_memory.md"
        self.source.write_text(SAMPLE, encoding="utf-8")
        self.env = mock.patch.dict(
            os.environ,
            {
                "WORKBUDDY_HOME": str(self.home),
                "CLAUDE_HOME": str(self.base / "claude"),
                "CODEX_HOME": str(self.base / "codex"),
                "CLAUDE_PROJECT_ROOT": str(self.base / "project"),
                "CODEX_PROJECT_ROOT": str(self.base / "project"),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def test_first_pass_imports_then_stays_quiet(self) -> None:
        tracker = HashTracker()
        first = poll_once(self.store, tracker=tracker, now=0.0)
        self.assertEqual(first.imported, 1)

        second = poll_once(self.store, tracker=tracker, now=100.0)
        self.assertEqual(second.imported, 0, "unchanged content must not re-import")
        self.assertEqual(len(self.store.scan()[0]), 1)

    def test_rewrite_with_new_content_imports_only_the_new_fact(self) -> None:
        tracker = HashTracker()
        poll_once(self.store, tracker=tracker, now=0.0)
        self.source.write_text(SAMPLE + "- prefers pnpm\n", encoding="utf-8")
        again = poll_once(self.store, tracker=tracker, now=100.0)
        self.assertEqual(again.imported, 1)
        self.assertEqual(len(self.store.scan()[0]), 2)

    def test_debounce_delays_then_releases_once(self) -> None:
        tracker = HashTracker()
        debounce = Debouncer(quiet_seconds=3.0)
        mtimes = MtimeTracker()
        poll = lambda now: poll_once(  # noqa: E731
            self.store, tracker=tracker, debouncer=debounce, mtimes=mtimes, now=now
        )
        self.assertEqual(poll(0.0).imported, 0)
        self.assertEqual(poll(1.0).imported, 0)
        self.assertEqual(poll(2.0).imported, 0)
        self.assertEqual(poll(5.0).imported, 1, "quiet for 3s -> released once")
        self.assertEqual(poll(9.0).imported, 0, "no further change -> nothing")

    def test_restart_does_not_double_import(self) -> None:
        poll_once(self.store, tracker=HashTracker(), now=0.0)
        # A fresh tracker (process restart) re-reads everything, but the vault
        # fingerprint set still refuses duplicates.
        again = poll_once(self.store, tracker=HashTracker(), now=100.0)
        self.assertEqual(again.imported, 0)
        self.assertEqual(again.duplicate, 1)


if __name__ == "__main__":
    unittest.main()
