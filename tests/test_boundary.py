"""Boundary guard tests: the code repo and the vault must never overlap.

These tests encode the rule that failed in practice — a vault path inside a
git working tree is one `git add -A` away from publishing someone's memories.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryhub import boundary  # noqa: E402


def _load_linter():
    path = ROOT / "tools" / "lint_staged.py"
    spec = importlib.util.spec_from_file_location("lint_staged", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lint_staged = _load_linter()


class IsolationTests(unittest.TestCase):
    def test_vault_inside_another_repo_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            host = Path(raw)
            subprocess.run(["git", "init", "-q", str(host)], check=False)
            vault = host / "vault"
            vault.mkdir()
            problems = boundary.violations(vault)
            self.assertTrue(problems)
            self.assertIn("inside another git repository", problems[0])

    def test_vault_inside_this_project_is_refused(self) -> None:
        problems = boundary.violations(ROOT / "vault")
        self.assertTrue(any("source tree" in problem for problem in problems))

    def test_standalone_vault_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            vault.mkdir()
            self.assertEqual(boundary.violations(vault), [])

    def test_vault_may_be_its_own_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            vault.mkdir()
            subprocess.run(["git", "init", "-q", str(vault)], check=False)
            self.assertEqual(boundary.violations(vault), [])


class RemoteTests(unittest.TestCase):
    def test_remote_pointing_at_code_repo_is_flagged(self) -> None:
        problems = boundary.remote_violations(
            ["https://github.com/iCurrer/MemoryHub.git"]
        )
        self.assertEqual(len(problems), 1)

    def test_private_remote_is_accepted(self) -> None:
        self.assertEqual(
            boundary.remote_violations(["git@github.com:iCurrer/private-mem.git"]),
            [],
        )


class HookTests(unittest.TestCase):
    def test_pre_push_hook_is_written_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            (vault / ".git" / "hooks").mkdir(parents=True)
            hook = boundary.install_pre_push_hook(vault)
            self.assertIsNotNone(hook)
            assert hook is not None
            self.assertIn("refusing to push memories", hook.read_text(encoding="utf-8"))

    def test_hook_refuses_code_repo_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            (vault / ".git" / "hooks").mkdir(parents=True)
            hook = boundary.install_pre_push_hook(vault)
            assert hook is not None
            result = subprocess.run(
                ["sh", str(hook), "origin", "https://github.com/iCurrer/MemoryHub.git"],
                cwd=str(vault),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)

    def test_hook_allows_private_remote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            (vault / ".git" / "hooks").mkdir(parents=True)
            hook = boundary.install_pre_push_hook(vault)
            assert hook is not None
            result = subprocess.run(
                ["sh", str(hook), "backup", "git@example.com:me/mem.git"],
                cwd=str(vault),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0)


class LintRulesTests(unittest.TestCase):
    def test_vault_data_path_is_forbidden(self) -> None:
        self.assertTrue(lint_staged.check_path("vault/preference/mem_x.md"))
        self.assertTrue(lint_staged.check_path("inbox/pending/mem_y.md"))
        self.assertTrue(lint_staged.check_path("database/index.sqlite"))

    def test_source_path_is_allowed(self) -> None:
        self.assertEqual(lint_staged.check_path("src/memoryhub/models.py"), [])

    def test_commit_message_with_address_is_blocked(self) -> None:
        problems = lint_staged.check_commit_message("fix: 服务器在示例市示例区示例路 7 号")  # pii:synthetic
        self.assertTrue(problems)

    def test_commit_message_with_uuid_is_blocked(self) -> None:
        problems = lint_staged.check_commit_message(
            "chore: sync 2a1795f6-360a-4f02-a0b4-3e915eb79bcf"  # pii:synthetic
        )
        self.assertTrue(problems)

    def test_ordinary_message_passes(self) -> None:
        self.assertEqual(
            lint_staged.check_commit_message("feat: add boundary guard\n\nTwo repos."), []
        )

    def test_synthetic_marker_exempts_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "fixture.md"
            fixture.write_text("地址 示例市示例区示例路 7 号  # pii:synthetic\n", encoding="utf-8")
            original = lint_staged.ROOT
            lint_staged.ROOT = Path(raw)
            try:
                problems = lint_staged.check_content("fixture.md", [])
            finally:
                lint_staged.ROOT = original
            self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
