"""The derived one-liner: verified, disposable, and never in the vault."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from memoryhub import summary
from memoryhub.models import MemoryEntry

BODY = "## 十、审美偏好\n\n喜欢：Apple 风格、ColorOS 的精致交互、Notion 的信息组织。"


class FakeProvider:
    """Answers like a model would, without a network or a key."""

    def __init__(self, replies: List[str]) -> None:
        self.replies = list(replies)
        self.seen: List[List[Dict[str, str]]] = []
        self.cfg = type("Cfg", (), {"model": "fake-model"})()

    def chat(self, messages: List[Dict[str, str]]) -> str:
        self.seen.append(messages)
        if not self.replies:
            return '{"line": "", "quote": ""}'
        return self.replies.pop(0)


def entry(body: str = BODY) -> MemoryEntry:
    return MemoryEntry.create(category="preference", body=body)


class ParseReplyTests(unittest.TestCase):
    def test_accepts_a_quote_that_is_actually_in_the_body(self) -> None:
        raw = json.dumps({"line": "审美偏好偏 Apple 风格", "quote": "喜欢：Apple 风格"}, ensure_ascii=False)
        parsed = summary.parse_reply(raw, BODY)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["line"], "审美偏好偏 Apple 风格")

    def test_refuses_a_quote_that_is_not_in_the_body(self) -> None:
        # The whole defence in one assertion: a model that invents a sentence
        # usually invents its evidence too, so requiring both makes invention
        # visible instead of fluent.
        raw = json.dumps({"line": "用户喜欢极简设计", "quote": "用户明确说喜欢极简"}, ensure_ascii=False)
        self.assertIsNone(summary.parse_reply(raw, BODY))

    def test_refuses_a_quote_that_is_too_short_to_prove_anything(self) -> None:
        raw = json.dumps({"line": "审美是 Apple 风格", "quote": "喜欢"}, ensure_ascii=False)
        self.assertIsNone(summary.parse_reply(raw, BODY))

    def test_whitespace_differences_do_not_break_the_quote(self) -> None:
        raw = json.dumps({"line": "喜欢 Apple 与 Notion", "quote": "Apple  风格、ColorOS"}, ensure_ascii=False)
        self.assertIsNotNone(summary.parse_reply(raw, BODY))

    def test_an_empty_line_is_a_normal_answer_not_an_error(self) -> None:
        raw = json.dumps({"line": "", "quote": "喜欢：Apple 风格"}, ensure_ascii=False)
        self.assertIsNone(summary.parse_reply(raw, BODY))

    def test_survives_a_code_fence_and_trailing_prose(self) -> None:
        raw = '```json\n{"line": "喜欢 Apple 与 Notion", "quote": "喜欢：Apple 风格"}\n```'
        self.assertIsNotNone(summary.parse_reply(raw, BODY))

    def test_rejects_output_that_is_not_json(self) -> None:
        self.assertIsNone(summary.parse_reply("这句话没有 JSON", BODY))

    def test_clips_a_long_line_and_drops_a_trailing_colon(self) -> None:
        long_line = "审美偏好：" + "很长" * 40
        raw = json.dumps({"line": long_line, "quote": "喜欢：Apple 风格"}, ensure_ascii=False)
        parsed = summary.parse_reply(raw, BODY)
        self.assertIsNotNone(parsed)
        self.assertLessEqual(len(parsed["line"]), summary.MAX_CHARS + 1)
        self.assertFalse(parsed["line"].endswith("："))


class SidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_missing_file_is_simply_no_summaries(self) -> None:
        self.assertEqual(summary.load(self.root), {})
        self.assertIsNone(summary.for_entry({}, entry()))

    def test_a_corrupt_file_does_not_break_the_page(self) -> None:
        summary.path_for(self.root).parent.mkdir(parents=True, exist_ok=True)
        summary.path_for(self.root).write_text("{not json", encoding="utf-8")
        self.assertEqual(summary.load(self.root), {})

    def test_editing_the_body_invalidates_its_sentence(self) -> None:
        one = entry()
        data = {one.id: {"line": "旧的概括", "quote": "喜欢：Apple 风格",
                         "body_hash": summary.body_hash(one.body), "model": "m", "at": "x"}}
        self.assertEqual(summary.for_entry(data, one), "旧的概括")
        changed = entry(BODY + "\n补充一句。")
        changed.id = one.id
        self.assertIsNone(summary.for_entry(data, changed), "正文变了，旧概括必须失效")

    def test_round_trips_through_disk(self) -> None:
        one = entry()
        summary.save(self.root, {one.id: {"line": "一句", "quote": "喜欢：Apple 风格",
                                          "body_hash": summary.body_hash(one.body),
                                          "model": "m", "at": "x"}})
        self.assertEqual(summary.load(self.root)[one.id]["line"], "一句")

    def test_stats_counts_covered_and_missing(self) -> None:
        a, b = entry(), entry(BODY + "\n另一条。")
        data = {a.id: {"line": "一句", "quote": "喜欢：Apple 风格",
                       "body_hash": summary.body_hash(a.body), "model": "m", "at": "x"}}
        self.assertEqual(summary.stats([a, b], data), {"total": 2, "covered": 1, "missing": 1})

    def test_pending_lists_never_summarized_entries(self) -> None:
        a, b = entry(), entry(BODY + "\n另一条。")
        data = {a.id: {"line": "一句", "quote": "喜欢：Apple 风格",
                       "body_hash": summary.body_hash(a.body), "model": "m", "at": "x"}}
        self.assertEqual([e.id for e in summary.pending([a, b], data)], [b.id])


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_verified_reply_becomes_a_summary(self) -> None:
        one = entry()
        provider = FakeProvider([json.dumps({"line": "喜欢 Apple 与 Notion", "quote": "喜欢：Apple 风格"},
                                            ensure_ascii=False)])
        merged, failures = summary.generate([one], {}, provider)
        self.assertEqual(failures, [])
        self.assertEqual(merged[one.id]["line"], "喜欢 Apple 与 Notion")
        self.assertEqual(merged[one.id]["model"], "fake-model")

    def test_an_unverifiable_reply_is_a_failure_and_leaves_no_entry(self) -> None:
        one = entry()
        provider = FakeProvider([json.dumps({"line": "编的", "quote": "原文里没有这句"}, ensure_ascii=False)])
        merged, failures = summary.generate([one], {}, provider)
        self.assertEqual(merged, {})
        self.assertEqual(failures, [one.id])

    def test_the_prompt_carries_the_body_verbatim(self) -> None:
        one = entry()
        provider = FakeProvider([json.dumps({"line": "x", "quote": "喜欢：Apple 风格"}, ensure_ascii=False)])
        summary.generate([one], {}, provider)
        user = provider.seen[0][-1]["content"]
        self.assertIn("喜欢：Apple 风格、ColorOS 的精致交互", user)

    def test_one_bad_reply_does_not_stop_the_run(self) -> None:
        a, b = entry(), entry(BODY + "\n另一条。")
        provider = FakeProvider([
            "不是 JSON",
            json.dumps({"line": "第二句", "quote": "喜欢：Apple 风格"}, ensure_ascii=False),
        ])
        merged, failures = summary.generate([a, b], {}, provider)
        self.assertEqual(failures, [a.id])
        self.assertIn(b.id, merged)

    def test_generate_never_writes_to_disk_by_itself(self) -> None:
        one = entry()
        provider = FakeProvider([json.dumps({"line": "x", "quote": "喜欢：Apple 风格"}, ensure_ascii=False)])
        summary.generate([one], {}, provider)
        self.assertFalse(summary.path_for(self.root).exists(), "落盘由调用方决定，半路死掉不该留半个文件")


class PageGuardTests(unittest.TestCase):
    """The page must not depend on anything the preview frame blocks."""

    def test_the_page_uses_no_modal_dialog(self) -> None:
        # `confirm()` inside a sandboxed frame returns false without showing
        # anything -- the button looks dead, and it did. Confirmation is now a
        # second click on the same button.
        page = (Path(__file__).parent.parent / "src" / "memoryhub" / "webui" / "index.html").read_text(encoding="utf-8")
        for forbidden in ("confirm(", "alert(", "prompt("):
            self.assertNotIn(forbidden, page)


if __name__ == "__main__":
    unittest.main()
