"""Conflict detection and the supersede chain (P3-05).

What is asserted here is not that the model finds the right conflicts -- that
cannot be asserted without a real model. It is that everything around the model
holds: the corpus is packed without losing an entry, an invented id or an
unlocatable quote cannot produce a verdict, a proposal never becomes a write,
and the one write that does change the vault refuses every case where the naive
version loses data.

The real case that motivated this feature is reproduced in
``SupersedeTests.test_the_paper_title_case`` -- a 400-character entry saying the
title is not settled next to a 36-character entry that settles it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import conflicts  # noqa: E402
from memoryhub.ai import AIConfig, Provider, detect, parse_conflicts, pack  # noqa: E402
from memoryhub.ai.conflict import (  # noqa: E402
    MAX_ENTRIES,
    ConflictPair,
    looks_mixed,
)
from memoryhub.ai.organize import _fingerprint  # noqa: E402
from memoryhub.cli import main  # noqa: E402
from memoryhub.models import MemoryEntry, ValidationError  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


OLD_TITLE = "### 个人工作台「我的大四」\n- 毕业论文（尚未冻结）：先确定题目 → 确定企业和研究资料 → 建立研究结构"
NEW_TITLE = "毕业论文：《基于多 Agent 架构的企业财务共享中心工作流程优化研究》"


def _reply(pairs):
    body = {"pairs": pairs}
    return {
        "choices": [
            {"message": {"content": json.dumps(body, ensure_ascii=False)}}
        ]
    }


class FakeTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append(payload)
        return self.payload


def _provider(payload):
    return Provider(
        AIConfig(base_url="https://example.test/v1", model="fake"),
        transport=FakeTransport(payload),
    )


def _entry(entry_id: str, body: str, *, category: str = "project", **kwargs) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        category=category,
        body=body,
        status=kwargs.pop("status", "approved"),
        trust=kwargs.pop("trust", "review"),
        created_at=kwargs.pop("created_at", "2026-11-24T10:00:00+08:00"),
        updated_at=kwargs.pop("updated_at", "2026-11-24T10:00:00+08:00"),
        valid_from=kwargs.pop("valid_from", "2026-11-24T10:00:00+08:00"),
        **kwargs,
    )


def _bodies(*entries: MemoryEntry) -> dict:
    return {entry.id: _fingerprint(entry.body) for entry in entries}


ID_A = "mem_20261124T100000_aaaaaaaa"
ID_B = "mem_20261125T100000_bbbbbbbb"


# --------------------------------------------------------------------- pack


class PackTests(unittest.TestCase):
    def test_small_corpus_is_one_batch(self) -> None:
        entries = [_entry(ID_A, "a" * 100), _entry(ID_B, "b" * 100)]
        self.assertEqual(len(pack(entries, limit=1000)), 1)

    def test_no_entry_is_dropped_when_splitting(self) -> None:
        entries = [_entry(f"mem_2026112{i}T100000_abcdef0{i}", "字" * 300) for i in range(1, 6)]
        batches = pack(entries, limit=400)
        self.assertGreater(len(batches), 1)
        seen = [entry.id for batch in batches for entry in batch]
        self.assertEqual(sorted(seen), sorted(entry.id for entry in entries))

    def test_an_oversized_entry_still_gets_its_own_batch(self) -> None:
        # Splitting an entry in half would hand the model a fragment it would
        # then "complete" from imagination -- the failure organize.py exists for.
        entries = [_entry(ID_A, "字" * 5000), _entry(ID_B, "短")]
        batches = pack(entries, limit=100)
        self.assertIn([ID_A], [[e.id for e in batch] for batch in batches])


# ------------------------------------------------------------ the judgement


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bodies = _bodies(_entry(ID_A, OLD_TITLE), _entry(ID_B, NEW_TITLE))

    def parse(self, pairs):
        return parse_conflicts({"pairs": pairs}, self.bodies)

    def test_a_supersede_with_locatable_evidence_survives(self) -> None:
        pairs, problems = self.parse(
            [
                {
                    "a": ID_A,
                    "b": ID_B,
                    "relation": "supersedes",
                    "winner": ID_B,
                    "reason": "前者称题目未定，后者已给出确定题目",
                    "evidence": {
                        ID_A: "毕业论文（尚未冻结）：先确定题目",
                        ID_B: "《基于多 Agent 架构的企业财务共享中心工作流程优化研究》",
                    },
                }
            ]
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0].verified)
        self.assertEqual(pairs[0].winner, ID_B)

    def test_an_invented_id_is_dropped(self) -> None:
        pairs, problems = self.parse(
            [
                {
                    "a": ID_A,
                    "b": "mem_20260101T000000_deadbeef",
                    "relation": "contradicts",
                    "reason": "x",
                    "evidence": {ID_A: "毕业论文", "mem_20260101T000000_deadbeef": "y"},
                }
            ]
        )
        self.assertEqual(pairs, [])
        self.assertIn("不在本次送出", problems[0])

    def test_self_comparison_is_dropped(self) -> None:
        pairs, _ = self.parse(
            [{"a": ID_A, "b": ID_A, "relation": "contradicts", "reason": "x", "evidence": {}}]
        )
        self.assertEqual(pairs, [])

    def test_unknown_relation_is_dropped(self) -> None:
        pairs, problems = self.parse(
            [{"a": ID_A, "b": ID_B, "relation": "related", "reason": "x", "evidence": {}}]
        )
        self.assertEqual(pairs, [])
        self.assertIn("关系", problems[0])

    def test_supersedes_without_a_winner_becomes_contradicts(self) -> None:
        # A claim about which one still holds cannot be made without naming it.
        pairs, problems = self.parse(
            [
                {
                    "a": ID_A,
                    "b": ID_B,
                    "relation": "supersedes",
                    "reason": "x",
                    "evidence": {ID_A: "毕业论文（尚未冻结）", ID_B: "《基于多 Agent 架构"},
                }
            ]
        )
        self.assertEqual(pairs[0].relation, "contradicts")
        self.assertIsNone(pairs[0].winner)
        self.assertIn("winner", problems[0])

    def test_fabricated_evidence_removes_the_verdict_but_keeps_the_pair(self) -> None:
        # The pair may still be worth a human look; the *verdict* must not be.
        pairs, problems = self.parse(
            [
                {
                    "a": ID_A,
                    "b": ID_B,
                    "relation": "supersedes",
                    "winner": ID_B,
                    "reason": "x",
                    "evidence": {ID_A: "原文里没有这句话", ID_B: "也没有这句"},
                }
            ]
        )
        self.assertEqual(len(pairs), 1)
        self.assertFalse(pairs[0].verified)
        self.assertEqual(pairs[0].relation, "contradicts")
        self.assertIsNone(pairs[0].winner)
        self.assertIn("定位不到", problems[0])

    def test_evidence_keyed_by_a_and_b_is_not_trusted(self) -> None:
        pairs, _ = self.parse(
            [
                {
                    "a": ID_A,
                    "b": ID_B,
                    "relation": "contradicts",
                    "reason": "x",
                    "evidence": {"a": "毕业论文（尚未冻结）", "b": "《基于多 Agent 架构"},
                }
            ]
        )
        self.assertFalse(pairs[0].verified)

    def test_missing_reason_is_dropped(self) -> None:
        pairs, problems = self.parse(
            [{"a": ID_A, "b": ID_B, "relation": "contradicts", "evidence": {}}]
        )
        self.assertEqual(pairs, [])
        self.assertIn("reason", problems[0])


class DetectTests(unittest.TestCase):
    def test_one_call_for_a_small_corpus_and_the_prompt_carries_both_bodies(self) -> None:
        entries = [
            _entry(ID_A, OLD_TITLE, created_at="2026-11-24T10:00:00+08:00"),
            _entry(ID_B, NEW_TITLE, created_at="2026-11-25T10:00:00+08:00"),
        ]
        transport = FakeTransport(
            _reply(
                [
                    {
                        "a": ID_A,
                        "b": ID_B,
                        "relation": "supersedes",
                        "winner": ID_B,
                        "reason": "题目已定",
                        "evidence": {ID_A: "毕业论文（尚未冻结）", ID_B: "《基于多 Agent 架构"},
                    }
                ]
            )
        )
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = detect(entries, provider)
        self.assertEqual(result.calls, 1)
        self.assertGreater(result.chars_sent, 0)
        self.assertEqual(len(result.pairs), 1)

        sent = transport.calls[0]["messages"]
        self.assertIn(ID_A, sent[1]["content"])
        self.assertIn(ID_B, sent[1]["content"])
        self.assertIn("evidence", sent[0]["content"])

    def test_a_single_entry_never_calls_the_model(self) -> None:
        provider = _provider(_reply([]))
        self.assertEqual(detect([_entry(ID_A, "x")], provider).calls, 0)

    def test_too_many_entries_refuses_instead_of_truncating(self) -> None:
        entries = [_entry(f"mem_20261124T100000_{i:08x}", "x") for i in range(MAX_ENTRIES + 1)]
        with self.assertRaises(ValueError) as caught:
            detect(entries, _provider(_reply([])))
        self.assertIn("--category", str(caught.exception))


# ------------------------------------------------------------------ records


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root)
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pair(self, relation="supersedes", winner=ID_B, verified=True) -> ConflictPair:
        return ConflictPair(
            a=ID_A,
            b=ID_B,
            relation=relation,
            reason="题目已定",
            evidence={ID_A: "毕业论文（尚未冻结）", ID_B: "《基于多 Agent 架构"},
            verified=verified,
            winner=winner,
        )

    def test_path_is_independent_of_reporting_order(self) -> None:
        self.assertEqual(
            conflicts.record_path(self.root, ID_A, ID_B),
            conflicts.record_path(self.root, ID_B, ID_A),
        )

    def test_record_holds_no_copy_of_the_body(self) -> None:
        record = self.pair().to_record(model="fake")
        text = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("个人工作台", text)
        self.assertEqual(
            conflicts.save_record(self.root, record).read_text(encoding="utf-8").count("个人工作台"),
            0,
        )

    def test_round_trip_and_ordering(self) -> None:
        first = self.pair().to_record(model="fake", detected_at="2026-11-25T10:00:00+08:00")
        second = self.pair(relation="contradicts", winner=None).to_record(
            model="fake", detected_at="2026-11-25T11:00:00+08:00"
        )
        other = dict(second, pair=[ID_A, "mem_20261123T100000_cccccccc"])
        conflicts.save_record(self.root, first)
        conflicts.save_record(self.root, second)
        conflicts.save_record(self.root, other)

        records, problems = conflicts.load_records(self.root)
        self.assertEqual(problems, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["detected_at"], "2026-11-25T11:00:00+08:00")

    def test_a_broken_file_is_reported_not_skipped_silently(self) -> None:
        (self.root / "inbox" / "conflicts" / "junk.json").write_text("{", encoding="utf-8")
        records, problems = conflicts.load_records(self.root)
        self.assertEqual(records, [])
        self.assertIn("junk.json", problems[0])

    def test_a_record_without_two_ids_is_reported(self) -> None:
        # Hand-edited files exist; the listing must not crash on them.
        path = self.root / "inbox" / "conflicts" / "half.json"
        path.write_text('{"pair": ["only-one"]}', encoding="utf-8")
        records, problems = conflicts.load_records(self.root)
        self.assertEqual(records, [])
        self.assertIn("两个 id", problems[0])

    def test_proposals_are_invisible_to_the_vault(self) -> None:
        # inbox/conflicts is scanned for *.md; a proposal that looked like an
        # entry would turn doctor red for a non-problem.
        conflicts.save_record(self.root, self.pair().to_record(model="fake"))
        entries, errors = self.store.scan()
        self.assertEqual(entries, [])
        self.assertEqual(errors, [])

    def test_resolve_moves_the_proposal_and_keeps_the_decision(self) -> None:
        conflicts.save_record(self.root, self.pair().to_record(model="fake"))
        moved = conflicts.resolve_record(
            self.root, ID_A, ID_B, keep=ID_B, retired=ID_A, note="题目已定"
        )
        self.assertIsNotNone(moved)
        assert moved is not None
        self.assertTrue(moved.is_file())
        self.assertTrue(moved.as_posix().endswith("logs/conflicts/" + moved.name))
        self.assertFalse(conflicts.record_path(self.root, ID_A, ID_B).exists())
        data = json.loads(moved.read_text(encoding="utf-8"))
        self.assertEqual(data["resolution"]["keep"], ID_B)
        self.assertEqual(data["resolution"]["note"], "题目已定")

    def test_resolve_without_a_proposal_is_a_no_op(self) -> None:
        self.assertIsNone(
            conflicts.resolve_record(self.root, ID_A, ID_B, keep=ID_B, retired=ID_A)
        )


# ------------------------------------------------------------- the one write


class SupersedeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root)
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self, body: str, **kwargs) -> MemoryEntry:
        entry = MemoryEntry.create(category="project", body=body, **kwargs)
        entry.status = "approved"
        self.store.create(entry)
        return entry

    def test_the_paper_title_case(self) -> None:
        old = self.make(OLD_TITLE)
        new = self.make(NEW_TITLE)
        retired, keeper = conflicts.apply_supersede(self.store, old.id, new.id, note="题目已定")

        self.assertEqual(retired.status, "archived")
        self.assertEqual(retired.superseded_by, new.id)
        self.assertIsNotNone(retired.valid_until)
        self.assertTrue(retired.is_expired())
        self.assertIn(old.id, keeper.supersedes)

        reloaded = self.store.get(old.id)
        assert reloaded is not None
        self.assertEqual(reloaded.superseded_by, new.id)
        keeper_reloaded = self.store.get(new.id)
        assert keeper_reloaded is not None
        self.assertIn(old.id, keeper_reloaded.supersedes)
        self.assertEqual(keeper_reloaded.extra.get("supersede_note"), "题目已定")

    def test_an_unapproved_old_entry_is_refused(self) -> None:
        pending = MemoryEntry.create(category="project", body="x")
        self.store.create(pending)
        new = self.make(NEW_TITLE)
        with self.assertRaises(ValidationError):
            conflicts.apply_supersede(self.store, pending.id, new.id)

    def test_pointing_at_a_pending_replacement_is_refused(self) -> None:
        # Otherwise the old fact is archived while its replacement stays
        # invisible to every MCP client: the fact vanishes from the library.
        old = self.make(OLD_TITLE)
        pending = MemoryEntry.create(category="project", body=NEW_TITLE)
        self.store.create(pending)
        with self.assertRaises(ValidationError) as caught:
            conflicts.apply_supersede(self.store, old.id, pending.id)
        self.assertIn("不可见", str(caught.exception))

    def test_an_expired_replacement_is_refused(self) -> None:
        old = self.make(OLD_TITLE)
        new = self.make(NEW_TITLE)
        new.valid_until = "2026-01-01T00:00:00+08:00"
        self.store.save(new)
        with self.assertRaises(ValidationError):
            conflicts.apply_supersede(self.store, old.id, new.id)

    def test_self_supersede_is_refused(self) -> None:
        old = self.make(OLD_TITLE)
        with self.assertRaises(ValidationError):
            conflicts.apply_supersede(self.store, old.id, old.id)

    def test_missing_id_raises_keyerror(self) -> None:
        new = self.make(NEW_TITLE)
        with self.assertRaises(KeyError):
            conflicts.apply_supersede(self.store, "mem_20261124T100000_deadbeef", new.id)

    def test_double_supersede_is_refused(self) -> None:
        first = self.make(OLD_TITLE)
        second = self.make("中间版本")
        third = self.make(NEW_TITLE)
        conflicts.apply_supersede(self.store, first.id, second.id)
        with self.assertRaises(ValidationError) as caught:
            conflicts.apply_supersede(self.store, first.id, third.id)
        self.assertIn("已被", str(caught.exception))

    def test_a_cycle_is_refused(self) -> None:
        # Cycles cannot be created through the CLI (a superseded entry is
        # archived, so it can never be the replacement), but a hand-edited
        # frontmatter can produce one. The walk is defence in depth.
        retired = self.make("B")
        keeper = self.make("A")
        keeper.superseded_by = retired.id
        self.store.save(keeper)

        with self.assertRaises(ValidationError) as caught:
            conflicts.apply_supersede(self.store, retired.id, keeper.id)
        self.assertIn("环", str(caught.exception))

    def test_looks_mixed_flags_the_real_entry(self) -> None:
        self.assertTrue(looks_mixed(_entry(ID_A, OLD_TITLE + "\n- 目录：D:\\WorkBuddy\n- 目标：整合")))
        self.assertFalse(looks_mixed(_entry(ID_B, NEW_TITLE)))


# ---------------------------------------------------------------------- CLI


class ConflictCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "MEMORYHUB_HOME": str(self.home),
            "WORKBUDDY_HOME": str(self.base / "workbuddy"),
            "CLAUDE_HOME": str(self.base / "claude-home"),
            "CLAUDE_PROJECT_ROOT": str(self.base / "claude-project"),
            "CODEX_HOME": str(self.base / "codex-home"),
            "CODEX_PROJECT_ROOT": str(self.base / "codex-project"),
            "CHERRY_STUDIO_HOME": str(self.base / "cherry"),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MEMORYHUB_VAULT", None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def add(self, body: str) -> str:
        code, out, err = self.run_cli(
            "new", "--vault", str(self.vault), "--category", "project",
            "--body", body, "--approve",
        )
        self.assertEqual(code, 0, err)
        return out.split()[0]

    def setUpVault(self) -> None:
        code, _out, err = self.run_cli("init", "--vault", str(self.vault), "--no-git")
        self.assertEqual(code, 0, err)

    def test_empty_state_points_at_the_ai_command(self) -> None:
        self.setUpVault()
        code, out, _err = self.run_cli("conflicts", "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("没有冲突提案", out)
        self.assertIn("--ai", out)

    def test_dry_run_writes_nothing(self) -> None:
        self.setUpVault()
        old = self.add(OLD_TITLE)
        new = self.add(NEW_TITLE)
        code, out, _err = self.run_cli(
            "supersede", old, "--by", new, "--vault", str(self.vault), "--dry-run"
        )
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        store = VaultStore(self.vault)
        reloaded = store.get(old)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "approved")

    def test_non_tty_without_yes_refuses(self) -> None:
        self.setUpVault()
        old = self.add(OLD_TITLE)
        new = self.add(NEW_TITLE)
        code, _out, err = self.run_cli("supersede", old, "--by", new, "--vault", str(self.vault))
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)

    def test_supersede_then_queries_hide_the_old_entry(self) -> None:
        self.setUpVault()
        old = self.add(OLD_TITLE)
        new = self.add(NEW_TITLE)
        code, out, err = self.run_cli(
            "supersede", old, "--by", new, "--vault", str(self.vault), "--yes"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("archived", out)

        _code, listed, _err = self.run_cli("list", "--vault", str(self.vault))
        self.assertIn(new, listed)
        self.assertNotIn(old, listed)

        _code, history, _err = self.run_cli(
            "list", "--vault", str(self.vault), "--include-expired"
        )
        self.assertIn(old, history)

    def test_show_prints_the_reason_and_evidence(self) -> None:
        self.setUpVault()
        old = self.add(OLD_TITLE)
        new = self.add(NEW_TITLE)
        root = Path(self.vault)
        conflicts.save_record(
            root,
            ConflictPair(
                a=old,
                b=new,
                relation="supersedes",
                reason="前者称题目未定，后者已给出确定题目",
                evidence={old: "毕业论文（尚未冻结）", new: "《基于多 Agent 架构"},
                verified=True,
                winner=new,
            ).to_record(model="fake"),
        )
        code, out, _err = self.run_cli("conflicts", "--show", old, "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("题目未定", out)
        self.assertIn("证据（逐字）", out)
        self.assertIn(f"memoryhub supersede {old} --by {new}", out)

    def test_ai_without_configuration_exits_1(self) -> None:
        self.setUpVault()
        code, _out, err = self.run_cli("conflicts", "--ai", "--vault", str(self.vault))
        self.assertEqual(code, 1)
        self.assertIn("没有配置 AI", err)


class _ChatHandler(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible endpoint. Real HTTP, no network."""

    reply: dict = {"pairs": []}
    last_request: dict = {}

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        type(self).last_request = json.loads(self.rfile.read(length) or b"{}")
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(self.reply, ensure_ascii=False)}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class EndToEndHttpTests(unittest.TestCase):
    """The whole path: config file -> HTTP -> validation -> proposal on disk."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "vault-root"
        self.home = self.base / "home"
        self.home.mkdir()
        store = VaultStore(self.vault)
        store.init_layout()
        self.old = MemoryEntry.create(category="project", body=OLD_TITLE)
        self.new = MemoryEntry.create(category="project", body=NEW_TITLE)
        for entry in (self.old, self.new):
            entry.status = "approved"
            store.create(entry)
        self.store = store

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_proposal_written_and_vault_untouched(self) -> None:
        _ChatHandler.reply = {
            "pairs": [
                {
                    "a": self.old.id,
                    "b": self.new.id,
                    "relation": "supersedes",
                    "winner": self.new.id,
                    "reason": "题目已定",
                    "evidence": {self.old.id: "毕业论文（尚未冻结）", self.new.id: "《基于多 Agent 架构"},
                }
            ]
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            (self.home / "config.toml").write_text(
                # Quoted keys are not needed; config.load parses TOML directly.
                f'[ai]\nbase_url = "http://127.0.0.1:{port}/v1"\nmodel = "fake"\n',
                encoding="utf-8",
            )
            env = {
                "MEMORYHUB_HOME": str(self.home),
                "MEMORYHUB_API_KEY": "test-key",
            }
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(
                        ["conflicts", "--ai", "--yes", "--vault", str(self.vault)]
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(code, 0, err.getvalue())
        record_file = conflicts.record_path(self.vault, self.old.id, self.new.id)
        self.assertTrue(record_file.is_file(), out.getvalue())
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual(record["relation"], "supersedes")
        self.assertEqual(record["winner"], self.new.id)
        self.assertTrue(record["verified"])

        # The model answered; nothing in the vault may have moved.
        for entry_id in (self.old.id, self.new.id):
            current = self.store.get(entry_id)
            assert current is not None
            self.assertEqual(current.status, "approved")
            self.assertEqual(current.supersedes, [])
            self.assertIsNone(current.superseded_by)


if __name__ == "__main__":
    unittest.main()
