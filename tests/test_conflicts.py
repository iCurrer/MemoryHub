"""Claim-level judgement, the proposal on disk, and the one write (P3-05).

What is asserted here is not that the model finds the right conflicts -- that
cannot be asserted without one. It is that everything around the model holds:
each call carries exactly one claim pair, an invented quote cannot produce a
verdict, a direction claim needs evidence from the claim it points at, a partial
conflict is labelled instead of silently becoming a whole-entry archive, and the
one write in the workflow refuses every case where the naive version loses data.

The real case that motivated the rewrite is reproduced in
``JudgeNarrowingTests.test_no_unrelated_entry_reaches_the_prompt`` -- eight
entries of context were enough to erase the paper-title conflict on the real
vault, so the guard is structural, not prompt-level.
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
from memoryhub.ai import (  # noqa: E402
    AIConfig,
    Claim,
    ClaimVerdict,
    ConflictPair,
    Provider,
    aggregate,
    detect,
    judge,
    parse_verdict,
)
from memoryhub.ai.claims import Candidate, collect  # noqa: E402
from memoryhub.ai.conflict import looks_mixed  # noqa: E402
from memoryhub.cli import main  # noqa: E402
from memoryhub.models import MemoryEntry, ValidationError  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

OLD_TITLE = "### 个人工作台「我的大四」\n- 毕业论文（尚未冻结）：先确定题目 → 确定企业和研究资料 → 建立研究结构"
NEW_TITLE = "毕业论文：《基于多 Agent 架构的企业财务共享中心工作流程优化研究》"

ID_A = "mem_20261125T155817_782bcbcf"
ID_B = "mem_20261124T205154_e6e751a1"
ID_C = "mem_20261124T205154_cccccccc"


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


def _claim(entry_id: str, index: int, text: str) -> Claim:
    return Claim(entry_id=entry_id, index=index, text=text)


def _body(reply: object) -> dict:
    """An OpenAI-shaped response carrying ``reply`` as the answer."""
    return {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}


class ScriptedTransport:
    """Returns queued answers in order and records every request."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append(payload)
        reply = self.replies.pop(0) if self.replies else {"relation": "none"}
        return _body(reply)


def _provider(*replies: object) -> Provider:
    return Provider(
        AIConfig(base_url="https://example.test/v1", model="fake"),
        transport=ScriptedTransport(*replies),
    )


SUPERSEDE_ANSWER = {
    "relation": "supersede",
    "newer": "b",
    "quote_a": "毕业论文（尚未冻结）：先确定题目",
    "quote_b": "《基于多 Agent 架构的企业财务共享中心工作流程优化研究》",
    "direction": "基于多 Agent 架构",
    "reason": "前者称题目尚未冻结，后者已给出确定题目",
}


# ------------------------------------------------------------------ parsing


class ParseVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = Candidate(
            left=_claim(ID_A, 1, "毕业论文（尚未冻结）：先确定题目"),
            right=_claim(ID_B, 0, NEW_TITLE),
            score=0.9,
        )

    def parse(self, payload: object):
        return parse_verdict(payload, self.candidate)

    def test_a_supported_supersede_becomes_a_direction(self) -> None:
        verdict, problems = self.parse(SUPERSEDE_ANSWER)
        assert verdict is not None
        self.assertEqual(problems, [])
        self.assertTrue(verdict.verified)
        self.assertEqual(verdict.relation, "supersede")
        self.assertEqual(verdict.winner_entry, ID_B)

    def test_none_is_a_normal_answer(self) -> None:
        verdict, problems = self.parse({"relation": "none", "reason": "各说各的"})
        assert verdict is not None
        self.assertEqual(problems, [])
        self.assertEqual(verdict.relation, "none")
        self.assertIsNone(verdict.winner_entry)

    def test_the_plural_relation_from_version_one_records_is_accepted(self) -> None:
        # Old proposals on disk say "supersedes"; a hand-written answer using it
        # must not be rejected for a spelling the project used to require.
        verdict, _ = self.parse(dict(SUPERSEDE_ANSWER, relation="supersedes"))
        assert verdict is not None
        self.assertEqual(verdict.relation, "supersede")

    def test_the_singular_contradict_is_accepted(self) -> None:
        # Measured: on the paper-title pair the model answered "contradict" and
        # the module discarded a correct judgement over one letter. A validator
        # that manufactures false negatives is worse than a lenient one.
        verdict, problems = self.parse(
            {
                "relation": "contradict",
                "quote_a": "毕业论文（尚未冻结）：先确定题目",
                "quote_b": "《基于多 Agent 架构",
                "reason": "一个说题目没定，一个给出了题目",
            }
        )
        assert verdict is not None
        self.assertEqual(problems, [])
        self.assertEqual(verdict.relation, "contradicts")
        self.assertTrue(verdict.verified)

    def test_a_quote_from_outside_the_claim_does_not_count(self) -> None:
        # The whole point of claim granularity: a quote that only exists
        # elsewhere in the entry means the model compared something other than
        # the two fragments it was asked about.
        verdict, problems = self.parse(
            dict(SUPERSEDE_ANSWER, quote_a="个人工作台「我的大四」")
        )
        assert verdict is not None
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.relation, "contradicts")
        self.assertIsNone(verdict.winner_entry)
        self.assertIn("定位不到", problems[0])

    def test_an_invented_quote_downgrades_instead_of_vanishing(self) -> None:
        # The pair is still worth a human look; the verdict is not.
        verdict, _ = self.parse(
            {"relation": "contradicts", "quote_a": "原文里没有这句", "quote_b": "也没有这句"}
        )
        assert verdict is not None
        self.assertFalse(verdict.verified)
        self.assertEqual(verdict.relation, "contradicts")

    def test_a_supersede_without_a_direction_evidence_becomes_contradicts(self) -> None:
        # Direction is the field that used to be guessed from ``created_at``.
        # Without wording that proves precedence, guessing is exactly the error.
        verdict, problems = self.parse(
            {k: v for k, v in SUPERSEDE_ANSWER.items() if k != "direction"}
        )
        assert verdict is not None
        self.assertEqual(verdict.relation, "contradicts")
        self.assertIsNone(verdict.winner_entry)
        self.assertIn("没说清哪条更新", problems[0])

    def test_direction_evidence_must_come_from_the_newer_side(self) -> None:
        verdict, problems = self.parse(
            dict(SUPERSEDE_ANSWER, direction="毕业论文（尚未冻结）")
        )
        assert verdict is not None
        self.assertEqual(verdict.relation, "contradicts")
        self.assertIn("不在被判为较新的那段", problems[0])

    def test_newer_outside_a_and_b_is_refused(self) -> None:
        verdict, _ = self.parse(dict(SUPERSEDE_ANSWER, newer="c"))
        assert verdict is not None
        self.assertEqual(verdict.relation, "contradicts")

    def test_an_unknown_relation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.parse({"relation": "related"})

    def test_a_non_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.parse(["not", "an", "object"])


# ---------------------------------------------------------------- narrowing


