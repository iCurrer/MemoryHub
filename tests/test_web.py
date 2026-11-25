"""Tests for the local page (P6-03).

Three things are worth asserting here, and they are not the HTML:

1. The page cannot be reached without the per-run token, and cannot be reached
   from a non-loopback ``Host`` even with the token (DNS rebinding).
2. The write actions go through the same store calls as the CLI -- so a
   ``blocked`` entry is refused for the reason the CLI gives, not because the
   button happened to be hidden.
3. ``--read-only`` is enforced on the server, not by the page choosing not to
   send the request.
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import access, conflicts, summary, web  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402


class ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "vault"
        self.store = VaultStore(self.root)
        self.store.init_layout()
        self.server = None

    def start(self, **kwargs) -> web.App:
        self.server, app = web.make_server(self.store, port=0, **kwargs)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        return app

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        token: str = "",
        host: str = "",
    ):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token:
            headers["X-MemoryHub-Token"] = token
        if host:
            headers["Host"] = host
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type") or ""
        conn.close()
        if "json" in content_type:
            return response.status, json.loads(raw.decode("utf-8"))
        return response.status, raw.decode("utf-8")

    def seed(self, body: str, *, category: str = "profile", sensitivity: str = "normal") -> MemoryEntry:
        entry = MemoryEntry.create(
            category=category, body=body, sensitivity=sensitivity, source_adapter="test"
        )
        self.store.create(entry)
        return entry


class AuthTests(ServerCase):
    def test_page_carries_the_token_only_when_the_request_proves_it(self) -> None:
        app = self.start()
        status, html = self.request("GET", f"/?t={app.token}")
        self.assertEqual(status, 200)
        self.assertIn(app.token, html)
        self.assertIn("<title>MemoryHub</title>", html)

    def test_page_shell_loads_without_a_token_but_holds_no_secret(self) -> None:
        # A reload used to be a dead end: the script rewrote the URL, and the
        # server answered the tokenless request with 401 JSON. The shell is now
        # always served; the token is what stays protected, not the layout.
        app = self.start()
        status, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(app.token, html)
        self.assertNotIn("__MEMORYHUB_TOKEN__", html, "占位符不能被原样送出")

    def test_api_needs_the_token(self) -> None:
        self.start()
        status, body = self.request("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_foreign_host_is_refused_even_with_the_token(self) -> None:
        # The token alone is not enough: a page on the internet can resolve a
        # hostname to 127.0.0.1 and reach this server, but it cannot forge Host.
        app = self.start()
        status, body = self.request(
            "GET", "/api/overview", token=app.token, host="evil.example.com"
        )
        self.assertEqual(status, 403)
        self.assertIn("本机", body["error"])

    def test_no_telemetry_in_the_page(self) -> None:
        # The claim "nothing leaves the machine" is only checkable if the page
        # has no remote reference in it. This is that check.
        app = self.start()
        _, html = self.request("GET", f"/?t={app.token}")
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)


class OverviewTests(ServerCase):
    def test_counts_every_status_and_reports_the_sync_block(self) -> None:
        app = self.start()
        first = self.seed("主人是会计学专业的学生。")
        second = self.seed("毕业论文题目已定。", category="project")
        self.store.set_status(first.id, "approved")
        self.store.set_status(second.id, "approved")
        self.seed("待审的一条。", category="knowledge")

        status, data = self.request("GET", "/api/overview", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_status"]["approved"], 2)
        self.assertEqual(data["by_status"]["pending"], 1)
        self.assertEqual(data["sync"]["live"], 2)
        self.assertGreater(data["sync"]["block_chars"], 0)
        self.assertEqual(len(data["sync"]["targets"]), 3)
        # The page states who has been reading; with no MCP calls yet it must
        # report zero rather than omit the field (a missing key reads as "not
        # implemented" in the browser).
        self.assertEqual(data["access"]["calls"], 0)
        self.assertEqual(data["access"]["labels"], [])

    def test_overview_reports_reads_once_they_happen(self) -> None:
        app = self.start()
        entry = self.seed("一条被读过的记忆。", category="preference")
        self.store.set_status(entry.id, "approved")
        access.record(self.store.root, "memory_search", [entry.id], "propose", "claude-code")

        status, data = self.request("GET", "/api/overview", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual(data["access"]["calls"], 1)
        self.assertEqual(data["access"]["labels"], ["claude-code"])
        self.assertEqual(data["access"]["read_entries"], 1)

    def test_pending_lists_blocked_first(self) -> None:
        app = self.start()
        self.seed("普通一条。", category="knowledge")
        # Deliberately not shaped like a street address: the boundary hook in
        # this repo blocks anything with a house number, fixtures included.
        self.seed("一条被标为敏感的住址描述。", sensitivity="sensitive")
        status, data = self.request("GET", "/api/pending", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual([e["trust"] for e in data["entries"]], ["blocked", "auto"])


class ReviewTests(ServerCase):
    def test_approve_moves_the_entry(self) -> None:
        app = self.start()
        entry = self.seed("主人用 YonBIP 做过财务实施。", category="project")
        status, data = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "approve"}, token=app.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["entry"]["status"], "approved")
        self.assertEqual(self.store.get(entry.id).status, "approved")

    def test_blocked_is_refused_with_the_reason(self) -> None:
        app = self.start()
        entry = self.seed("门牌级住址。", sensitivity="sensitive")
        status, data = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "approve"}, token=app.token
        )
        self.assertEqual(status, 409)
        self.assertIn("sensitivity", data["error"])
        self.assertEqual(self.store.get(entry.id).status, "pending")

    def test_reject_and_recategorise(self) -> None:
        app = self.start()
        entry = self.seed("一句没有主语的状态行。", category="knowledge")
        status, _ = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "category", "category": "preference"},
            token=app.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(entry.id).category, "preference")

        status, _ = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "reject"}, token=app.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(entry.id).status, "rejected")

    def test_unknown_category_is_rejected(self) -> None:
        app = self.start()
        entry = self.seed("随便一条。", category="knowledge")
        status, data = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "category", "category": "瞎写"},
            token=app.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("未知分类", data["error"])

    def test_missing_entry_is_404(self) -> None:
        app = self.start()
        status, data = self.request(
            "POST", "/api/review", {"id": "mem_20260101T000000_deadbeef", "action": "approve"},
            token=app.token,
        )
        self.assertEqual(status, 404)

    def test_read_only_refuses_writes(self) -> None:
        app = self.start(allow_write=False)
        entry = self.seed("一条待审。", category="knowledge")
        status, data = self.request(
            "POST", "/api/review", {"id": entry.id, "action": "approve"}, token=app.token
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.store.get(entry.id).status, "pending")


class ProposalTests(ServerCase):
    def _propose(self) -> tuple:
        older = self.seed("毕业论文（尚未冻结）：先确定题目。", category="project")
        newer = self.seed("毕业论文题目已定为多 Agent 财务共享中心。", category="project")
        self.store.set_status(older.id, "approved")
        self.store.set_status(newer.id, "approved")
        conflicts.save_record(
            self.root,
            {
                "pair": [older.id, newer.id],
                "relation": "contradicts",
                "scope": "partial",
                "winner": newer.id,
                "reason": "一条说题目未定，一条说已定。",
                "verified": True,
                "model": "test",
                "detected_at": "2026-11-25T19:00:00+08:00",
                "claims": [],
            },
        )
        return older, newer

    def test_proposal_carries_both_sides(self) -> None:
        app = self.start()
        older, newer = self._propose()
        status, data = self.request("GET", "/api/proposals", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["proposals"]), 1)
        proposal = data["proposals"][0]
        self.assertEqual(proposal["relation"], "contradicts")
        self.assertEqual(proposal["left"]["id"], older.id)
        self.assertIsNone(proposal["stale"])
        self.assertTrue(proposal["left"]["body"])

    def test_stale_proposal_is_flagged_not_hidden(self) -> None:
        app = self.start()
        older, newer = self._propose()
        third = self.seed("临时一条。", category="knowledge")
        conflicts.save_record(
            self.root,
            {
                "pair": [third.id, older.id],
                "relation": "duplicate",
                "scope": "partial",
                "detected_at": "2026-11-25T19:00:01+08:00",
            },
        )
        status, data = self.request("GET", "/api/proposals", token=app.token)
        self.assertEqual(status, 200)
        stale = [p for p in data["proposals"] if third.id in (p["left_id"], p["right_id"])]
        self.assertEqual(len(stale), 1)
        self.assertIn("approved", stale[0]["stale"])

    def test_supersede_retires_and_closes_the_record(self) -> None:
        app = self.start()
        older, newer = self._propose()
        status, data = self.request(
            "POST", "/api/supersede", {"old": older.id, "new": newer.id, "note": "webui:test"},
            token=app.token,
        )
        self.assertEqual(status, 200, data)
        retired = self.store.get(older.id)
        self.assertEqual(retired.status, "archived")
        self.assertEqual(retired.superseded_by, newer.id)
        self.assertIn(older.id, self.store.get(newer.id).supersedes)
        # The proposal moved to logs/ with the decision attached.
        self.assertFalse(conflicts.find_record(self.root, older.id, newer.id))
        resolved = list((self.root / "logs" / "conflicts").glob("*.json"))
        self.assertEqual(len(resolved), 1)
        record = json.loads(resolved[0].read_text(encoding="utf-8"))
        self.assertEqual(record["resolution"]["keep"], newer.id)

    def test_supersede_refuses_a_retired_replacement(self) -> None:
        # Reversing a decision is not a second supersede: the retired side is no
        # longer approved, and the store says so instead of silently rewriting
        # history. A human clears the field on purpose if they really mean it.
        app = self.start()
        older, newer = self._propose()
        self.assertEqual(
            self.request(
                "POST", "/api/supersede", {"old": older.id, "new": newer.id}, token=app.token
            )[0],
            200,
        )
        status, data = self.request(
            "POST", "/api/supersede", {"old": newer.id, "new": older.id}, token=app.token
        )
        self.assertIn(status, (409, 500))
        self.assertTrue(data["error"])


class CrowdedTests(ServerCase):
    """The page must warn before a supersede that would hide several topics.

    Learned from the real library: the only genuine conflict there is a 401-char
    entry that also carries work directories, usage preferences and a file list.
    """

    def test_a_short_single_fact_is_not_crowded(self) -> None:
        entry = MemoryEntry.create(category="project", body="毕业论文题目已定。")
        self.assertFalse(web.is_crowded(entry))

    def test_a_long_entry_is_crowded(self) -> None:
        entry = MemoryEntry.create(category="project", body="顺手记一句。" * 40)
        self.assertTrue(web.is_crowded(entry))

    def test_many_topic_lines_are_crowded_even_when_short(self) -> None:
        body = "## 工作台\n- **目录**：D:/x\n1. 论文\n- **偏好**：本地优先\n"
        entry = MemoryEntry.create(category="project", body=body)
        self.assertLess(len(body), 180)
        self.assertTrue(web.is_crowded(entry))

    def test_proposals_carry_the_flag(self) -> None:
        app = self.start()
        short = self.seed("毕业论文题目已定。", category="project")
        long_one = self.seed("## 工作台\n" + "内容。" * 60, category="project")
        self.store.set_status(short.id, "approved")
        self.store.set_status(long_one.id, "approved")
        conflicts.save_record(
            self.root,
            {"pair": [short.id, long_one.id], "relation": "contradicts",
             "scope": "partial", "detected_at": "2026-11-25T19:00:00+08:00"},
        )
        status, data = self.request("GET", "/api/proposals", token=app.token)
        self.assertEqual(status, 200)
        sides = {side["id"]: side["crowded"] for side in
                 (data["proposals"][0]["left"], data["proposals"][0]["right"])}
        self.assertEqual(sides, {short.id: False, long_one.id: True})


class HeadlineTests(unittest.TestCase):
    """The headline must be *taken* from the entry, never written.

    A generated title would be the first place "the model may copy, never
    compose" (ADR-016) gets broken -- in the most visible spot on the page. So
    every assertion here is about substring-or-nothing, not about quality.
    """

    def heading_entry(self) -> MemoryEntry:
        return MemoryEntry.create(
            category="project",
            body="## 十五、优先事项（2026-11-24 快照）\n\n1. 毕业论文：确定选题\n2. 求职：探索 AI 产品",
        )

    def test_a_section_label_becomes_the_caption_not_the_title(self) -> None:
        # "十五、优先事项（2026-11-24 快照）" is a filing shelf, not a fact. The
        # card wants the fact on top and the shelf in a small chip.
        head = web.headline(self.heading_entry())
        self.assertEqual(head["text"], "毕业论文：确定选题")
        self.assertEqual(head["section"], "十五、优先事项（2026-11-24 快照）")
        self.assertEqual(head["skip"], 3, "标题行、空行与选中的那行都不必再出现")

    def test_an_entry_with_no_fact_line_falls_back_to_its_label(self) -> None:
        entry = MemoryEntry.create(category="profile", body="## 十、审美偏好\n\n## 十一、摄影\n")
        head = web.headline(entry)
        self.assertIn("审美偏好", head["text"])

    def test_a_section_caption_is_only_ever_a_label(self) -> None:
        # A previous bullet is not a shelf. Using one produced captions like
        # "- 视觉中国签约摄影师；2024 C…" in the chip.
        entry = MemoryEntry.create(
            category="knowledge",
            body="- 视觉中国签约摄影师\n- 老物件维修关注：iPod touch 1G\n",
        )
        self.assertEqual(web.headline(entry)["section"], "")

    def test_a_bullet_is_used_and_not_repeated_below(self) -> None:
        entry = MemoryEntry.create(
            category="decision",
            body="- **主线（2026.09 更新）：** 新道实习已结束，当前重心是备考。\n- **次线：** 别的事",
        )
        head = web.headline(entry)
        self.assertTrue(head["text"].startswith("主线"))
        self.assertEqual(head["skip"], 1, "标题已经把第一条说完了，正文不该再说一遍")

    def test_plain_first_line_is_used_as_is(self) -> None:
        entry = MemoryEntry.create(
            category="project",
            body="毕业论文：《基于多 Agent 架构的企业财务共享中心工作流程优化研究》",
        )
        self.assertIn("毕业论文", web.headline(entry)["text"])

    def test_headline_never_exceeds_the_limit(self) -> None:
        entry = MemoryEntry.create(category="knowledge", body="很长的一句话" * 40)
        head = web.headline(entry)["text"]
        self.assertLessEqual(len(head), web.MAX_HEADLINE + 1)
        self.assertTrue(head.endswith("…"))

    def test_headline_is_a_substring_of_what_it_came_from(self) -> None:
        # The property that makes this safe: strip the ellipsis and the markdown
        # emphasis, and what is left must literally appear in the body.
        entry = MemoryEntry.create(
            category="project",
            body="### 个人工作台「我的大四」\n- 目录：D:/x\n- 使用偏好：本地优先\n",
        )
        head = web.headline(entry)["text"]
        self.assertIn(head.rstrip("…"), entry.body.replace("**", ""))

    def test_short_entry_has_a_headline_too(self) -> None:
        # The page hides it below 60 chars (the body is the headline), but the
        # payload always carries one so callers need no special case.
        entry = MemoryEntry.create(category="knowledge", body="短句。")
        self.assertEqual(web.headline(entry)["text"], "短句。")

    def test_payload_carries_headline_and_line_count(self) -> None:
        entry = self.heading_entry()
        brief = web.entry_brief(entry)
        self.assertEqual(brief["headline"]["skip"], 3)
        self.assertEqual(brief["lines"], len(entry.body.strip().splitlines()))


class BrowseAndSummaryTests(ServerCase):
    """The card feed needs one more view and one optional sentence."""

    def approved(self, body: str, created_at: str = "", category: str = "preference") -> MemoryEntry:
        entry = MemoryEntry.create(
            category=category,
            body=body,
            source_adapter="test",
            created_at=created_at or "2026-11-24T09:00:00+08:00",
        )
        self.store.create(entry)
        self.store.set_status(entry.id, "approved")
        return entry

    def test_entries_returns_approved_newest_first_and_nothing_else(self) -> None:
        app = self.start()
        old = self.approved("旧的一条。", created_at="2020-01-01T00:00:00+08:00")
        new = self.approved("新的一条。", created_at="2026-11-25T00:00:00+08:00")
        pending = self.seed("还没审的一条。", category="preference")

        status, body = self.request("GET", "/api/entries", token=app.token)
        self.assertEqual(status, 200)
        ids = [item["id"] for item in body["entries"]]
        self.assertEqual(ids, [new.id, old.id])
        self.assertNotIn(pending.id, ids)

    def test_a_superseded_entry_is_not_browsable(self) -> None:
        app = self.start()
        kept = self.approved("留下的一条。")
        gone = self.approved("被取代的一条。")
        conflicts.apply_supersede(self.store, gone.id, kept.id, note="test")
        status, body = self.request("GET", "/api/entries", token=app.token)
        self.assertEqual(status, 200)
        ids = [item["id"] for item in body["entries"]]
        self.assertIn(kept.id, ids)
        self.assertNotIn(gone.id, ids)

    def test_the_card_carries_the_stored_sentence(self) -> None:
        app = self.start()
        entry = self.approved("## 十、审美偏好\n\n喜欢：Apple 风格、Notion。")
        summary.save(self.store.root, {entry.id: {
            "line": "审美偏好偏 Apple 与 Notion",
            "quote": "喜欢：Apple 风格",
            "body_hash": summary.body_hash(entry.body),
            "model": "m", "at": "2026-11-25T00:00:00+08:00",
        }})
        status, body = self.request("GET", "/api/entries", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["entries"][0]["summary"], "审美偏好偏 Apple 与 Notion")
        # The entry's own line is still there, so the page can show the quote
        # when it explains where the sentence came from.
        self.assertEqual(body["entries"][0]["headline"]["text"], "喜欢：Apple 风格、Notion。")

    def test_a_stale_sentence_is_not_shown(self) -> None:
        # The sidecar can outlive the body it described. Showing it anyway would
        # be the one failure mode that looks like success.
        app = self.start()
        entry = self.approved("正文第一版。")
        summary.save(self.store.root, {entry.id: {
            "line": "旧的概括", "quote": "正文第一版",
            "body_hash": summary.body_hash("完全不同的正文"),
            "model": "m", "at": "2026-11-25T00:00:00+08:00",
        }})
        status, body = self.request("GET", "/api/entries", token=app.token)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["entries"][0]["summary"])

    def test_overview_reports_how_many_cards_have_a_sentence(self) -> None:
        app = self.start()
        one = self.approved("一条有概括的。")
        self.approved("一条没有概括的。")
        summary.save(self.store.root, {one.id: {
            "line": "一句", "quote": "一条有概括的",
            "body_hash": summary.body_hash(one.body),
            "model": "m", "at": "2026-11-25T00:00:00+08:00",
        }})
        status, body = self.request("GET", "/api/overview", token=app.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["summaries"], {"total": 2, "covered": 1, "missing": 1})

if __name__ == "__main__":
    unittest.main()
