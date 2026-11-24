"""Adapter protocol and first real adapter (P2-03).

Run with:

    PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.adapters import build, names  # noqa: E402
from memoryhub.adapters.base import Adapter, SourcePath, bullets  # noqa: E402
from memoryhub.adapters.claude import ClaudeAdapter  # noqa: E402
from memoryhub.adapters.codex import CodexAdapter  # noqa: E402
from memoryhub.adapters.workbuddy import WorkBuddyAdapter  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

SAMPLE = """\
# User Memory Profile
> Last updated: 2026-11-15T07:52:40+08:00
> Version: 23

## Memory Block

- Prefers Python for tooling unless there is a measured performance need.
- Dislikes tooling that writes into other tools' config files without asking.

## Working Style

- Asks for the reasoning first, the code second.
"""


class DummyAdapter(Adapter):
    """Smallest possible adapter: two ordered sources, one memory each."""

    name = "dummy"
    source_kind = "doc"

    def __init__(self, paths: list[Path]):
        self._paths = paths

    def discover(self) -> list[SourcePath]:
        return [
            SourcePath(
                path=path,
                adapter=self.name,
                source_kind=self.source_kind,
                precedence=index,
                layer="project",
            )
            for index, path in enumerate(self._paths)
        ]

    def read(self, source: SourcePath) -> list[MemoryEntry]:
        return [
            MemoryEntry.create(
                category="knowledge",
                body=source.path.read_text(encoding="utf-8"),
                source_adapter=self.name,
                source_path=source.path.as_posix(),
                source_kind=self.source_kind,
                precedence=source.precedence,
                layer=source.layer,
            )
        ]


class JsonlDummyAdapter(Adapter):
    """Proves the protocol is not tied to markdown (P2-03 criterion 2)."""

    name = "dummy-jsonl"
    source_kind = "session_distill"

    def __init__(self, path: Path):
        self._path = path

    def discover(self) -> list[SourcePath]:
        return [SourcePath(path=self._path, adapter=self.name, source_kind=self.source_kind)]

    def read(self, source: SourcePath) -> list[MemoryEntry]:
        import json

        entries = []
        for line in source.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            entries.append(
                MemoryEntry.create(
                    category="knowledge",
                    body=record["learned"],
                    source_adapter=self.name,
                    source_path=source.path.as_posix(),
                    source_kind=self.source_kind,
                )
            )
        return entries


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root / "vault-root")
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_can_write_is_always_false(self) -> None:
        # ADR-002: no adapter may ever write back.
        for name in names():
            adapter = build(name)
            self.assertFalse(adapter.capabilities()["can_write"])

    def test_dummy_adapter_completes_discover_read_inbox(self) -> None:
        first = self.root / "a.md"
        second = self.root / "b.md"
        first.write_text("alpha", encoding="utf-8")
        second.write_text("beta", encoding="utf-8")
        adapter = DummyAdapter([first, second])

        discovered = adapter.discover()
        self.assertEqual([s.precedence for s in discovered], [0, 1])

        for source in discovered:
            for entry in adapter.read(source):
                self.store.create(entry)

        entries, errors = self.store.scan()
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.status == "pending" for e in entries))
        self.assertTrue(all("inbox" in p.parts for p in self.store.entry_paths()))

    def test_source_order_is_preserved_into_entries(self) -> None:
        first = self.root / "global.md"
        second = self.root / "project.md"
        first.write_text("global rule", encoding="utf-8")
        second.write_text("project rule", encoding="utf-8")
        adapter = DummyAdapter([first, second])
        entries = [adapter.read(s)[0] for s in adapter.discover()]
        self.assertEqual([e.precedence for e in entries], [0, 1])
        self.assertEqual([e.layer for e in entries], ["project", "project"])

    def test_jsonl_adapter_is_not_tied_to_markdown(self) -> None:
        path = self.root / "session.jsonl"
        path.write_text(
            '{"learned": "redis needed for tests"}\n'
            '{"learned": "pnpm preferred over npm"}\n',
            encoding="utf-8",
        )
        adapter = JsonlDummyAdapter(path)
        entries = adapter.read(adapter.discover()[0])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1].body.strip(), "pnpm preferred over npm")
        self.assertEqual(entries[0].source_kind, "session_distill")

    def test_split_quoted_metadata_skips_the_title(self) -> None:
        from memoryhub.adapters.base import strip_quoted_metadata

        meta, rest = strip_quoted_metadata(SAMPLE)
        self.assertEqual(meta["last updated"], "2026-11-15T07:52:40+08:00")
        self.assertFalse(any(line.startswith(">") for line in rest))

    def test_bullets_fold_continuation_lines(self) -> None:
        text = "## S\n\n- first line\n  continued here\n- second\n"
        self.assertEqual(bullets(text), [("S", "first line continued here"), ("S", "second")])


class WorkBuddyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "memory").mkdir(parents=True)
        self.source = self.home / "memory" / "demo_memory.md"
        self.source.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discover_finds_the_memory_file(self) -> None:
        sources = WorkBuddyAdapter(self.home).discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_kind, "auto_memory")
        self.assertEqual(sources[0].confidence, "high")

    def test_missing_directory_discovers_nothing(self) -> None:
        self.assertEqual(WorkBuddyAdapter(self.home / "absent").discover(), [])

    def test_read_produces_one_entry_per_bullet(self) -> None:
        adapter = WorkBuddyAdapter(self.home)
        entries = adapter.read(adapter.discover()[0])
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].body.strip(), "Prefers Python for tooling unless there is a measured performance need.")
        self.assertIn("working-style", entries[2].tags)

    def test_imported_entries_are_pending_review_with_provenance(self) -> None:
        adapter = WorkBuddyAdapter(self.home)
        source = adapter.discover()[0]
        entry = adapter.read(source)[0]
        self.assertEqual(entry.status, "pending")
        self.assertEqual(entry.trust, "review")  # profile -> review, ADR-003
        self.assertEqual(entry.source_kind, "auto_memory")
        self.assertEqual(entry.source_adapter, "workbuddy")
        self.assertTrue(entry.source_hash.startswith("sha256:"))
        self.assertEqual(entry.source_path, source.path.as_posix())

    def test_created_at_comes_from_the_source_header(self) -> None:
        from datetime import datetime

        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("2026-11-15T07:52:40"))
        # Contract: stored times always carry a timezone.
        self.assertIsNotNone(datetime.fromisoformat(entry.created_at).tzinfo)

    def test_invalid_header_timestamp_falls_back_to_now(self) -> None:
        self.source.write_text(
            "# User Memory Profile\n> Last updated: not-a-date\n\n## Memory Block\n\n- one\n",
            encoding="utf-8",
        )
        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("20"))

    def test_bom_does_not_hide_the_header(self) -> None:
        # Windows editors write a BOM; without stripping it the title line does
        # not match and the `> Last updated:` header is lost.
        self.source.write_text("\ufeff" + SAMPLE, encoding="utf-8")
        adapter = WorkBuddyAdapter(self.home)
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertTrue(entry.created_at.startswith("2026-11-15T07:52:40"))

    # ---------------------------------------------------- persona directory

    def write_persona(self) -> None:
        persona = self.home / "user-abc123-personal"
        persona.mkdir(parents=True, exist_ok=True)
        (persona / "IDENTITY.md").write_text(
            "# 身份\n\n## 角色\n\n独立开发者，做本地优先工具。\n", encoding="utf-8"
        )
        (persona / "USER.md").write_text(
            "# 用户\n\n## 偏好\n\n- 喜欢先看推理再看代码\n", encoding="utf-8"
        )
        (persona / "SOUL.md").write_text("# SOUL\n\n## 风格\n\n- 直接\n", encoding="utf-8")
        (persona / "MEMORY.md").write_text(
            "# 长期事实\n\n## 硬约束\n\n- 不自动加依赖\n- 不在公开仓贴绝对路径\n",
            encoding="utf-8",
        )

    def test_persona_directory_is_discovered_as_profile(self) -> None:
        self.write_persona()
        sources = {s.path.name: s for s in WorkBuddyAdapter(self.home).discover()}
        self.assertEqual(sources["IDENTITY.md"].source_kind, "profile")
        self.assertEqual(sources["USER.md"].source_kind, "profile")
        self.assertEqual(sources["SOUL.md"].source_kind, "profile")
        self.assertEqual(sources["MEMORY.md"].source_kind, "auto_memory")
        self.assertEqual(sources["demo_memory.md"].source_kind, "auto_memory")

    def test_persona_files_split_into_sections_and_bullets(self) -> None:
        self.write_persona()
        adapter = WorkBuddyAdapter(self.home)
        by_name = {s.path.name: s for s in adapter.discover()}

        identity = adapter.read(by_name["IDENTITY.md"])
        self.assertEqual(len(identity), 2)  # title + one section
        self.assertTrue(all(e.category == "profile" for e in identity))
        self.assertTrue(all(e.trust == "review" for e in identity))
        self.assertTrue(all(e.source_hash.startswith("sha256:") for e in identity))

        index = adapter.read(by_name["MEMORY.md"])
        self.assertEqual(len(index), 2, "persona MEMORY.md is a bullet list of facts")
        self.assertTrue(all(e.source_kind == "auto_memory" for e in index))

    def test_persona_and_profile_do_not_collide(self) -> None:
        self.write_persona()
        adapter = WorkBuddyAdapter(self.home)
        seen = set()
        for source in adapter.discover():
            for entry in adapter.read(source):
                key = (entry.source_path, entry.body)
                self.assertNotIn(key, seen)
                seen.add(key)


class ClaudeAdapterTests(unittest.TestCase):
    """Fixtures are built from the documented layout, not from a real install."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "claude-home"
        self.project = self.root / "project"
        (self.project / ".claude" / "rules").mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def adapter(self) -> ClaudeAdapter:
        return ClaudeAdapter(home=self.home, project_root=self.project)

    def test_discover_covers_user_project_and_local_layers(self) -> None:
        self.write("claude-home/CLAUDE.md", "# User\n> prefers pnpm\n")
        self.write("claude-home/rules/preferences.md", "- use tabs\n")
        self.write("project/CLAUDE.md", "# Project\n\n## Rules\n\n- run tests\n")
        self.write("project/.claude/rules/testing.md", "---\npaths: [\"**/*.py\"]\n---\n- pytest\n")
        self.write("project/CLAUDE.local.md", "- local sandbox url\n")

        sources = self.adapter().discover()
        layers = [(s.layer, s.precedence) for s in sources]
        self.assertEqual(
            layers,
            [
                ("global", 0),
                ("global", 1),
                ("project", 10),
                ("project", 11),
                ("local", 20),
            ],
        )
        self.assertTrue(all(s.source_kind == "instruction" for s in sources))

    def test_project_claude_dir_is_used_when_root_file_is_absent(self) -> None:
        self.write("project/.claude/CLAUDE.md", "## Only here\n\n- x\n")
        sources = [s for s in self.adapter().discover() if s.layer == "project"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].path.name, "CLAUDE.md")

    def test_missing_everything_is_not_an_error(self) -> None:
        self.assertEqual(self.adapter().discover(), [])

    def test_instruction_file_splits_into_sections_without_fence_damage(self) -> None:
        self.write(
            "project/CLAUDE.md",
            "# Project\n\n## 编码约定\n\n- 2 spaces\n\n"
            "## 命令\n\n```bash\n# not a heading\npnpm test\n```\n",
        )
        adapter = self.adapter()
        entries = adapter.read([s for s in adapter.discover() if s.layer == "project"][0])
        self.assertEqual(len(entries), 3)  # title + two sections
        # CJK headings must survive slugging; otherwise the tag is silently dropped.
        self.assertIn("编码约定", [e.tags[-1] for e in entries])
        commands = [e for e in entries if "pnpm test" in e.body][0]
        self.assertIn("# not a heading", commands.body)

    def test_instruction_entries_need_review(self) -> None:
        self.write("project/CLAUDE.md", "## Rule\n\n- never force push\n")
        adapter = self.adapter()
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertEqual(entry.source_kind, "instruction")
        # Project-scope instruction file -> a standing rule.
        self.assertEqual(entry.category, "decision")
        self.assertEqual(entry.trust, "review")
        self.assertEqual(entry.status, "pending")
        # Provenance must include the content hash: "which version of that file".
        self.assertTrue(entry.source_hash.startswith("sha256:"))

    def test_auto_memory_maps_type_and_modified(self) -> None:
        self.write(
            "claude-home/projects/D--project-demo/memory/feedback_testing.md",
            "---\nname: feedback_testing\ntype: feedback\n"
            "modified: 2026-11-20T10:00:00+08:00\n---\n\nAlways use pnpm.\n",
        )
        self.write(
            "claude-home/projects/D--project-demo/memory/MEMORY.md",
            "# Index\n\n- feedback_testing: use pnpm\n",
        )
        adapter = self.adapter()
        memory_sources = [s for s in adapter.discover() if s.source_kind == "auto_memory"]
        self.assertEqual(len(memory_sources), 1, "MEMORY.md must be treated as an index")
        entry = adapter.read(memory_sources[0])[0]
        self.assertEqual(entry.category, "preference")  # feedback -> preference
        self.assertEqual(entry.source_kind, "auto_memory")
        self.assertTrue(entry.created_at.startswith("2026-11-20T10:00:00"))
        self.assertIn("feedback", entry.tags)
        self.assertTrue(any(tag.startswith("claude-project-") for tag in entry.tags))

    def test_global_claude_md_is_filed_as_profile(self) -> None:
        # Claude Code writes what it learned about the user into the *user-scope*
        # CLAUDE.md, so that layer is a profile, not a project rule.
        self.write("claude-home/CLAUDE.md", "# 用户信息\n\n- 独立开发者\n")
        adapter = self.adapter()
        sources = {s.layer: s for s in adapter.discover()}
        entry = adapter.read(sources["global"])[0]
        self.assertEqual(entry.category, "profile")
        self.assertEqual(entry.source_kind, "instruction")
        self.assertEqual(entry.trust, "review")

    def test_unknown_type_falls_back_to_knowledge(self) -> None:
        self.write(
            "claude-home/projects/p/memory/odd.md",
            "---\ntype: something-new\n---\n\nA fact.\n",
        )
        adapter = self.adapter()
        entry = adapter.read(adapter.discover()[0])[0]
        self.assertEqual(entry.category, "knowledge")

    def test_oversized_instruction_file_is_skipped(self) -> None:
        from memoryhub.adapters import claude as claude_module

        self.write("project/CLAUDE.md", "x" * 2048)
        original = claude_module.MAX_INSTRUCTION_BYTES
        claude_module.MAX_INSTRUCTION_BYTES = 1024
        try:
            sources = self.adapter().discover()
        finally:
            claude_module.MAX_INSTRUCTION_BYTES = original
        self.assertEqual(sources, [])