class JudgeNarrowingTests(unittest.TestCase):
    """The structural guard against the failure measured on the real vault."""

    def test_no_unrelated_entry_reaches_the_prompt(self) -> None:
        entries = {
            ID_A: _entry(ID_A, OLD_TITLE),
            ID_B: _entry(ID_B, NEW_TITLE),
            ID_C: _entry(ID_C, "周末去拍了外滩的日落，用了 35mm 定焦镜头"),
        }
        candidate = Candidate(
            left=_claim(ID_A, 1, "毕业论文（尚未冻结）：先确定题目"),
            right=_claim(ID_B, 0, NEW_TITLE),
            score=0.9,
        )
        transport = ScriptedTransport({"relation": "none"})
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            judge(candidate, entries, provider)

        self.assertEqual(len(transport.calls), 1)
        prompt = transport.calls[0]["messages"][1]["content"]
        self.assertIn("毕业论文（尚未冻结）", prompt)
        self.assertIn("基于多 Agent 架构", prompt)
        self.assertNotIn("外滩", prompt)
        self.assertNotIn(ID_C, prompt)

    def test_the_recording_date_is_not_sent(self) -> None:
        # ``created_at`` is when the memory was written down, not when the fact
        # happened. Sending it is what made the model call a late-recorded old
        # note "newer". The prompt must not contain it at all.
        entries = {
            ID_A: _entry(ID_A, OLD_TITLE, created_at="2026-11-25T15:58:17+08:00"),
            ID_B: _entry(ID_B, NEW_TITLE, created_at="2026-11-24T20:51:54+08:00"),
        }
        candidate = Candidate(
            left=_claim(ID_A, 1, "毕业论文（尚未冻结）：先确定题目"),
            right=_claim(ID_B, 0, NEW_TITLE),
            score=0.9,
        )
        transport = ScriptedTransport({"relation": "none"})
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            judge(candidate, entries, provider)
        prompt = transport.calls[0]["messages"][1]["content"]
        self.assertNotIn("2026-11-25", prompt)
        self.assertNotIn("2026-11-24", prompt)

    def test_the_system_prompt_forbids_inferring_newer_from_dates(self) -> None:
        # Belt and braces: even if a date leaks in from a body, the rule is
        # stated. A test on prose is weak, but this rule was violated twice.
        from memoryhub.ai.conflict import JUDGE_SYSTEM

        self.assertIn("录入时间", JUDGE_SYSTEM)
        self.assertIn("不要用日期判断谁更新", JUDGE_SYSTEM)
        self.assertIn("none", JUDGE_SYSTEM)


# ---------------------------------------------------------------- aggregate


class AggregateTests(unittest.TestCase):
    def verdict(self, relation: str, *, left=1, right=0, verified=True, winner=None):
        return ClaimVerdict(
            left=_claim(ID_A, left, "毕业论文（尚未冻结）：先确定题目"),
            right=_claim(ID_B, right, NEW_TITLE),
            relation=relation,
            reason="题目已定",
            quotes={
                f"{ID_A}#{left}": "毕业论文（尚未冻结）",
                f"{ID_B}#{right}": "《基于多 Agent 架构",
            },
            verified=verified,
            winner_entry=winner,
        )

    def test_none_verdicts_produce_no_proposal(self) -> None:
        self.assertEqual(aggregate([self.verdict("none")], {ID_A: 1, ID_B: 1}), [])

    def test_the_strongest_verdict_wins(self) -> None:
        pairs = aggregate(
            [self.verdict("duplicate"), self.verdict("supersede", winner=ID_B)],
            {ID_A: 2, ID_B: 3},
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].relation, "supersede")
        self.assertEqual(pairs[0].winner, ID_B)

    def test_a_partial_conflict_is_labelled_partial(self) -> None:
        # The summary-entry failure: if only one of five claims is stale, the
        # proposal must say so, because archiving the entry would hide the other
        # four from every query.
        pairs = aggregate([self.verdict("supersede", winner=ID_B)], {ID_A: 5, ID_B: 1})
        self.assertEqual(pairs[0].scope, "partial")

    def test_a_whole_conflict_is_not_labelled_partial(self) -> None:
        pairs = aggregate([self.verdict("contradicts")], {ID_A: 1, ID_B: 1})
        self.assertEqual(pairs[0].scope, "whole")

    def test_an_unverified_verdict_loses_its_direction(self) -> None:
        pairs = aggregate(
            [self.verdict("supersede", verified=False, winner=ID_B)], {ID_A: 1, ID_B: 1}
        )
        self.assertEqual(pairs[0].relation, "contradicts")
        self.assertIsNone(pairs[0].winner)

    def test_the_proposal_keeps_claim_level_detail(self) -> None:
        pairs = aggregate([self.verdict("contradicts")], {ID_A: 1, ID_B: 1})
        claim = pairs[0].claims[0]
        self.assertEqual(claim["left"], f"{ID_A}#1")
        self.assertEqual(claim["right"], f"{ID_B}#0")
        self.assertEqual(claim["quote_left"], "毕业论文（尚未冻结）")


# ------------------------------------------------------------------- detect


