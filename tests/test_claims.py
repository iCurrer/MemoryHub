"""Splitting entries into claims, and pairing claims worth comparing.

These tests exist because the previous version compared whole entries, and that
choice -- not the model -- produced the two real failures on record: a summary
entry reported as six duplicates, and the paper-title conflict missing. So what
is asserted here is the shape of the comparison unit and the visibility of the
pairing cut, not any particular similarity score.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.ai.claims import (  # noqa: E402
    MIN_CLAIM_CHARS,
    Candidate,
    Claim,
    candidates,
    collect,
    split_claims,
)
from memoryhub.models import MemoryEntry  # noqa: E402

SUMMARY = """### 个人工作台「我的大四」
- 目录：D:/WorkBuddy、D:/2026_毕业论文_会计学
- 目标：整合学业、毕业论文、求职、个人项目和 Agent
- 毕业论文（尚未冻结）：先确定题目 → 确定企业和研究资料 → 建立研究结构
- 使用偏好：本地记忆、IMA、文件化管理、尽量不依赖 Notion
- 已有管理文件：PROJECT_STATE、RESEARCH_BASELINE、DECISION_LOG"""

TITLE = "毕业论文：《基于多 Agent 架构的企业财务共享中心工作流程优化研究》"


def entry(entry_id: str, body: str, *, category: str = "project") -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        category=category,
        body=body,
        status="approved",
        created_at="2026-11-24T10:00:00+08:00",
        updated_at="2026-11-24T10:00:00+08:00",
        valid_from="2026-11-24T10:00:00+08:00",
    )


class SplitTests(unittest.TestCase):
    def test_bullets_headings_and_arrows_do_not_glue_claims_together(self) -> None:
        pieces = split_claims(SUMMARY)
        self.assertIn("毕业论文（尚未冻结）：先确定题目", pieces)
        self.assertIn("确定企业和研究资料", pieces)
        self.assertIn("建立研究结构", pieces)
        # The bullet marker and the "###" must not survive into the claim: the
        # model would quote them and the quote test would still pass, wasting the
        # one check that catches invented evidence.
        for piece in pieces:
            self.assertFalse(piece.startswith(("-", "#", "*")))

    def test_a_one_sentence_entry_is_one_claim(self) -> None:
        self.assertEqual(split_claims(TITLE), [TITLE])

    def test_fragments_below_the_floor_are_dropped(self) -> None:
        # "目录：" alone is a heading stub; comparing it to anything is noise.
        pieces = split_claims("目录：\n- 目标：\n这是一个足够长的正经断言内容")
        self.assertEqual(pieces, ["这是一个足够长的正经断言内容"])

    def test_an_entry_of_only_fragments_falls_back_to_the_whole_body(self) -> None:
        # Dropping every piece would remove the entry from every comparison
        # without saying so. A short entry is still a claim.
        body = "- 甲\n- 乙\n- 丙\n- 丁\n- 戊\n- 己\n- 庚"
        self.assertEqual(split_claims(body), [body.strip()])

    def test_empty_body_yields_no_claims(self) -> None:
        self.assertEqual(split_claims("   \n\n- \n"), [])


class CollectTests(unittest.TestCase):
    def test_keys_are_stable_and_prefixed_by_entry(self) -> None:
        claims = collect([entry("mem_a", SUMMARY), entry("mem_b", TITLE)])
        self.assertTrue(all(claim.key.startswith(claim.entry_id + "#") for claim in claims))
        again = collect([entry("mem_a", SUMMARY), entry("mem_b", TITLE)])
        self.assertEqual([c.key for c in claims], [c.key for c in again])

    def test_an_entry_without_claims_contributes_nothing(self) -> None:
        claims = collect([entry("mem_a", " \n "), entry("mem_b", TITLE)])
        self.assertEqual([claim.entry_id for claim in claims], ["mem_b"])


class CandidateTests(unittest.TestCase):
    def test_claims_from_the_same_entry_are_never_paired(self) -> None:
        # Two claims of one entry disagreeing is a splitting problem, not a
        # conflict: reporting it would ask the reader to arbitrate a single
        # memory against itself.
        chosen, _stats = candidates(collect([entry("mem_a", SUMMARY)]))
        self.assertEqual(chosen, [])

    def test_the_paper_title_pair_is_reachable(self) -> None:
        # The regression that matters: on the real vault this pair sits at
        # rank 63 of 40,045 by IDF Dice. Anyone raising the bar above it must
        # see this test fail, because it is the only conflict that ever
        # actually happened.
        entries = [
            entry("mem_20261125T155817_782bcbcf", SUMMARY),
            entry("mem_20261124T205154_e6e751a1", TITLE),
            entry("mem_filler", "周末去拍了外滩的日落，用了 35mm 定焦"),
        ]
        chosen, stats = candidates(collect(entries), limit=50)
        pairs = [{c.left.entry_id, c.right.entry_id} for c in chosen]
        self.assertIn(
            {"mem_20261125T155817_782bcbcf", "mem_20261124T205154_e6e751a1"}, pairs
        )
        self.assertGreater(stats.scored, 0)

    def test_the_subject_anchor_reaches_what_similarity_cannot(self) -> None:
        # The measured failure: this pair shares only 毕业/业论/论文 -- the most
        # common bigrams in the corpus -- so its Dice score is low and it ranked
        # 207th of 4,803. It is also the only conflict that ever actually
        # happened. The subject is stated first and identical, so the anchor
        # channel must pick it up regardless of score.
        entries = [
            entry("mem_20261125T155817_782bcbcf", SUMMARY),
            entry("mem_20261124T205154_e6e751a1", TITLE),
        ]
        chosen, stats = candidates(collect(entries), limit=1)
        self.assertTrue(chosen, "the subject channel produced nothing")
        stale = next(c for c in chosen if "尚未冻结" in c.left.text or "尚未冻结" in c.right.text)
        self.assertIn("基于多 Agent 架构", stale.left.text + stale.right.text)
        self.assertGreaterEqual(stats.anchored, 1)

    def test_a_generic_opener_does_not_open_the_floodgates(self) -> None:
        # If a subject bucket grows past the ceiling, the anchor is a filler word
        # ("用户", "不喜欢") rather than a subject; only its best pairs are kept.
        entries = [
            entry(f"mem_g{i}", f"用户偏好第 {i} 号：界面要简洁，不要花哨的动效 {i}")
            for i in range(40)
        ]
        chosen, stats = candidates(collect(entries), limit=500)
        self.assertLessEqual(stats.anchored, 8)
        self.assertGreater(stats.anchored, 0)

    def test_a_verbose_entry_cannot_flood_the_candidate_list(self) -> None:
        summary = entry("mem_summary", SUMMARY)
        # Each single-fact entry overlaps several of the summary's claims at
        # once -- that is what the real vault looked like, and why six entries
        # each produced a "duplicate" verdict against the same summary.
        # Distinct openers so the *similarity* channel is what is under test;
        # the subject channel is deliberately exempt from the per-pair cap.
        singles = [
            entry(
                f"mem_single{i}",
                f"编号{i}号：目录在 D:/WorkBuddy 下，目标是整合学业、毕业论文、求职、个人项目和 Agent",
            )
            for i in range(6)
        ]
        chosen, stats = candidates(
            collect([summary] + singles), limit=100, per_entry_pair=1
        )
        per_pair = {}
        for candidate in chosen:
            key = tuple(sorted((candidate.left.entry_id, candidate.right.entry_id)))
            per_pair[key] = per_pair.get(key, 0) + 1
        self.assertTrue(all(count <= 1 for count in per_pair.values()))
        self.assertGreater(stats.crowded_out, 0)

    def test_the_pairing_cut_is_reported_rather_than_silent(self) -> None:
        # "No conflicts found" must never be printed for pairs that were never
        # sent, so the numbers have to add up.
        entries = [entry(f"mem_{i}", f"第 {i} 条完全不同的话题内容 {i * 7}") for i in range(5)]
        claims = collect(entries)
        chosen, stats = candidates(claims, limit=1)
        total = len(claims) * (len(claims) - 1) // 2
        self.assertEqual(stats.scored + stats.unshared, total)
        self.assertEqual(stats.selected, len(chosen))
        self.assertLessEqual(stats.selected, 1)

    def test_limit_cuts_are_counted(self) -> None:
        entries = [
            entry(f"mem_{i}", f"共同话题内容第 {i} 条：本地记忆与文件化管理")
            for i in range(6)
        ]
        _chosen, stats = candidates(collect(entries), limit=2, per_entry_pair=2)
        self.assertEqual(stats.selected, 2)
        self.assertGreater(stats.over_limit, 0)

    def test_one_entry_is_not_a_corpus(self) -> None:
        chosen, stats = candidates(collect([entry("mem_a", SUMMARY)]))
        self.assertEqual(chosen, [])
        self.assertEqual(stats.selected, 0)

    def test_results_are_ordered_and_deterministic(self) -> None:
        entries = [
            entry("mem_a", "毕业论文题目尚未确定"),
            entry("mem_b", "毕业论文题目是财务共享中心流程优化"),
            entry("mem_c", "毕业论文题目已确定为多智能体架构"),
        ]
        first, _ = candidates(collect(entries))
        second, _ = candidates(collect(entries))
        self.assertEqual([(c.left.key, c.right.key) for c in first],
                         [(c.left.key, c.right.key) for c in second])
        scores = [candidate.score for candidate in first]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIsInstance(first[0], Candidate)
        self.assertIsInstance(first[0].left, Claim)


if __name__ == "__main__":
    unittest.main()
