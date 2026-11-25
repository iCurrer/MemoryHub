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

from memoryhub import conflicts, web  # noqa: E402
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
    def test_page_needs_the_token(self) -> None:
        app = self.start()
        status, _ = self.request("GET", "/")
        self.assertEqual(status, 401)
        status, html = self.request("GET", f"/?t={app.token}")
        self.assertEqual(status, 200)
        self.assertIn(app.token, html)
        self.assertIn("MEMORYHUB", html)

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


if __name__ == "__main__":
    unittest.main()
