"""Tests for the AI organisation layer.

The point of these tests is not that the model answers well -- it cannot be
asserted. It is that everything *around* the model is airtight: a mock transport
stands in for the network, so the prompt, the JSON extraction, the per-item
validation, the quote location check and the write path are all exercised end to
end without a single API call.

The quote check gets the most attention because it is the only automated defence
against the model inventing a memory that a later AI will read as fact.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from memoryhub.ai import AIConfig, AIError, Provider, organize, parse_proposals
from memoryhub.ai.organize import batch_split, quote_is_located, _fingerprint
from memoryhub.ai.schema import MAX_BODY_CHARS, extract_json


SOURCE = """\
# 协作规则

真实姓名绝不外泄。

技术偏好：开源优先，能本地部署就不依赖云服务。

## 项目

MemoryHub 是一个跨 AI 共享记忆的工具，仓库在 github.com/iCurrer/MemoryHub。
"""


def _reply(entries):
    import json

    return {"choices": [{"message": {"content": json.dumps({"entries": entries}, ensure_ascii=False)}}]}


class FakeTransport:
    """Records the request and answers with a canned body."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _provider(transport):
    cfg = AIConfig(base_url="https://example.test/v1", model="fake-model")
    return Provider(cfg, transport=transport)


class FingerprintTests(unittest.TestCase):
    def test_punctuation_and_width_do_not_break_location(self):
        # A faithful quote may come back with different quote marks or spacing;
        # rejecting those would make the check useless in practice.
        source = _fingerprint("真实姓名绝不外泄。")
        self.assertTrue(quote_is_located("真实姓名绝不外泄", source))
        self.assertTrue(quote_is_located("真实姓名 绝不外泄！", source))

    def test_paraphrase_is_not_located(self):
        source = _fingerprint("真实姓名绝不外泄。")
        self.assertFalse(quote_is_located("真实姓名不得对外泄露", source))

    def test_three_chars_is_too_short_to_prove_anything(self):
        source = _fingerprint("开源优先，能本地部署就不依赖云服务。")
        self.assertFalse(quote_is_located("开源", source))


class BatchSplitTests(unittest.TestCase):
    def test_short_text_is_one_batch(self):
        self.assertEqual(batch_split("abc", 100), ["abc"])

    def test_splits_on_blank_lines_only(self):
        text = "a" * 60 + "\n\n" + "b" * 60
        batches = batch_split(text, 70)
        self.assertEqual(len(batches), 2)
        # A hard cut would have sliced mid-string and handed the model a fragment
        # to "complete" from imagination.
        for batch in batches:
            self.assertFalse(batch.startswith("a") and batch.endswith("b"))


class ExtractJsonTests(unittest.TestCase):
    def test_tolerates_markdown_fence(self):
        self.assertEqual(extract_json('```json\n{"entries": []}\n```'), {"entries": []})

    def test_tolerates_leading_prose(self):
        self.assertEqual(extract_json('好的，结果如下：\n{"entries": []}'), {"entries": []})

    def test_raises_when_there_is_no_json(self):
        with self.assertRaises(ValueError):
            extract_json("我不确定该怎么整理。")


class ParseProposalsTests(unittest.TestCase):
    def test_good_item_passes(self):
        entries, problems = parse_proposals(
            {"entries": [{"category": "decision", "body": "姓名不外泄", "quote": "真实姓名绝不外泄"}]}
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].category, "decision")
        self.assertEqual(problems, [])

    def test_bad_category_is_dropped_not_fatal(self):
        entries, problems = parse_proposals(
            {
                "entries": [
                    {"category": "nonsense", "body": "x", "quote": "y"},
                    {"category": "profile", "body": "称呼主人", "quote": "称呼：主人"},
                ]
            }
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].category, "profile")
        self.assertTrue(any("nonsense" in p for p in problems))

    def test_item_without_quote_is_dropped(self):
        entries, problems = parse_proposals(
            {"entries": [{"category": "decision", "body": "姓名不外泄"}]}
        )
        self.assertEqual(entries, [])
        self.assertTrue(any("quote" in p for p in problems))

    def test_oversized_body_is_dropped(self):
        entries, problems = parse_proposals(
            {
                "entries": [
                    {
                        "category": "project",
                        "body": "x" * (MAX_BODY_CHARS + 1),
                        "quote": "some quote",
                    }
                ]
            }
        )
        self.assertEqual(entries, [])
        self.assertTrue(any("超过" in p for p in problems))

    def test_duplicate_bodies_are_collapsed(self):
        item = {"category": "decision", "body": "同一句", "quote": "同一句原文"}
        entries, problems = parse_proposals({"entries": [item, dict(item)]})
        self.assertEqual(len(entries), 1)
        self.assertTrue(any("重复" in p for p in problems))

    def test_missing_envelope_raises(self):
        with self.assertRaises(ValueError):
            parse_proposals({"items": []})


