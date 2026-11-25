"""Tests for the write-back layer (P5-06 / ADR-019).

The one that matters most is :class:`EchoChamberTests`: if ``import`` does not
skip the managed block, every ``sync`` becomes input for the next ``import``
and the vault slowly fills with copies of itself. Everything else here is
plumbing; that one is the reason the marker exists.
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

from memoryhub import managed, sync  # noqa: E402
from memoryhub.adapters.claude import ClaudeAdapter  # noqa: E402
from memoryhub.adapters.base import SourcePath  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402


def make(body: str, *, category: str = "profile", created: str = "2026-11-24T10:00:00+08:00") -> MemoryEntry:
    return MemoryEntry.create(
        category=category, body=body, tags=["t"], created_at=created
    )


class MarkerTests(unittest.TestCase):
    def test_strip_returns_the_owner_text(self) -> None:
        text = "# 标题\n\n正文一\n\n<!-- MEMORYHUB:BEGIN x -->\n区块\n<!-- MEMORYHUB:END -->\n\n正文二\n"
        result = managed.strip_block(text)
        self.assertIn("正文一", result)
        self.assertIn("正文二", result)
        self.assertNotIn("MEMORYHUB", result)
        self.assertNotIn("区块", result)

    def test_strip_without_block_is_identity(self) -> None:
        text = "# 只有我自己的内容\n"
        self.assertEqual(managed.strip_block(text), text)

    def test_append_does_not_touch_existing_text(self) -> None:
        before = "# 标题\n\n正文\n"
        after = managed.replace_block(before, "<!-- MEMORYHUB:BEGIN -->\nX\n<!-- MEMORYHUB:END -->")
        self.assertTrue(after.startswith(before.rstrip("\n")))
        self.assertIn("X", after)

    def test_replace_is_idempotent(self) -> None:
        block = "<!-- MEMORYHUB:BEGIN -->\n第一版\n<!-- MEMORYHUB:END -->"
        once = managed.replace_block("# 我的文件\n", block)
        twice = managed.replace_block(once, block)
        self.assertEqual(once, twice)
        self.assertEqual(once.count(managed.BEGIN), 1)

    def test_new_block_replaces_old_one_in_place(self) -> None:
        first = "<!-- MEMORYHUB:BEGIN -->\n旧\n<!-- MEMORYHUB:END -->"
        second = "<!-- MEMORYHUB:BEGIN -->\n新\n<!-- MEMORYHUB:END -->"
        text = managed.replace_block("头\n\n" + first + "\n\n尾\n", second)
        self.assertIn("新", text)
        self.assertNotIn("旧", text)
        self.assertIn("尾", text)


class RenderTests(unittest.TestCase):
    def test_select_skips_pending_superseded_and_expired(self) -> None:
        live = make("活着的")
        live.status = "approved"
        pending = make("待审的")
        superseded = make("被取代的")
        superseded.status = "approved"
        superseded.superseded_by = "mem_x"
        expired = make("过期的")
        expired.status = "approved"
        expired.valid_until = "2020-01-01T00:00:00+08:00"
        chosen = sync.select([live, pending, superseded, expired])
        self.assertEqual([entry.body for entry in chosen], ["活着的"])

    def test_render_is_deterministic(self) -> None:
        entries = [make("乙", created="2026-11-25T10:00:00+08:00"), make("甲")]
        for entry in entries:
            entry.status = "approved"
        first = sync.render_block(entries, synced_at="2026-11-25T18:00:00+08:00")
        second = sync.render_block(list(reversed(entries)), synced_at="2026-11-25T18:00:00+08:00")
        self.assertEqual(first, second)

    def test_single_line_becomes_a_bullet_and_multi_line_keeps_structure(self) -> None:
        entry = make("一条短事实")
        entry.status = "approved"
        long_entry = make("## 小节\n\n- 细节一\n- 细节二")
        long_entry.status = "approved"
        block = sync.render_block([entry, long_entry], synced_at="2026-11-25T18:00:00+08:00")
        self.assertIn("- 一条短事实", block)
        self.assertIn("**小节**", block)
        self.assertIn("- 细节一", block)

    def test_block_is_bounded_by_markers(self) -> None:
        entry = make("内容")
        entry.status = "approved"
        block = sync.render_block([entry])
        self.assertTrue(block.startswith(managed.BEGIN))
        self.assertTrue(block.rstrip().endswith(managed.END))


class PlanAndWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.target_file = self.base / "CLAUDE.md"
        self.target_file.write_text("# 我自己的规则\n\n不要改我。\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def target(self, **kwargs) -> sync.Target:
        return sync.Target(key="t", label="测试", pattern=str(self.target_file), **kwargs)

    def test_plan_does_not_write(self) -> None:
        entry = make("内容")
        entry.status = "approved"
        before = self.target_file.read_text(encoding="utf-8")
        plans = sync.plan([entry], [self.target()])
        self.assertTrue(plans[0].changes)
        self.assertEqual(self.target_file.read_text(encoding="utf-8"), before)

    def test_missing_target_is_skipped_unless_it_creates(self) -> None:
        entry = make("内容")
        entry.status = "approved"
        absent = sync.Target(key="x", label="缺失", pattern=str(self.base / "nope.md"))
        self.assertIsNone(sync.plan([entry], [absent])[0].path)
        created = sync.Target(
            key="x", label="缺失", pattern=str(self.base / "nope.md"), creates=True
        )
        self.assertFalse(sync.plan([entry], [created])[0].exists)

    def test_write_keeps_owner_text_and_makes_a_backup(self) -> None:
        entry = make("内容")
        entry.status = "approved"
        item = sync.plan([entry], [self.target()])[0]
        path, saved = sync.write(item)
        text = path.read_text(encoding="utf-8")
        self.assertIn("不要改我。", text)
        self.assertIn("内容", text)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.read_text(encoding="utf-8"), "# 我自己的规则\n\n不要改我。\n")

    def test_second_plan_is_stable_even_across_seconds(self) -> None:
        """Re-planning an unchanged vault must not look like a change.

        The first version compared blocks containing a fresh timestamp, so every
        run rewrote every file and dropped a backup. It looked fine in tests only
        because both runs happened within the same second.
        """
        entry = make("内容")
        entry.status = "approved"
        first = sync.plan([entry], [self.target()], synced_at="2026-11-25T10:00:00+08:00")[0]
        sync.write(first)
        later = sync.plan([entry], [self.target()], synced_at="2026-11-26T11:11:11+08:00")[0]
        self.assertFalse(later.changes)
        self.assertIn("2026-11-25T10:00:00+08:00", later.after)
        self.assertNotIn("2026-11-26T11:11:11+08:00", later.after)

    def test_changed_content_updates_the_stamp(self) -> None:
        entry = make("旧内容")
        entry.status = "approved"
        sync.write(sync.plan([entry], [self.target()], synced_at="2026-11-25T10:00:00+08:00")[0])
        entry.body = "新内容"
        later = sync.plan([entry], [self.target()], synced_at="2026-11-26T11:11:11+08:00")[0]
        self.assertTrue(later.changes)
        self.assertIn("新内容", later.after)
        self.assertIn("2026-11-26T11:11:11+08:00", later.after)

    def test_write_twice_leaves_one_block(self) -> None:
        entry = make("内容")
        entry.status = "approved"
        sync.write(sync.plan([entry], [self.target()])[0])
        second = sync.plan([entry], [self.target()])[0]
        self.assertFalse(second.changes)
        self.assertEqual(second.before.count(managed.BEGIN), 1)


class EchoChamberTests(unittest.TestCase):
    """The block must never be read back as memories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.path = self.base / "CLAUDE.md"
        self.path.write_text(
            "# 我的规则\n\n## 一、身份\n\n会计学 ACCA 全英班。\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_adapter_ignores_the_managed_block(self) -> None:
        entry = make("会计学 ACCA 全英班。")
        entry.status = "approved"
        target = sync.Target(key="t", label="t", pattern=str(self.path))
        sync.write(sync.plan([entry], [target])[0])

        self.assertIn(managed.BEGIN, self.path.read_text(encoding="utf-8"))

        adapter = ClaudeAdapter(home=self.base / "claude-home", project_root=self.base)
        source = SourcePath(path=self.path, adapter="claude", source_kind="instructions")
        bodies = [parsed.body for parsed in adapter.read(source)]
        joined = "\n".join(bodies)
        self.assertNotIn("MEMORYHUB", joined)
        self.assertNotIn("长期记忆（MemoryHub 同步）", joined)
        self.assertIn("会计学 ACCA 全英班", joined)


class SyncCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.claude_md = self.base / "claude-home" / "CLAUDE.md"
        self.claude_md.parent.mkdir(parents=True)
        self.claude_md.write_text("# 我自己的规则\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        from memoryhub.cli import main

        out, err = io.StringIO(), io.StringIO()
        env = {
            "MEMORYHUB_HOME": str(self.home),
            "WORKBUDDY_HOME": str(self.base / "workbuddy-home"),
            "CLAUDE_HOME": str(self.base / "claude-home"),
            "CLAUDE_PROJECT_ROOT": str(self.base / "claude-project"),
            "CODEX_HOME": str(self.base / "codex-home"),
            "CODEX_PROJECT_ROOT": str(self.base / "codex-project"),
        }
        with mock.patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:  # argparse --help and friends
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self) -> None:
        self.run_cli("init", "--vault", str(self.vault), "--no-git")
        code, out, _err = self.run_cli("sync", "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(self.claude_md.read_text(encoding="utf-8"), "# 我自己的规则\n")

    def test_write_then_sync_again_reports_up_to_date(self) -> None:
        self.run_cli("init", "--vault", str(self.vault), "--no-git")
        first = self.run_cli("sync", "--vault", str(self.vault), "--write")
        self.assertEqual(first[0], 0, first[2])
        self.assertIn(managed.BEGIN, self.claude_md.read_text(encoding="utf-8"))
        second = self.run_cli("sync", "--vault", str(self.vault), "--write")
        self.assertEqual(second[0], 0, second[2])
        self.assertIn("全部已是最新", second[1])

    def test_unknown_target_is_refused(self) -> None:
        self.run_cli("init", "--vault", str(self.vault), "--no-git")
        code, _out, err = self.run_cli("sync", "--vault", str(self.vault), "--to", "cursor")
        self.assertEqual(code, 1)
        self.assertIn("未知目标", err)

    def test_list_shows_resolved_paths(self) -> None:
        code, out, _err = self.run_cli("sync", "--vault", str(self.vault), "--list")
        self.assertEqual(code, 0)
        self.assertIn("CLAUDE.md", out)


if __name__ == "__main__":
    unittest.main()