class CodexAdapterTests(unittest.TestCase):
    """Chain semantics verified against codex-cli 0.147.0 (see docs/04 §2.1)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "codex-home"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def adapter(self, cwd: Optional[Path] = None) -> CodexAdapter:
        return CodexAdapter(
            home=self.home, project_root=self.project, cwd=cwd or self.project
        )

    def test_global_agents_md_is_the_weakest_layer(self) -> None:
        self.write("codex-home/AGENTS.md", "# Global\n\n- prefer pnpm\n")
        sources = self.adapter().discover()
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0].layer, sources[0].precedence), ("global", 0))

    def test_override_shadows_agents_md_in_the_same_directory(self) -> None:
        # Verified empirically: the shadowed AGENTS.md is not loaded at all.
        self.write("project/AGENTS.md", "- shadowed rule\n")
        self.write("project/AGENTS.override.md", "- winning rule\n")
        sources = self.adapter().discover()
        self.assertEqual([s.path.name for s in sources], ["AGENTS.override.md"])

    def test_root_and_nested_chain_keep_order(self) -> None:
        self.write("project/AGENTS.md", "# Root\n\n- root rule\n")
        self.write("project/sub/AGENTS.md", "# Sub\n\n- sub rule\n")
        sources = self.adapter(cwd=self.project / "sub").discover()
        self.assertEqual([s.layer for s in sources], ["project", "nested"])
        self.assertEqual([s.precedence for s in sources], [1, 2])

    def test_fallback_names_come_from_config(self) -> None:
        self.write(
            "codex-home/config.toml",
            'project_doc_fallback_filenames = ["TEAM_GUIDE.md"]\n',
        )
        self.write("project/TEAM_GUIDE.md", "- team rule\n")
        sources = self.adapter().discover()
        self.assertEqual([s.path.name for s in sources], ["TEAM_GUIDE.md"])

    def test_combined_size_cap_stops_the_chain(self) -> None:
        self.write("codex-home/config.toml", "project_doc_max_bytes = 64\n")
        self.write("project/AGENTS.md", "x" * 100 + "\n")
        self.write("project/sub/AGENTS.md", "- never reached\n")
        sources = self.adapter(cwd=self.project / "sub").discover()
        self.assertEqual(sources, [])

    def test_empty_files_are_ignored(self) -> None:
        self.write("project/AGENTS.md", "")
        self.assertEqual(self.adapter().discover(), [])

    def test_instruction_entries_need_review(self) -> None:
        self.write(
            "project/AGENTS.md",
            "# Rules\n\n- never force push\n\n"
            "## Commands\n\n```bash\n# not a heading\npnpm test\n```\n",
        )
        adapter = self.adapter()
        entries = adapter.read(adapter.discover()[0])
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.source_kind == "instruction" for e in entries))
        self.assertTrue(all(e.category == "decision" for e in entries))
        self.assertTrue(all(e.trust == "review" for e in entries))
        self.assertTrue(all(e.source_hash.startswith("sha256:") for e in entries))
        self.assertIn("# not a heading", entries[1].body)


if __name__ == "__main__":
    unittest.main()
