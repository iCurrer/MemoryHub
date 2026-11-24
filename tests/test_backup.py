"""Backup: local commit always, remote push only when safe (P5-04).

Run against real temporary git repositories: the behaviour under test *is* git's,
so mocking it would only verify my assumptions.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.backup import (  # noqa: E402
    GITIGNORE_LINES,
    commit,
    ensure_repo,
    has_changes,
    push,
    remotes,
    sensitive_entries,
    write_gitignore,
)
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

GIT = shutil.which("git")


@unittest.skipUnless(GIT, "git is required for backup tests")
class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "vault-root"
        self.store = VaultStore(self.root)
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, body: str, sensitivity: str = "normal") -> MemoryEntry:
        entry = MemoryEntry.create(
            category="preference", body=body, sensitivity=sensitivity
        )
        self.store.create(entry)
        return entry

    # ------------------------------------------------------------- repo

    def test_ensure_repo_is_idempotent(self) -> None:
        self.assertTrue(ensure_repo(self.root))
        self.assertFalse(ensure_repo(self.root))

    def test_gitignore_excludes_derived_state_and_keeps_user_lines(self) -> None:
        (self.root / ".gitignore").write_text("# mine\nsecrets/\n", encoding="utf-8")
        self.assertTrue(write_gitignore(self.root))
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("# mine", text, "user entries must survive")
        for line in GITIGNORE_LINES:
            self.assertIn(line.strip(), text)
        self.assertFalse(write_gitignore(self.root), "second run changes nothing")

    # ------------------------------------------------------------ commit

    def test_commit_creates_history_and_second_run_is_a_noop(self) -> None:
        ensure_repo(self.root)
        write_gitignore(self.root)
        self.add("prefers pnpm")
        sha = commit(self.root, "first")
        self.assertIsNotNone(sha)
        self.assertFalse(has_changes(self.root))
        self.assertIsNone(commit(self.root, "second"))

    def test_derived_state_is_not_committed(self) -> None:
        ensure_repo(self.root)
        write_gitignore(self.root)
        (self.root / "database").mkdir(exist_ok=True)
        (self.root / "database" / "index.db").write_bytes(b"binary")
        (self.root / "logs").mkdir(exist_ok=True)
        (self.root / "logs" / "run.log").write_text("noise", encoding="utf-8")
        self.add("prefers pnpm")
        commit(self.root, "first")
        tracked = subprocess.run(
            [GIT, "-C", str(self.root), "ls-files"], capture_output=True, text=True
        ).stdout
        self.assertNotIn("index.db", tracked)
        self.assertNotIn("run.log", tracked)
        self.assertIn("prefers pnpm", (self.root / "inbox/pending").joinpath(
            next(p.name for p in (self.root / "inbox/pending").glob("*.md"))
        ).read_text(encoding="utf-8"))

    # -------------------------------------------------------------- push

    def test_push_to_a_local_bare_remote(self) -> None:
        bare = Path(self._tmp.name) / "backup.git"
        subprocess.run([GIT, "init", "--bare", "-q", str(bare)], check=True)
        ensure_repo(self.root)
        write_gitignore(self.root)
        self.add("prefers pnpm")
        commit(self.root, "first")
        subprocess.run(
            [GIT, "-C", str(self.root), "remote", "add", "backup", str(bare)], check=True
        )
        self.assertIn("backup", remotes(self.root))
        ok, detail = push(self.root, "backup")
        self.assertTrue(ok, detail)

    def test_push_to_a_missing_remote_reports_it(self) -> None:
        ensure_repo(self.root)
        self.add("x")
        commit(self.root, "first")
        ok, detail = push(self.root, "nope")
        self.assertFalse(ok)
        self.assertIn("no such remote", detail)

    # -------------------------------------------------------- the gate

    def test_sensitive_entries_are_detected(self) -> None:
        clean = self.add("prefers pnpm")
        leaky = self.add("lives at 示例区示例路 390 号", sensitivity="sensitive")
        flagged = {entry.id for entry in sensitive_entries(self.store)}
        self.assertEqual(flagged, {leaky.id})
        self.assertNotIn(clean.id, flagged)

    def test_blocked_entries_count_as_sensitive(self) -> None:
        entry = self.add("key: sk-abcdefghijklmnopqrstuvwx123456", sensitivity="secret")
        self.assertEqual(entry.trust, "blocked")
        self.assertEqual([e.id for e in sensitive_entries(self.store)], [entry.id])


if __name__ == "__main__":
    unittest.main()