class ProviderTests(unittest.TestCase):
    def test_sends_bearer_token_and_model(self):
        transport = FakeTransport(_reply([]))
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k-123"}):
            _provider(transport).chat([{"role": "user", "content": "hi"}])
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(call["payload"]["model"], "fake-model")
        self.assertEqual(call["headers"]["Authorization"], "Bearer k-123")

    def test_missing_key_names_the_variable(self):
        transport = FakeTransport(_reply([]))
        # Blank rather than clear=True: wiping the whole environment also removes
        # HOME, and building the error message needs it to name the config path.
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": ""}):
            with self.assertRaises(AIError) as ctx:
                _provider(transport).chat([{"role": "user", "content": "hi"}])
        self.assertIn("MEMORYHUB_API_KEY", str(ctx.exception))

    def test_server_error_field_is_surfaced(self):
        transport = FakeTransport({"error": {"message": "quota exceeded"}})
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            with self.assertRaises(AIError) as ctx:
                _provider(transport).chat([{"role": "user", "content": "hi"}])
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_content_parts_are_joined(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": [{"type": "text", "text": "ab"}, {"type": "text", "text": "cd"}]}}]}
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            self.assertEqual(_provider(transport).chat([]), "abcd")


class OrganizeTests(unittest.TestCase):
    def test_end_to_end_with_located_quotes(self):
        transport = FakeTransport(
            _reply(
                [
                    {"category": "decision", "body": "真实姓名绝不外泄", "quote": "真实姓名绝不外泄", "confidence": "high"},
                    {"category": "preference", "body": "技术偏好是开源优先、本地部署", "quote": "技术偏好：开源优先，能本地部署就不依赖云服务", "confidence": "high"},
                ]
            )
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = organize(SOURCE, _provider(transport))

        self.assertEqual(len(result.entries), 2)
        self.assertEqual(result.unverified, 0)
        self.assertEqual(result.calls, 1)
        self.assertTrue(all(ok for ok in result.trace.values()))
        # Every entry is pending; phase 3 forbids writing straight to vault/.
        self.assertTrue(all(e.status == "pending" for e in result.entries))
        self.assertTrue(all("ai-organized" in e.tags for e in result.entries))

    def test_invented_quote_is_kept_but_flagged(self):
        transport = FakeTransport(
            _reply(
                [
                    {
                        "category": "profile",
                        "body": "用户是某大学博士生",
                        "quote": "这句话原文里根本没有",
                        "confidence": "high",
                    }
                ]
            )
        )
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = organize(SOURCE, _provider(transport))

        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.unverified, 1)
        self.assertTrue(any("定位不到" in p for p in result.problems))

    def test_bad_json_in_one_batch_does_not_abort_the_rest(self):
        transport = FakeTransport({"choices": [{"message": {"content": "抱歉我无法完成"}}]})
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = organize(SOURCE, _provider(transport))
        self.assertEqual(result.entries, [])
        self.assertEqual(len(result.problems), 1)

    def test_empty_source_short_circuits_without_a_call(self):
        transport = FakeTransport(_reply([]))
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            result = organize("   \n\n  ", _provider(transport))
        self.assertEqual(result.entries, [])
        self.assertEqual(transport.calls, [])

    def test_prompt_forbids_rephrasing_and_carries_category_help(self):
        transport = FakeTransport(_reply([]))
        with mock.patch.dict(os.environ, {"MEMORYHUB_API_KEY": "k"}):
            organize(SOURCE, _provider(transport))
        system = transport.calls[0]["payload"]["messages"][0]["content"]
        self.assertIn("保持原文措辞", system)
        self.assertIn("原文没有的内容绝对不要出现", system)
        for category in ("profile", "preference", "project", "knowledge", "decision"):
            self.assertIn(category, system)


class ConfigTests(unittest.TestCase):
    def test_no_config_means_none_not_an_error(self):
        with mock.patch("memoryhub.config.load", return_value={}):
            self.assertIsNone(AIConfig.from_config())

    def test_nested_table_is_read(self):
        data = {"ai": {"base_url": "https://x/v1", "model": "m"}}
        with mock.patch("memoryhub.config.load", return_value=data):
            cfg = AIConfig.from_config()
        self.assertEqual(cfg.base_url, "https://x/v1")
        self.assertEqual(cfg.model, "m")

    def test_flat_keys_are_read_too(self):
        data = {"ai_base_url": "https://y/v1", "ai_model": "n"}
        with mock.patch("memoryhub.config.load", return_value=data):
            cfg = AIConfig.from_config()
        self.assertEqual(cfg.model, "n")

    def test_half_configured_is_an_actionable_error(self):
        with mock.patch("memoryhub.config.load", return_value={"ai": {"base_url": "https://x/v1"}}):
            with self.assertRaises(AIError) as ctx:
                AIConfig.from_config()
        self.assertIn("model", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