class DetectTests(unittest.TestCase):
    def entries(self):
        return [_entry(ID_A, OLD_TITLE), _entry(ID_B, NEW_TITLE)]

    def test_one_pair_one_call(self) -> None:
        transport = ScriptedTransport(SUPERSEDE_ANSWER)
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = detect(self.entries(), provider, max_pairs=1)
        self.assertEqual(result.calls, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(result.pairs), 1)
        self.assertEqual(result.pairs[0].relation, "supersede")
        self.assertGreater(result.chars_sent, 0)

    def test_a_single_entry_never_calls_the_model(self) -> None:
        provider = _provider({"relation": "none"})
        self.assertEqual(detect([_entry(ID_A, OLD_TITLE)], provider).calls, 0)

    def test_the_planned_hook_can_cancel_before_spending_anything(self) -> None:
        seen = {}

        def planned(stats, count: int) -> bool:
            seen["selected"] = stats.selected
            seen["count"] = count
            return False

        transport = ScriptedTransport(SUPERSEDE_ANSWER)
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = detect(self.entries(), provider, on_planned=planned)
        self.assertEqual(transport.calls, [])
        self.assertEqual(result.calls, 0)
        self.assertEqual(result.pairs, [])
        self.assertGreater(seen["selected"], 0)
        self.assertIn("取消", result.problems[0])

    def test_progress_is_reported_for_every_pair(self) -> None:
        seen = []
        provider = _provider({"relation": "none"}, {"relation": "none"}, {"relation": "none"})
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            detect(self.entries(), provider, max_pairs=3,
                   on_progress=lambda done, total, verdict: seen.append((done, total)))
        self.assertEqual(seen[0][0], 1)
        self.assertEqual(seen[-1][0], len(seen))

    def test_repeated_transport_failure_stops_instead_of_burning_the_rest(self) -> None:
        from memoryhub.ai.provider import AIError

        class Broken:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, payload, headers, timeout):
                self.calls += 1
                raise AIError("boom")

        transport = Broken()
        provider = Provider(
            AIConfig(base_url="https://example.test/v1", model="fake"), transport=transport
        )
        # Three similar entries produce enough pairs for the budget to trip.
        corpus = self.entries() + [
            _entry(ID_C, "毕业论文题目是财务共享中心流程优化研究")
        ]
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = detect(corpus, provider, max_pairs=20)
        self.assertLessEqual(transport.calls, 3)
        self.assertTrue(any("中止" in problem for problem in result.problems))

    def test_a_bad_answer_does_not_abort_the_run(self) -> None:
        provider = _provider({"relation": "nonsense"}, {"relation": "none"})
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = detect(self.entries(), provider, max_pairs=2)
        self.assertEqual(result.calls, 1)
        self.assertTrue(any("relation" in problem for problem in result.problems))


