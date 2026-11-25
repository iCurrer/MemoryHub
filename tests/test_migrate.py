"""Migration commands: recategorize and `scan --refresh`.

Both exist because a rule change must not leave two generations of the same
memory in the review queue, and neither may rewrite the user's own filing
decision without being asked.
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


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.claude_home = self.base / "claude-home"
        self.claude_home.mkdir()
        code, _out, err = self.run_cli("init", "--vault", str(self.vault), "--no-git")
        self.assertEqual(code, 0, err)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "MEMORYHUB_HOME": str(self.home),
            "CLAUDE_HOME": str(self.claude_home),
            "CLAUDE_PROJECT_ROOT": str(self.base / "claude-project"),
            "CODEX_HOME": str(self.base / "codex-home"),
            "CODEX_PROJECT_ROOT": str(self.base / "codex-project"),
            "WORKBUDDY_HOME": str(self.base / "workbuddy"),
            "CHERRY_STUDIO_HOME": str(self.base / "cherry-home"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEMORYHUB_VAULT", None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def new_entry(self, body: str, category: str = "profile") -> str:
        code, out, err = self.run_cli(
            "new", "--category", category, "--body", body, "--vault", str(self.vault)
        )
        self.assertEqual(code, 0, err)
        return out.split()[0]

    # -------------------------------------------------- recategorize

    def test_dry_run_reports_without_writing(self) -> None:
        entry_id = self.new_entry("真实姓名绝不外泄", category="profile")
        code, out, _err = self.run_cli("recategorize", "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("profile", out)
        self.assertIn("decision", out)
        self.assertIn("--apply", out)
        code, shown, _err = self.run_cli("show", entry_id, "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("profile", shown)

    def test_apply_refiles_the_entry(self) -> None:
        entry_id = self.new_entry("真实姓名绝不外泄", category="profile")
        code, out, err = self.run_cli(
            "recategorize", "--apply", "--vault", str(self.vault)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("re-filed", out)
        code, shown, _err = self.run_cli("show", entry_id, "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("decision", shown)

    def test_entries_without_a_matching_rule_are_reported_not_guessed(self) -> None:
        self.new_entry("今天天气不错，适合散步。", category="profile")
        code, out, _err = self.run_cli("recategorize", "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("no category changes", out)
        self.assertIn("matched no rule", out)

    # -------------------------------------------------- scan --refresh

    def test_refresh_archives_entries_the_source_no_longer_produces(self) -> None:
        claude_md = self.claude_home / "CLAUDE.md"
        claude_md.write_text(
            "# 偏好\n\n- 偏好本地优先，不要云端。\n\n# 目标\n\n- 今年以备考为主。\n",
            encoding="utf-8",
        )
        code, out, err = self.run_cli("scan", "--adapter", "claude", "--vault", str(self.vault))
        self.assertEqual(code, 0, err)
        first = self.run_cli("stats", "--vault", str(self.vault))[1]
        self.assertIn("pending", first)

        claude_md.write_text("# 偏好\n\n- 现在只做本地部署。\n", encoding="utf-8")
        code, out, err = self.run_cli(
            "scan", "--adapter", "claude", "--refresh", "--vault", str(self.vault)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("archived", out)

        code, listed, _err = self.run_cli(
            "list", "--status", "archived", "--vault", str(self.vault)
        )
        self.assertEqual(code, 0)
        self.assertIn("entries", listed)
        self.assertNotIn("0 entries", listed)

    def test_dry_run_never_archives(self) -> None:
        claude_md = self.claude_home / "CLAUDE.md"
        claude_md.write_text("# 偏好\n\n- 偏好本地优先。\n", encoding="utf-8")
        self.run_cli("scan", "--adapter", "claude", "--vault", str(self.vault))
        claude_md.write_text("# 偏好\n\n- 完全不同的内容。\n", encoding="utf-8")
        code, out, _err = self.run_cli(
            "scan", "--adapter", "claude", "--refresh", "--dry-run", "--vault", str(self.vault)
        )
        self.assertEqual(code, 0)
        self.assertNotIn("archived", out)


if __name__ == "__main__":
    unittest.main()
