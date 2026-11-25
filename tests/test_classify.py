"""Category rules and the entry splitter.

Both are pure functions, which is the point: the import path must behave the
same on a machine with no network, no API key and no prior state. These tests
pin the behaviour that `memoryhub recategorize` relies on, including the cases
where the first implementation was wrong.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.adapters.base import (  # noqa: E402
    drop_heading_only,
    sections,
    split_oversized,
)
from memoryhub.classify import classify, score  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_red_line_beats_the_name_it_mentions(self) -> None:
        # Was filed as `profile` by the first implementation, which lost the one
        # property that makes the line actionable.
        self.assertEqual(classify("真实姓名绝不外泄")[0], "decision")

    def test_identity_fact(self) -> None:
        self.assertEqual(classify("称呼：主人｜AI 自称：天枢（枢）")[0], "profile")
        self.assertEqual(classify("学校：示例市示例大学")[0], "profile")

    def test_preference(self) -> None:
        self.assertEqual(classify("偏好结构化输出（表格+粗体）")[0], "preference")

    def test_project_survives_a_paragraph_full_of_rules(self) -> None:
        # Real entry: the project paragraph also said "必须"/"不得". Scoring alone
        # tied at 7 and the tie-break picked `decision`. A weight-3 signal
        # (github.com) now decides.
        body = (
            "## 六、当前重点项目：MemoryHub\n"
            "- 仓库：https://github.com/iCurrer/MemoryHub\n"
            "- 接手前必须重新检查代码，不得信任文档\n"
        )
        category, reason = classify(body)
        self.assertEqual(category, "project")
        self.assertIn("decisive", reason)

    def test_unmatched_body_keeps_the_import_default(self) -> None:
        category, reason = classify("今天天气不错", default="knowledge")
        self.assertEqual(category, "knowledge")
        self.assertEqual(reason, "no rule matched")

    def test_short_body_is_not_guessed(self) -> None:
        _category, reason = classify("好", default="profile")
        self.assertEqual(reason, "body too short for rules")

    def test_reason_lists_the_decisive_signals(self) -> None:
        category, reason = classify("摄影组织：只写参与活动，绝不写会员")
        self.assertEqual(category, "decision")
        # Only decisive signals are reported once one fires: listing the losers
        # would suggest they counted.
        self.assertIn("绝不(3)", reason)
        self.assertIn("decisive", reason)

    def test_reason_sums_weights_when_nothing_is_decisive(self) -> None:
        category, reason = classify("文件写入一律原子替换，这是约定")
        self.assertEqual(category, "decision")
        self.assertIn("一律(2)", reason)
        self.assertIn("约定(1)", reason)
        self.assertNotIn("decisive", reason)

    def test_score_returns_weights(self) -> None:
        hits = score("必须做，绝不妥协")
        self.assertIn(("绝不", 3), hits["decision"])
        self.assertIn(("必须", 2), hits["decision"])


class SplitTests(unittest.TestCase):
    def test_short_body_is_returned_untouched(self) -> None:
        pairs = [("标题", "## 标题\n\n一句话。")]
        self.assertEqual(split_oversized(pairs), pairs)

    def test_long_body_splits_at_subheadings_and_keeps_parent_context(self) -> None:
        body = (
            "## 七、其他重要项目\n"
            "### Pocket CCD\n- 相机项目，独立开发。\n\n"
            "### 带屏键盘\n- 彩色小屏模块。\n"
        )
        pieces = split_oversized([("七、其他重要项目", body)], max_chars=40)
        self.assertGreater(len(pieces), 1)
        # The parent heading must not become its own empty entry: it rides along
        # as context on the first child.
        self.assertTrue(all("其他重要项目" in piece[1][:40] for piece in pieces[:1]))
        self.assertTrue(any("Pocket CCD" in piece[1] for piece in pieces))
        for _heading, piece in pieces:
            self.assertTrue(piece.strip())

    def test_long_body_without_subheadings_splits_at_blank_lines(self) -> None:
        body = "## 段落\n\n" + "\n\n".join(f"第{i}段内容，长度凑数。" for i in range(6))
        pieces = split_oversized([("段落", body)], max_chars=60)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(piece) <= 60 for _heading, piece in pieces))

    def test_single_block_without_blank_lines_falls_back_to_sentences(self) -> None:
        body = "## 长句\n" + "".join(f"第{i}句话在这里。" for i in range(10))
        pieces = split_oversized([("长句", body)], max_chars=60)
        self.assertGreater(len(pieces), 1)

    def test_drop_heading_only_removes_empty_sections(self) -> None:
        pairs = [
            ("三、偏好", "## 三、偏好"),
            ("四、目标", "## 四、目标\n- 先就业再创业"),
        ]
        kept = drop_heading_only(pairs)
        self.assertEqual(len(kept), 1)
        self.assertIn("先就业", kept[0][1])

    def test_sections_still_ignores_headings_inside_fences(self) -> None:
        text = "## 命令\n\n```bash\n# 不是标题\npnpm test\n```\n"
        self.assertEqual(len(sections(text)), 1)


if __name__ == "__main__":
    unittest.main()
