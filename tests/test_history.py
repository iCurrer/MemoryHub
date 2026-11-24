"""Vault history via git (P4-03).

These tests drive a real temporary git repository: the whole point of reading
history from git is that it is *real* history, and mocking `git log` would test
the parser against my own assumptions rather than against git.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.backup import HistoryUnavailable, collect, since_to_git, summarise  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

GIT = shutil.which("git")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [GIT, "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@unittest.skipUnless(GIT, "git is required for history tests")
class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault-root"
        self.store = VaultStore(self.root)
        self.store.init_layout()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def add_approved(self, body: str) -> MemoryEntry:
        entry = MemoryEntry.create(category="preference", body=body)
        self.store.create(entry)
        self.store.set_status(entry.id, "approved")
        return entry

    # ------------------------------------------------------------ since

    def test_since_specs_are_translated(self) -> None:
        self.assertEqual(since_to_git("7d"), "7 days ago")
        self.assertEqual(since_to_git("12h"), "12 hours ago")
        self.assertEqual(since_to_git("2w"), "2 weeks ago")
        self.assertEqual(since_to_git("2026-11-01"), "2026-11-01")
        self.assertIsNone(since_to_git(None))

    # -------------------------------------------------------- collection

    def test_added_entry_shows_up_as_added(self) -> None:
        entry = self.add_approved("prefers python")
        self.commit("add one memory")
        commits = collect(self.root)
        self.assertEqual(len(commits), 1)
        changes = {c.entry_id: c for c in commits[0].changes if c.entry_id}
        self.assertIn(entry.id, changes)
        self.assertEqual(changes[entry.id].action, "A")
        self.assertEqual(changes[entry.id].category, "preference")

    def test_update_and_removal_are_distinguished(self) -> None:
        entry = self.add_approved("prefers python")
        self.commit("add")

        # Reload before editing: set_status() returned a *new* object, and saving a
        # stale snapshot would send the file back to inbox/ (see save() docstring).
        fresh = self.store.get(entry.id)
        fresh.body = "prefers python 3.12\n"
        self.store.save(fresh)
        self.commit("edit")

        path = self.store.paths_for_id(entry.id)[0]
        path.unlink()
        self.commit("delete")

        commits = collect(self.root)  # newest first
        actions = [
            change.action
            for commit in commits
            for change in commit.changes
            if change.entry_id == entry.id
        ]
        self.assertEqual(actions[0], "D")
        self.assertIn("M", actions)
        self.assertIn("A", actions)

    def test_summary_counts(self) -> None:
        first = self.add_approved("one")
        second = self.add_approved("two")
        self.commit("two memories")
        stats = summarise(collect(self.root))
        self.assertEqual(stats["commits"], 1)
        self.assertEqual(stats["added"], 2)
        self.assertEqual(stats["entries"], 2)
        self.assertEqual({first.id, second.id}, {first.id, second.id})

    def test_non_memory_files_are_reported_but_not_counted_as_entries(self) -> None:
        (self.root / "notes.txt").write_text("x", encoding="utf-8")
        self.commit("unrelated")
        commits = collect(self.root)
        self.assertEqual(summarise(commits)["entries"], 0)
        self.assertTrue(all(not change.entry_id for change in commits[0].changes))

    def test_repo_without_commits_is_an_error_not_an_empty_list(self) -> None:
        with self.assertRaises(HistoryUnavailable) as caught:
            collect(self.root)
        self.assertIn("no commits", str(caught.exception))

    def test_non_repo_is_an_error(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(HistoryUnavailable) as caught:
            collect(plain)
        self.assertIn("not a git repository", str(caught.exception))

    def test_limit_caps_the_commits_read(self) -> None:
        for index in range(3):
            self.add_approved(f"fact {index}")
            self.commit(f"c{index}")
        self.assertEqual(len(collect(self.root, limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
