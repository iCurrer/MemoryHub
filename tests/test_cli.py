"""End-to-end CLI tests. Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
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

from memoryhub.cli import main  # noqa: E402


class CliTests(unittest.TestCase):
    """Each test gets its own vault and its own config home."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.workbuddy = self.base / "workbuddy"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "MEMORYHUB_HOME": str(self.home),
            "WORKBUDDY_HOME": str(self.workbuddy),
            # Isolate every adapter: without these they would scan the repository
            # this test runs from and import its own AGENTS.md / CLAUDE.md.
            "CLAUDE_HOME": str(self.base / "claude-home"),
            "CLAUDE_PROJECT_ROOT": str(self.base / "claude-project"),
            "CODEX_HOME": str(self.base / "codex-home"),
            "CODEX_PROJECT_ROOT": str(self.base / "codex-project"),
            "CHERRY_STUDIO_HOME": str(self.base / "cherry-home"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEMORYHUB_VAULT", None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def init_vault(self, *extra: str) -> None:
        code, _out, err = self.run_cli("init", "--vault", str(self.vault), "--no-git", *extra)
        self.assertEqual(code, 0, err)

    # ------------------------------------------------------------ --vault

    def test_vault_flag_before_subcommand_is_honoured(self) -> None:
        other = self.base / "before"
        code, _out, err = self.run_cli("--vault", str(other), "init", "--no-git")
        self.assertEqual(code, 0, err)
        self.assertTrue((other / "vault").is_dir())

    def test_vault_flag_after_subcommand_is_honoured(self) -> None:
        other = self.base / "after"
        code, _out, err = self.run_cli("init", "--vault", str(other), "--no-git")
        self.assertEqual(code, 0, err)
        self.assertTrue((other / "vault").is_dir())

    def test_trailing_vault_flag_is_accepted_by_read_commands(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli("doctor", "--vault", str(self.vault))
        self.assertEqual(code, 0, err)

    def test_trailing_vault_flag_wins_over_leading_one(self) -> None:
        trailing = self.base / "trailing"
        code, _out, err = self.run_cli(
            "--vault", str(self.base / "leading"), "init", "--vault", str(trailing), "--no-git"
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((trailing / "vault").is_dir())
        self.assertFalse((self.base / "leading").exists())

    # ------------------------------------------------------------ doctor

    def test_doctor_passes_on_a_freshly_initialised_vault(self) -> None:
        self.init_vault()
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[FAIL]", out)
        self.assertIn("[WARN]", out)  # git was skipped on purpose

    def test_doctor_fails_when_the_vault_is_missing(self) -> None:
        code, out, err = self.run_cli("doctor", "--vault", str(self.base / "absent"))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", out)

    # ------------------------------------------------------- approve tier

    def test_approve_refuses_a_review_tier_category(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli(
            "new", "--category", "profile", "--body", "solo developer", "--approve"
        )
        self.assertEqual(code, 1)
        self.assertIn("ADR-003", err)
        _code, listed, _err = self.run_cli("list")
        self.assertIn("0 entries", listed)

    def test_force_overrides_the_review_tier(self) -> None:
        self.init_vault()
        code, out, err = self.run_cli(
            "new", "--category", "profile", "--body", "solo developer",
            "--approve", "--force",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("status=approved trust=review", out)

    def test_approved_blocked_entry_is_still_refused_even_with_force(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli(
            "new", "--category", "knowledge", "--body", "token",
            "--sensitivity", "secret", "--approve", "--force",
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked", err)

    # ------------------------------------------------------- error paths

    def test_missing_file_reports_an_error_not_a_traceback(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli(
            "new", "--category", "knowledge", "--file", str(self.base / "nope.txt")
        )
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertNotIn("Traceback", err)

    def test_show_reports_a_hand_edited_frontmatter_as_an_error(self) -> None:
        self.init_vault()
        code, out, err = self.run_cli("new", "--category", "preference", "--body", "prefers python")
        self.assertEqual(code, 0, err)
        entry_id = out.split()[0]
        entry_path = next(self.vault.rglob("*.md"))
        entry_path.write_text(
            entry_path.read_text(encoding="utf-8").replace("tags: []", "tags: [unclosed"),
            encoding="utf-8",
        )
        code, _out, err = self.run_cli("show", entry_id)
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", err)
        self.assertNotIn("Traceback", err)

    def test_list_warns_about_unreadable_files(self) -> None:
        self.init_vault()
        self.run_cli("new", "--category", "preference", "--body", "prefers python")
        entry_path = next(self.vault.rglob("*.md"))
        entry_path.write_text("---\nid: nope\n---\n", encoding="utf-8")
        code, _out, err = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("could not be read", err)

    # -------------------------------------------------------------- scan

    def write_workbuddy_sample(self) -> None:
        memory = self.workbuddy / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "demo_memory.md").write_text(
            "# User Memory Profile\n"
            "> Last updated: 2026-11-15T07:52:40+08:00\n"
            "\n## Memory Block\n\n"
            "- prefers python for tooling\n"
            "- dislikes silent write-backs\n",
            encoding="utf-8",
        )

    def test_scan_imports_pending_entries(self) -> None:
        self.init_vault()
        self.write_workbuddy_sample()
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertIn("2 imported", out)
        _code, listed, _err = self.run_cli("list", "--status", "pending")
        self.assertIn("2 entries", listed)

    def test_scan_is_idempotent(self) -> None:
        self.init_vault()
        self.write_workbuddy_sample()
        self.run_cli("scan")
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertIn("0 imported, 2 already present", out)
        _code, listed, _err = self.run_cli("stats")
        self.assertIn("total    2", listed)

    def test_scan_dry_run_writes_nothing(self) -> None:
        self.init_vault()
        self.write_workbuddy_sample()
        code, out, _err = self.run_cli("scan", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would be imported", out)
        _code, listed, _err = self.run_cli("list")
        self.assertIn("0 entries", listed)

    def test_scan_with_no_sources_succeeds(self) -> None:
        self.init_vault()
        code, out, err = self.run_cli("scan")
        self.assertEqual(code, 0, err)
        self.assertIn("0 source(s)", out)


if __name__ == "__main__":
    unittest.main()
