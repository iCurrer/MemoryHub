"""Read-only MCP endpoint (P5-01).

Unit tests cover the filtering rules; one integration test speaks real JSON-RPC
over stdio, because that is the only way to prove two things this project keeps
getting wrong: the handshake works, and nothing pollutes stdout.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.mcp import get, listing, search  # noqa: E402
from memoryhub.models import MemoryEntry  # noqa: E402
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
        self.assertEqual(names, ["memory_get", "memory_list", "memory_search"])
        # Read-only surface: no write/propose tool may ever appear here (ADR-002).
        self.assertFalse(any("write" in name or "propose" in name for name in names))

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


if __name__ == "__main__":
    unittest.main()