# ------------------------------------------------------------------ records


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = VaultStore(self.root)
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pair(self, relation="supersede", winner=ID_B, verified=True) -> ConflictPair:
        return ConflictPair(
            a=ID_A,
            b=ID_B,
            relation=relation,
            reason="题目已定",
            evidence={ID_A: "毕业论文（尚未冻结）", ID_B: "《基于多 Agent 架构"},
            verified=verified,
            winner=winner,
            scope="partial",
            claims=[
                {
                    "left": f"{ID_A}#1",
                    "right": f"{ID_B}#0",
                    "relation": relation,
                    "verified": verified,
                    "quote_left": "毕业论文（尚未冻结）",
                    "quote_right": "《基于多 Agent 架构",
                    "reason": "题目已定",
                }
            ],
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

    def test_record_carries_the_scope_and_the_claim_detail(self) -> None:
        record = self.pair().to_record(model="fake")
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["scope"], "partial")
        self.assertEqual(record["claims"][0]["left"], f"{ID_A}#1")

    def test_round_trip_and_ordering(self) -> None:
        first = self.pair().to_record(model="fake", detected_at="2026-11-25T10:00:00+08:00")
        second = self.pair(relation="contradicts", winner=None).to_record(
            model="fake", detected_at="2026-11-25T11:00:00+08:00"
        )
        other = dict(second, pair=[ID_A, ID_C])
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
        self.assertTrue(looks_mixed(_entry(ID_A, OLD_TITLE + "\n- 目录：D:/WorkBuddy\n- 目标：整合")))
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

    def test_show_prints_the_quotes_and_the_command(self) -> None:
        self.setUpVault()
        old = self.add(OLD_TITLE)
        new = self.add(NEW_TITLE)
        root = Path(self.vault)
        conflicts.save_record(
            root,
            ConflictPair(
                a=old,
                b=new,
                relation="supersede",
                reason="前者称题目未定，后者已给出确定题目",
                evidence={old: "毕业论文（尚未冻结）", new: "《基于多 Agent 架构"},
                verified=True,
                winner=new,
                scope="partial",
                claims=[
                    {
                        "left": f"{old}#1",
                        "right": f"{new}#0",
                        "relation": "supersede",
                        "verified": True,
                        "quote_left": "毕业论文（尚未冻结）：先确定题目",
                        "quote_right": "《基于多 Agent 架构",
                        "reason": "题目已定",
                    }
                ],
            ).to_record(model="fake"),
        )
        code, out, _err = self.run_cli("conflicts", "--show", old, "--vault", str(self.vault))
        self.assertEqual(code, 0)
        self.assertIn("题目未定", out)
        self.assertIn("冲突的片段", out)
        self.assertIn("毕业论文（尚未冻结）：先确定题目", out)
        self.assertIn("只有部分片段冲突", out)
        self.assertIn(f"memoryhub supersede {old} --by {new}", out)

    def test_ai_without_configuration_exits_1(self) -> None:
        self.setUpVault()
        code, _out, err = self.run_cli("conflicts", "--ai", "--vault", str(self.vault))
        self.assertEqual(code, 1)
        self.assertIn("没有配置 AI", err)

    def test_ai_prints_the_pairing_cut_before_sending(self) -> None:
        """Declining must happen before any call, and the cut must be visible."""
        self.setUpVault()
        self.add(OLD_TITLE)
        self.add(NEW_TITLE)
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(
            '[ai]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "fake"\n', encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            code, out, err = self.run_cli(
                "conflicts", "--ai", "--vault", str(self.vault), "--max-pairs", "3"
            )
        self.assertEqual(code, 1)
        self.assertIn("段对", out)
        self.assertIn("--yes", err)


class _ChatHandler(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible endpoint. Real HTTP, no network."""

    reply: dict = {"relation": "none"}
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
        _ChatHandler.reply = {"relation": "none"}
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

    def run_against_server(self, argv) -> tuple:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            (self.home / "config.toml").write_text(
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
                    code = main(argv)
            return code, out.getvalue(), err.getvalue()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_proposal_written_and_vault_untouched(self) -> None:
        _ChatHandler.reply = {
            "relation": "supersede",
            "newer": "b",
            "quote_a": "毕业论文（尚未冻结）：先确定题目",
            "quote_b": "《基于多 Agent 架构的企业财务共享中心工作流程优化研究》",
            "direction": "基于多 Agent 架构",
            "reason": "题目已定",
        }
        code, out, err = self.run_against_server(
            ["conflicts", "--ai", "--yes", "--max-pairs", "1", "--vault", str(self.vault)]
        )
        self.assertEqual(code, 0, err)
        record_file = conflicts.record_path(self.vault, self.old.id, self.new.id)
        self.assertTrue(record_file.is_file(), out)
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual(record["relation"], "supersede")
        self.assertTrue(record["verified"])
        self.assertEqual(record["scope"], "partial")

        # The request must carry the two claims and nothing about dates.
        prompt = _ChatHandler.last_request["messages"][1]["content"]
        self.assertIn("毕业论文（尚未冻结）", prompt)
        self.assertIn("基于多 Agent 架构", prompt)
        self.assertNotIn("2026-", prompt)

        # The model answered; nothing in the vault may have moved.
        for entry_id in (self.old.id, self.new.id):
            current = self.store.get(entry_id)
            assert current is not None
            self.assertEqual(current.status, "approved")
            self.assertEqual(current.supersedes, [])
            self.assertIsNone(current.superseded_by)


if __name__ == "__main__":
    unittest.main()
