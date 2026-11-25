"""MCP endpoint (P5-01, P5-07).

Unit tests cover the filtering rules; one integration test speaks real JSON-RPC
over stdio, because that is the only way to prove two things this project keeps
getting wrong: the handshake works, and nothing pollutes stdout.
"""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub import access  # noqa: E402
from memoryhub.mcp import (  # noqa: E402
    MAX_PROPOSAL_CHARS,
    TOOLS_BY_PERMISSION,
    build_server,
    get,
    listing,
    profile,
    project_context,
    propose,
    recent_decisions,
    search,
    status,
)
from memoryhub.models import MemoryEntry, ValidationError  # noqa: E402
from memoryhub.storage.markdown_store import VaultStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class McpToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VaultStore(Path(self._tmp.name) / "vault-root")
        self.store.init_layout()

        self.approved = MemoryEntry.create(
            category="preference", body="prefers python for tooling"
        )
        self.store.create(self.approved)
        self.store.set_status(self.approved.id, "approved")

        self.pending = MemoryEntry.create(
            category="profile", body="pending profile fact about python"
        )
        self.store.create(self.pending)

        self.decision = MemoryEntry.create(
            category="decision", body="use sqlite fts5 trigram"
        )
        self.store.create(self.decision)
        self.store.set_status(self.decision.id, "approved")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_search_returns_only_approved_entries(self) -> None:
        result = search(self.store, "python")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["id"], self.approved.id)
        self.assertEqual(result["results"][0]["status"], "approved")

    def test_search_hides_expired_entries(self) -> None:
        self.store.stamp_validity(
            self.decision.id,
            superseded_by=MemoryEntry.new_id(),
            valid_until="2020-01-01T00:00:00+08:00",
        )
        self.assertEqual(search(self.store, "sqlite")["total"], 0)

    def test_search_payload_carries_provenance_and_validity(self) -> None:
        payload = search(self.store, "python")["results"][0]
        for key in (
            "source_adapter",
            "source_kind",
            "source_path",
            "created_at",
            "updated_at",
            "valid_from",
            "valid_until",
            "superseded_by",
            "status",
            "trust",
            "tags",
            "layer",
            "precedence",
        ):
            self.assertIn(key, payload)

    def test_list_filters_by_category_and_hides_pending(self) -> None:
        every = listing(self.store)
        self.assertEqual(every["total"], 2)
        only_decisions = listing(self.store, category="decision")
        self.assertEqual([item["id"] for item in only_decisions["results"]], [self.decision.id])

    def test_list_truncates_and_reports_total(self) -> None:
        result = listing(self.store, limit=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["truncated"])

    def test_get_returns_pending_with_its_status(self) -> None:
        result = get(self.store, self.pending.id)
        self.assertTrue(result["found"])
        self.assertEqual(result["result"]["status"], "pending")
        self.assertEqual(result["result"]["trust"], "review")

    def test_get_unknown_id_is_not_an_error(self) -> None:
        self.assertEqual(get(self.store, "mem_20200101T000000_deadbeef"), {"found": False, "id": "mem_20200101T000000_deadbeef"})

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(search(self.store, "   ")["results"], [])


class McpProposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VaultStore(Path(self._tmp.name) / "vault-root")
        self.store.init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_proposal_is_pending_and_invisible_to_search(self) -> None:
        result = propose(self.store, "user prefers pnpm over npm", "preference")
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["needs_review"])

        entry = self.store.get(result["id"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, "pending")
        # The whole point of ADR-003: a proposal must not become truth by itself.
        self.assertEqual(search(self.store, "pnpm")["total"], 0)

    def test_approved_proposal_becomes_searchable(self) -> None:
        result = propose(self.store, "user prefers pnpm over npm", "preference")
        self.store.set_status(result["id"], "approved")
        self.assertEqual(search(self.store, "pnpm")["total"], 1)

    def test_empty_and_oversized_bodies_are_refused(self) -> None:
        self.assertFalse(propose(self.store, "   ")["created"])
        self.assertFalse(propose(self.store, "x" * (MAX_PROPOSAL_CHARS + 1))["created"])
        self.assertEqual(self.store.scan()[0], [])

    def test_unknown_category_is_refused(self) -> None:
        result = propose(self.store, "some fact", "nonsense")
        self.assertFalse(result["created"])
        self.assertIn("unknown category", result["reason"])

    def test_secret_shaped_body_is_blocked_not_approved(self) -> None:
        result = propose(self.store, "deploy key: sk-abcdefghijklmnopqrstuvwx123456", "knowledge")
        self.assertTrue(result["created"])
        self.assertEqual(result["trust"], "blocked")
        self.assertTrue(result["flagged"])
        with self.assertRaises(ValidationError):
            self.store.set_status(result["id"], "approved")

    def test_tags_are_split_and_provenance_is_recorded(self) -> None:
        result = propose(self.store, "uses sqlite fts5", "knowledge", "storage, sqlite")
        entry = self.store.get(result["id"])
        self.assertEqual(entry.tags, ["storage", "sqlite"])
        self.assertEqual(entry.source_adapter, "mcp")


class McpStdioTests(unittest.TestCase):
    """One real client session over stdin/stdout."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault-root"
        store = VaultStore(self.vault)
        store.init_layout()
        entry = MemoryEntry.create(category="preference", body="prefers python")
        store.create(entry)
        store.set_status(entry.id, "approved")

        env = {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "MEMORYHUB_HOME": str(self.root / "home"),
            "SYSTEMROOT": str(Path("C:/Windows")),
            "PATH": str(Path(sys.executable).parent),
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "memoryhub", "mcp", "--vault", str(self.vault)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=env,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.stderr: list[str] = []
        self.stdout_raw: list[str] = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(
            target=lambda: [self.stderr.append(line) for line in self.proc.stderr],
            daemon=True,
        ).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.stdout_raw.append(line)
            self.lines.put(line)

    def tearDown(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()
        self._tmp.cleanup()

    def call(self, message: dict, expect_id: int) -> dict:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                raw = self.lines.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            text = raw.strip()
            if not text:
                continue
            parsed = json.loads(text)  # a failure here means stdout was polluted
            if parsed.get("id") == expect_id:
                return parsed
        self.fail(f"no response for id={expect_id}; stderr={''.join(self.stderr)[:400]!r}")

    def test_full_session(self) -> None:
        init = self.call(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            1,
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "memoryhub")

        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self.proc.stdin.flush()

        tools = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
        names = sorted(tool["name"] for tool in tools["result"]["tools"])
        self.assertEqual(
            names,
            [
                "memory_get",
                "memory_list",
                "memory_profile",
                "memory_project_context",
                "memory_propose",
                "memory_recent_decisions",
                "memory_search",
            ],
        )
        # ADR-002: no client may write into the vault directly. Propose only
        # creates a pending entry, which is why it is allowed.
        self.assertFalse(any("write" in name for name in names))

        found = self.call(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": {"query": "python"}},
            },
            3,
        )
        payload = json.loads(found["result"]["content"][0]["text"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["results"][0]["body"], "prefers python")

        # Every line the server wrote to stdout must be a JSON-RPC message.
        for line in self.stdout_raw:
            if line.strip():
                self.assertIn("jsonrpc", json.loads(line))

class McpStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VaultStore(Path(self._tmp.name) / "vault-root")
        self.store.init_layout()
        approved = MemoryEntry.create(category="preference", body="prefers python")
        self.store.create(approved)
        self.store.set_status(approved.id, "approved")
        self.store.create(MemoryEntry.create(category="profile", body="pending fact"))
        blocked = MemoryEntry.create(
            category="knowledge", body="key: sk-abcdefghijklmnopqrstuvwx123456",
            sensitivity="sensitive",
        )
        self.store.create(blocked)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_status_counts_by_state(self) -> None:
        report = status(self.store, "admin")
        self.assertEqual(report["counts"].get("approved"), 1)
        self.assertEqual(report["counts"].get("pending"), 2)
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["unreadable"], 0)

    def test_status_reports_the_tools_of_its_own_tier(self) -> None:
        for permission, expected in TOOLS_BY_PERMISSION.items():
            self.assertEqual(status(self.store, permission)["tools"], list(expected))

    def test_tiers_only_grow(self) -> None:
        self.assertEqual(TOOLS_BY_PERMISSION["read"], (
            "memory_search",
            "memory_get",
            "memory_list",
            "memory_profile",
            "memory_project_context",
            "memory_recent_decisions",
        ))
        # The context shortcuts are reads; they must be present at every tier.
        for tier in TOOLS_BY_PERMISSION.values():
            for tool in ("memory_profile", "memory_project_context",
                         "memory_recent_decisions"):
                self.assertIn(tool, tier)
        self.assertIn("memory_propose", TOOLS_BY_PERMISSION["propose"])
        self.assertNotIn("memory_status", TOOLS_BY_PERMISSION["propose"])
        self.assertIn("memory_status", TOOLS_BY_PERMISSION["admin"])
        # No tier may ever expose a direct write (ADR-002).
        for tools in TOOLS_BY_PERMISSION.values():
            self.assertFalse(any("write" in name for name in tools))


class McpReadTierTests(unittest.TestCase):
    """A server started with --permission read must not advertise propose at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault-root"
        VaultStore(self.vault).init_layout()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tools_list_is_read_only(self) -> None:
        env = {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "MEMORYHUB_HOME": str(self.root / "home"),
            "SYSTEMROOT": str(Path("C:/Windows")),
            "PATH": str(Path(sys.executable).parent),
            "MEMORYHUB_MCP_PERMISSION": "",
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "memoryhub", "mcp", "--vault", str(self.vault),
             "--permission", "read"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, cwd=str(REPO_ROOT), env=env,
        )
        lines: "queue.Queue[str]" = queue.Queue()
        threading.Thread(
            target=lambda: [lines.put(line) for line in proc.stdout], daemon=True
        ).start()

        def send(payload: dict) -> None:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        def wait(message_id: int, timeout: float = 30) -> dict:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = lines.get(timeout=max(0.05, deadline - time.time()))
                except queue.Empty:
                    break
                if not raw.strip():
                    continue
                parsed = json.loads(raw)
                if parsed.get("id") == message_id:
                    return parsed
            self.fail(f"no response for id={message_id}")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}}})
            wait(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = wait(2)["result"]["tools"]
            self.assertEqual(
                sorted(tool["name"] for tool in tools),
                [
                    "memory_get",
                    "memory_list",
                    "memory_profile",
                    "memory_project_context",
                    "memory_recent_decisions",
                    "memory_search",
                ],
            )
        finally:
            proc.stdin.close()
            proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()


class McpContextToolsTests(unittest.TestCase):
    """The three shortcuts exist so a client does not have to guess category names."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = VaultStore(Path(self._tmp.name) / "vault-root")
        self.store.init_layout()

        self.me = MemoryEntry.create(
            category="profile", body="会计学 ACCA 全英班，2027 届", tags=["身份"]
        )
        self.project = MemoryEntry.create(
            category="project", body="MemoryHub：跨 AI 个人记忆治理层", tags=["memoryhub"]
        )
        self.old_decision = MemoryEntry.create(
            category="decision",
            body="用 FTS5 做检索，不引入向量库",
            created_at="2026-09-01T10:00:00+08:00",
        )
        self.new_decision = MemoryEntry.create(
            category="decision",
            body="回写走受管区块",
            created_at="2026-11-20T10:00:00+08:00",
        )
        self.unrelated = MemoryEntry.create(category="preference", body="喜欢 Python")
        for entry in (self.me, self.project, self.old_decision, self.new_decision, self.unrelated):
            self.store.create(entry)
            self.store.set_status(entry.id, "approved")

        self.pending_profile = MemoryEntry.create(category="profile", body="待审的身份事实")
        self.store.create(self.pending_profile)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_profile_returns_approved_profile_entries_only(self) -> None:
        data = profile(self.store)
        ids = [item["id"] for item in data["results"]]
        self.assertEqual(ids, [self.me.id])
        self.assertNotIn(self.pending_profile.id, ids)

    def test_profile_says_so_when_the_vault_is_empty(self) -> None:
        empty = VaultStore(Path(self._tmp.name) / "empty")
        empty.init_layout()
        data = profile(empty)
        self.assertEqual(data["results"], [])
        self.assertIn("ask the user", data["note"])

    def test_project_context_matches_body_and_tags(self) -> None:
        data = project_context(self.store, "memoryhub")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["id"], self.project.id)
        # The tag hit is reported, so a weak match can be judged by the caller.
        self.assertIn("body", data["results"][0]["matched_on"])
        self.assertIn("tags", data["results"][0]["matched_on"])

    def test_project_context_includes_decisions_about_that_project(self) -> None:
        entry = MemoryEntry.create(
            category="decision", body="MemoryHub 的写入口只有一个"
        )
        self.store.create(entry)
        self.store.set_status(entry.id, "approved")
        data = project_context(self.store, "memoryhub")
        self.assertEqual({item["id"] for item in data["results"]}, {self.project.id, entry.id})

    def test_project_context_ignores_unrelated_categories(self) -> None:
        data = project_context(self.store, "python")
        # ``unrelated`` is a preference, not a project or decision.
        self.assertEqual(data["total"], 0)

    def test_project_context_empty_is_explicit(self) -> None:
        data = project_context(self.store, "不存在的项目")
        self.assertEqual(data["results"], [])
        self.assertIn("never recorded", data["note"])

    def test_project_context_needs_a_name(self) -> None:
        self.assertEqual(project_context(self.store, "  ")["results"], [])

    def test_recent_decisions_order_by_when_decided_not_when_edited(self) -> None:
        data = recent_decisions(self.store)
        self.assertEqual(
            [item["id"] for item in data["results"]],
            [self.new_decision.id, self.old_decision.id],
        )

    def test_recent_decisions_respects_the_limit(self) -> None:
        data = recent_decisions(self.store, limit=1)
        self.assertEqual(data["returned"], 1)
        self.assertTrue(data["truncated"])


def invoke(vault: Path, permission: str, label: str, tool: str, **arguments: Any) -> dict:
    """Call one registered tool the way a client would, and decode the payload."""
    server = build_server(vault, permission, label)
    result = asyncio.run(server.call_tool(tool, arguments))
    return json.loads(result.content[0].text)


class McpAccessLogTests(unittest.TestCase):
    """Every read is logged, and the label comes from the command line (P5-07)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault-root"
        self.store = VaultStore(self.vault)
        self.store.init_layout()
        self.entry = MemoryEntry.create(category="preference", body="prefers python")
        self.store.create(self.entry)
        self.store.set_status(self.entry.id, "approved")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, tool: str, **arguments: Any) -> dict:
        return invoke(self.vault, "propose", "Claude-Code", tool, **arguments)

    def test_a_search_logs_the_label_typed_into_the_client_config(self) -> None:
        payload = self.call("memory_search", query="python")
        self.assertEqual(payload["total"], 1)
        rows = access.lines(self.vault)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "memory_search")
        self.assertEqual(rows[0]["label"], "claude-code")
        self.assertEqual(rows[0]["ids"], [self.entry.id])

    def test_a_get_logs_one_id_and_a_miss_logs_none(self) -> None:
        self.call("memory_get", entry_id=self.entry.id)
        self.call("memory_get", entry_id="mem_nope")
        rows = access.lines(self.vault)
        self.assertEqual([row["count"] for row in rows], [1, 0])

    def test_context_tools_are_logged_too(self) -> None:
        self.call("memory_profile")
        self.call("memory_project_context", name="python")
        self.call("memory_recent_decisions")
        self.assertEqual(
            [row["tool"] for row in access.lines(self.vault)],
            ["memory_profile", "memory_project_context", "memory_recent_decisions"],
        )

    def test_an_unlabelled_server_logs_unlabeled_rather_than_guessing(self) -> None:
        invoke(self.vault, "read", "", "memory_list")
        self.assertEqual(access.lines(self.vault)[0]["label"], "unlabeled")

    def test_propose_is_not_a_read_and_leaves_no_access_line(self) -> None:
        invoke(self.vault, "propose", "codex", "memory_propose", body="a durable preference")
        self.assertEqual(access.lines(self.vault), [])

    def test_status_reports_the_log_summary(self) -> None:
        self.call("memory_search", query="python")
        report = invoke(self.vault, "admin", "codex", "memory_status")
        self.assertEqual(report["access"]["calls"], 1)
        self.assertEqual(report["access"]["labels"], ["claude-code"])

if __name__ == "__main__":
    unittest.main()
