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

    # ------------------------------------------------------- review flow

    def make(self, body: str, category: str = "preference", **extra: str) -> str:
        self.init_vault()
        argv = ["new", "--category", category, "--body", body]
        for key, value in extra.items():
            argv.extend([f"--{key.replace('_', '-')}", value])
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return out.split()[0]

    def test_diff_shows_changed_lines_against_same_source(self) -> None:
        first = self.make("prefers pnpm", source_path="C:/x/CLAUDE.md", adapter="claude")
        self.run_cli("approve", first)
        second = self.make(
            "prefers pnpm and not yarn", source_path="C:/x/CLAUDE.md", adapter="claude"
        )
        code, out, err = self.run_cli("diff", second)
        self.assertEqual(code, 0, err)
        self.assertIn(f"against  {first}", out)
        self.assertIn("-prefers pnpm", out)
        self.assertIn("+prefers pnpm and not yarn", out)
        self.assertIn("C:/x/CLAUDE.md", out)

    def test_diff_with_explicit_against(self) -> None:
        first = self.make("alpha")
        second = self.make("beta")
        code, out, _err = self.run_cli("diff", second, "--against", first)
        self.assertEqual(code, 0)
        self.assertIn("-alpha", out)
        self.assertIn("+beta", out)

    def test_diff_without_counterpart_prints_the_body(self) -> None:
        only = self.make("solo fact", source_path="C:/y/CLAUDE.md")
        code, out, _err = self.run_cli("diff", only)
        self.assertEqual(code, 0)
        self.assertIn("nothing to compare against", out)
        self.assertIn("solo fact", out)

    def test_diff_on_missing_id_is_an_error(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli("diff", "mem_20200101T000000_deadbeef")
        self.assertEqual(code, 1)
        self.assertIn("no such memory", err)

    def test_approve_all_skips_blocked_entries(self) -> None:
        ok = self.make("a normal preference")
        blocked = self.make("api key: sk-abcdefghijklmnopqrstuvwx123456",
                            category="knowledge", sensitivity="sensitive")
        code, out, err = self.run_cli("approve", "--all")
        self.assertEqual(code, 0)
        self.assertIn(f"approved  {ok}", out)
        self.assertNotIn(blocked, out.split("blocked")[0])
        self.assertIn("1 blocked", err)
        _c, listed, _e = self.run_cli("list", "--status", "approved")
        self.assertIn("1 entries", listed)

    def test_approve_all_filters_by_category(self) -> None:
        self.make("a preference")
        profile = self.make("a profile fact", category="profile")
        code, out, _err = self.run_cli("approve", "--all", "--category", "profile")
        self.assertEqual(code, 0)
        self.assertIn(profile, out)
        self.assertIn("1 approved", out)
        _c, listed, _e = self.run_cli("list", "--status", "pending")
        self.assertIn("1 entries", listed)

    def test_approve_all_dry_run_writes_nothing(self) -> None:
        self.make("a preference")
        code, out, _err = self.run_cli("approve", "--all", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would be approved", out)
        _c, listed, _e = self.run_cli("stats")
        self.assertIn("pending", listed)
        self.assertNotIn("approved", listed)

    def test_approve_without_id_or_all_is_a_usage_error(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli("approve")
        self.assertEqual(code, 2)
        self.assertIn("--all", err)

    def test_sensitivity_raise_blocks_approval(self) -> None:
        entry = self.make("lives near a landmark")
        code, out, err = self.run_cli("sensitivity", entry, "--level", "sensitive")
        self.assertEqual(code, 0, err)
        self.assertIn("normal -> sensitive", out)
        self.assertIn("trust auto -> blocked", out)
        code, _out, err = self.run_cli("approve", entry)
        self.assertEqual(code, 1)
        self.assertIn("blocked", err)

    def test_sensitivity_can_be_lowered_again(self) -> None:
        entry = self.make("a fact")
        self.run_cli("sensitivity", entry, "--level", "secret")
        code, out, err = self.run_cli("sensitivity", entry, "--level", "normal")
        self.assertEqual(code, 0, err)
        self.assertIn("trust blocked -> auto", out)
        self.assertEqual(self.run_cli("approve", entry)[0], 0)

    def test_raising_sensitivity_on_an_approved_entry_is_refused(self) -> None:
        entry = self.make("already approved")
        self.run_cli("approve", entry)
        code, _out, err = self.run_cli("sensitivity", entry, "--level", "secret")
        self.assertEqual(code, 1)
        self.assertIn("Reject or archive", err)
        _c, listed, _e = self.run_cli("list", "--status", "approved")
        self.assertIn("1 entries", listed)

    def test_sensitivity_unknown_id_is_an_error(self) -> None:
        self.init_vault()
        code, _out, err = self.run_cli(
            "sensitivity", "mem_20200101T000000_deadbeef", "--level", "personal"
        )
        self.assertEqual(code, 1)
        self.assertIn("no such memory", err)

    # ------------------------------------------------------------- audit

    def test_audit_reports_under_classified_entries(self) -> None:
        clean = self.make("prefers pnpm")
        leaky = self.make("lives at 示例市示例区示例路 1 号")  # pii:synthetic
        code, out, _err = self.run_cli("audit")
        self.assertEqual(code, 0)
        self.assertIn(leaky, out)
        self.assertNotIn(clean, out)
        self.assertIn("--apply", out)

    def test_audit_apply_blocks_the_leaky_entry(self) -> None:
        leaky = self.make("lives at 示例市示例区示例路 1 号")  # pii:synthetic
        code, out, _err = self.run_cli("audit", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("1 entries re-classified", out)
        _c, listed, _e = self.run_cli("list", "--status", "pending")
        self.assertIn(leaky, listed)
        # blocked now: approving must be refused
        code, _out, err = self.run_cli("approve", leaky)
        self.assertEqual(code, 1)
        self.assertIn("blocked", err)

    def test_audit_apply_skips_approved_entries(self) -> None:
        entry = self.make("prefers pnpm")
        self.run_cli("approve", entry)
        leaky = self.make("phone 13900000000")
        self.run_cli("approve", leaky)
        code, _out, err = self.run_cli("audit", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("approved; reject or archive it first", err)

    def test_audit_on_a_clean_vault_says_so(self) -> None:
        self.make("prefers pnpm")
        code, out, _err = self.run_cli("audit")
        self.assertEqual(code, 0)
        self.assertIn("no unmarked sensitive content", out)


if __name__ == "__main__":
    unittest.main()
