"""A local page for the two queues nobody opens a terminal for (P6-03).

Why this exists before the packaging work it was scheduled behind: the backlog
produces two kinds of item -- pending memories and conflict proposals -- and the
only way to act on either was a TTY loop or a JSON file. 24 proposals were
generated on 2026-11-25 and none were read, not because they were wrong but
because reading one meant ``cat inbox/conflicts/*.json``. A queue with no
surface is a queue that does not exist.

Shape, and the reasoning behind each choice:

- **stdlib only.** ``http.server`` + one HTML file, no build step, no bundle, no
  node_modules. The project's whole dependency budget is two packages; a
  dashboard is not worth a third.
- **loopback + token.** Binds ``127.0.0.1`` and refuses any other ``Host``
  header (a DNS-rebinding page can reach 127.0.0.1 from the browser, and the
  Host check is what stops it). Every request carries a per-run token in a
  header, never a cookie -- a cookie would make cross-site requests
  authenticated, a custom header cannot be set cross-origin without a preflight
  this server never grants.
- **The page is a view, not a second implementation.** Every write goes through
  the same functions the CLI calls (``store.set_status``,
  ``conflicts.apply_supersede``), so the four guards on the supersede chain and
  the ``blocked`` refusal apply here without being restated. A UI that
  re-implements the rules would eventually disagree with them.
- **No vault edit from the browser beyond the two decisions.** Reading is open;
  approving, rejecting, re-categorising and superseding are the four things the
  CLI already allows. ``sync`` is shown as a plan only: it rewrites files three
  other tools load on start-up, and that stays a command a human types.
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import conflicts, sync
from .models import CATEGORIES, MemoryEntry, ValidationError
from .storage.markdown_store import StaleEntryError, VaultStore

#: Injected into the page so the browser can talk back. The placeholder exists
#: so the file on disk contains no secret -- a token committed to the repo would
#: be a token in every clone.
TOKEN_PLACEHOLDER = "__MEMORYHUB_TOKEN__"

_PAGE = Path(__file__).with_name("webui") / "index.html"

#: One request at a time. The store's own lock covers file writes, but the
#: read-modify-write in ``set_status`` spans two calls and two browser tabs are
#: a normal thing to have open.
_WRITE_LOCK = threading.Lock()

_MAX_BODY = 64 * 1024

#: Lines that start a topic inside an entry: a heading, a bold bullet, a
#: numbered item. Used by :func:`is_crowded` -- see the note there.
_TOPIC_LINE = re.compile(r"^\s*(?:#{2,4}\s+|[-*]\s+\*\*|\d+\.\s+)", re.M)


def is_crowded(entry: MemoryEntry) -> bool:
    """True when one entry holds several topics that age independently.

    The real library's only real conflict (2026-11-25) is 401 characters titled
    "个人工作台「我的大四」": work directories, usage preferences and a file
    list, plus one sentence about the thesis that went stale. Superseding it
    would hide all four things to retire one. The CLI warns about this and
    refuses to split; the page has to warn too, because a button makes the wrong
    move *easier* than typing an id did.

    Threshold is deliberately crude -- length plus counted topic lines -- and it
    is only ever used to *show a warning*, never to block a supersede. A wrong
    "crowded" costs a sentence of reading; a wrong "fine" costs three memories.
    """
    if len(entry.body) >= 180:
        return True
    return len(_TOPIC_LINE.findall(entry.body)) >= 3


def load_page(token: str) -> str:
    html = _PAGE.read_text(encoding="utf-8")
    return html.replace(TOKEN_PLACEHOLDER, token)


# ---------------------------------------------------------------------- headline

#: Longest headline before it is clipped. 48 characters is about one line at the
#: page's column width; longer and the "scan one line per entry" property is lost.
MAX_HEADLINE = 48

_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


def headline(entry: MemoryEntry) -> Dict[str, Any]:
    """The one line you read while scanning a list -- **taken**, not written.

    Every candidate is a substring of the entry, so a headline can never state
    something the entry does not. That is the whole reason this is extraction
    rather than summarisation: the project's only automated defence against
    invention is "the model may copy, never compose" (ADR-016), and a generated
    title would be the first place it gets violated -- in the most visible
    position on the page.

    What it gives up: a 401-character entry about a workbench reads as
    "个人工作台「我的大四」", not as a sentence explaining what changed. That
    sentence has to come from a synthesis layer (P3-03), which needs finer
    evidence than these entries currently have. Until then, an honest label
    beats a fluent guess.

    ``skip`` is how many leading lines the headline consumed, so the body is not
    shown with its own title repeated underneath it.
    """
    lines = entry.body.strip().splitlines()
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            return {"text": _clip(match.group(1)), "skip": index + 1}
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            # Not skipped: a bullet's siblings are the rest of the list, and
            # dropping the first one would leave the list starting mid-way.
            return {"text": _clip(_plain(match.group(1))), "skip": 0}
    first = next((line for line in lines if line.strip()), "")
    return {"text": _clip(_plain(first)), "skip": 0}


def _plain(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def _clip(text: str, limit: int = MAX_HEADLINE) -> str:
    text = re.sub(r"\s+", " ", _plain(text)).strip()
    if len(text) <= limit:
        # A trailing colon is the mark of "here comes the list"; as a headline it
        # just dangles ("不要过度迎合，不做无条件赞同的 AI：").
        return text.rstrip("：:")
    return text[:limit].rstrip(" ，。、；：,;:") + "…"


# --------------------------------------------------------------------- payloads


def entry_brief(entry: MemoryEntry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "category": entry.category,
        "body": entry.body,
        "status": entry.status,
        "trust": entry.trust,
        "sensitivity": entry.sensitivity,
        "confidence": entry.confidence,
        "source_adapter": entry.source_adapter,
        "source_kind": entry.source_kind,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "superseded_by": entry.superseded_by,
        "supersedes": list(entry.supersedes),
        "tags": list(entry.tags),
        "chars": len(entry.body),
        "lines": len(entry.body.strip().splitlines()),
        "crowded": is_crowded(entry),
        "headline": headline(entry),
    }


class App:
    """The read models and the four write actions, minus HTTP."""

    def __init__(self, store: VaultStore, token: str, *, allow_write: bool = True):
        self.store = store
        self.token = token
        self.allow_write = allow_write

    # ------------------------------------------------------------- read side

    def overview(self) -> Dict[str, Any]:
        entries, unreadable = self.store.scan()
        by_status: Dict[str, int] = {}
        by_category: Dict[str, int] = {}
        for entry in entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            by_category[entry.category] = by_category.get(entry.category, 0) + 1
        proposals, problems = conflicts.load_records(self.store.root)
        live = sync.select(entries)
        plans = sync.plan(entries, sync.default_targets())
        block_chars = len(plans[0].block) if plans else 0
        return {
            "vault": str(self.store.root),
            "total": len(entries),
            "by_status": by_status,
            "by_category": by_category,
            "unreadable": unreadable,
            "conflicts": {
                "open": len(proposals),
                "problems": problems,
                "by_relation": _count_relation(proposals),
            },
            "sync": {
                "live": len(live),
                "block_chars": block_chars,
                "warn_chars": sync.WARN_BLOCK_CHARS,
                "targets": [
                    {
                        "key": plan.target.key,
                        "label": plan.target.label,
                        "path": str(plan.path) if plan.path else plan.target.pattern,
                        "exists": plan.exists,
                        "changes": plan.changes,
                        "note": plan.note,
                    }
                    for plan in plans
                ],
            },
        }

    def pending(self) -> List[Dict[str, Any]]:
        entries, _ = self.store.scan()
        queue = [entry for entry in entries if entry.status == "pending"]
        # Blocked first: they cannot be approved, and finding that out after
        # reading forty entries is the one thing the CLI loop cannot avoid.
        queue.sort(key=lambda e: (e.trust != "blocked", e.created_at, e.id))
        return [entry_brief(entry) for entry in queue]

    def proposals(self) -> List[Dict[str, Any]]:
        records, _ = conflicts.load_records(self.store.root)
        out: List[Dict[str, Any]] = []
        for record in records:
            pair = record.get("pair") or []
            left = self.store.get(str(pair[0])) if len(pair) == 2 else None
            right = self.store.get(str(pair[1])) if len(pair) == 2 else None
            out.append(
                {
                    "left_id": str(pair[0]) if len(pair) == 2 else "",
                    "right_id": str(pair[1]) if len(pair) == 2 else "",
                    "relation": record.get("relation"),
                    "scope": record.get("scope"),
                    "winner": record.get("winner"),
                    "reason": record.get("reason"),
                    "verified": record.get("verified"),
                    "detected_at": record.get("detected_at"),
                    "model": record.get("model"),
                    "claims": record.get("claims") or [],
                    "left": entry_brief(left) if left else None,
                    "right": entry_brief(right) if right else None,
                    # A proposal whose entries have moved on cannot be applied
                    # by the browser: the ids still resolve, but a status change
                    # since detection makes the pair's premise stale.
                    "stale": _stale(record, left, right),
                }
            )
        return out

    # ------------------------------------------------------------ write side

    def review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_write()
        entry_id = str(payload.get("id") or "")
        action = str(payload.get("action") or "")
        with _WRITE_LOCK:
            entry = self.store.get(entry_id)
            if entry is None:
                raise HTTPError(404, f"没有这条记忆：{entry_id}")
            if action == "approve":
                if entry.trust == "blocked":
                    # Same refusal as the CLI, restated as a reason rather than
                    # a missing button: the fix (lower `sensitivity`) is a
                    # deliberate act, so the user should read why it is needed.
                    raise HTTPError(
                        409,
                        "这条是 blocked：直接批准会把它送进每个接上来的 AI 的上下文。"
                        "要批必须先降 sensitivity（memoryhub 里手改那一条）。",
                    )
                self.store.set_status(entry_id, "approved")
            elif action == "reject":
                self.store.set_status(entry_id, "rejected")
            elif action == "category":
                chosen = str(payload.get("category") or "")
                if chosen not in CATEGORIES:
                    raise HTTPError(400, f"未知分类：{chosen}")
                entry.category = chosen
                self.store.save(entry)
            else:
                raise HTTPError(400, f"未知动作：{action}")
        fresh = self.store.get(entry_id)
        return {"ok": True, "entry": entry_brief(fresh) if fresh else None}

    def supersede(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_write()
        old_id = str(payload.get("old") or "")
        new_id = str(payload.get("new") or "")
        note = str(payload.get("note") or "")
        with _WRITE_LOCK:
            retired, kept = conflicts.apply_supersede(
                self.store, old_id, new_id, note=note
            )
            conflicts.resolve_record(
                self.store.root, old_id, new_id, keep=new_id, retired=old_id, note=note
            )
        return {
            "ok": True,
            "retired": entry_brief(retired),
            "kept": entry_brief(kept),
        }

    def _require_write(self) -> None:
        if not self.allow_write:
            raise HTTPError(403, "本进程以只读启动（--read-only），改状态请回命令行")


def _count_relation(records: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records:
        relation = str(record.get("relation") or "unknown")
        counts[relation] = counts.get(relation, 0) + 1
    return counts


def _stale(
    record: Dict[str, Any],
    left: Optional[MemoryEntry],
    right: Optional[MemoryEntry],
) -> Optional[str]:
    if left is None or right is None:
        return "提案里有一侧的记忆已经不在库里"
    if left.status != "approved" or right.status != "approved":
        return f"有一侧不是 approved（{left.status} / {right.status}）"
    if left.superseded_by or right.superseded_by:
        return "有一侧已经被取代过"
    return None


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------- transport


class Handler(BaseHTTPRequestHandler):
    server_version = "memoryhub"
    protocol_version = "HTTP/1.1"
    app: App  # set by make_server

    # -- helpers

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _token_ok(self) -> bool:
        given = self.headers.get("X-MemoryHub-Token") or ""
        if not given:
            given = (parse_qs(urlparse(self.path).query).get("t") or [""])[0]
        return bool(given) and secrets.compare_digest(given, self.app.token)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page is inline CSS/JS; a CSP that forbids remote loads makes the
        # "no telemetry, nothing leaves the machine" claim checkable.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; form-action 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence. The console is where the URL and the token are printed, and a
        # per-request line buries them.
        return

    # -- verbs

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "只接受本机访问（Host 头不是环回地址）"})
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            # The shell is served without a token on purpose. It contains no
            # data -- every fact comes from /api/*, which still requires one.
            # Refusing here made reloading the page a dead end: the script had
            # rewritten the URL to drop ?t=, so F5 produced a page of raw 401
            # JSON with no way back. Inject the token when the request proves
            # it, leave the placeholder-derived empty string otherwise and let
            # the page fall back to the token it kept in sessionStorage.
            token = self.app.token if self._token_ok() else ""
            self._send(
                200, load_page(token).encode("utf-8"), "text/html; charset=utf-8"
            )
            return
        if not self._token_ok():
            self._json(401, {"ok": False, "error": "缺少或错误的 token"})
            return
        try:
            if path == "/api/overview":
                self._json(200, self.app.overview())
            elif path == "/api/pending":
                self._json(200, {"entries": self.app.pending()})
            elif path == "/api/proposals":
                self._json(200, {"proposals": self.app.proposals()})
            else:
                self._json(404, {"ok": False, "error": "没有这个地址"})
        except Exception as exc:  # noqa: BLE001 - a 500 must still be JSON
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "只接受本机访问"})
            return
        if not self._token_ok():
            self._json(401, {"ok": False, "error": "缺少或错误的 token"})
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self._json(413, {"ok": False, "error": "请求体过大"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "请求体不是 JSON"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "请求体必须是对象"})
            return
        try:
            if path == "/api/review":
                self._json(200, self.app.review(payload))
            elif path == "/api/supersede":
                self._json(200, self.app.supersede(payload))
            else:
                self._json(404, {"ok": False, "error": "没有这个地址"})
        except HTTPError as exc:
            self._json(exc.status, {"ok": False, "error": exc.message})
        except (ValidationError, StaleEntryError, KeyError) as exc:
            # These are the store's and the supersede chain's own refusals; they
            # already say what to do next, so they go to the user verbatim.
            self._json(409, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def make_server(
    store: VaultStore,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    allow_write: bool = True,
    token: Optional[str] = None,
) -> Tuple[ThreadingHTTPServer, App]:
    app = App(store, token or secrets.token_urlsafe(24), allow_write=allow_write)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server, app


def serve(
    store: VaultStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_write: bool = True,
    open_browser: bool = True,
) -> None:
    server, app = make_server(store, host=host, port=port, allow_write=allow_write)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/?t={app.token}"
    print(f"memoryhub serve  →  {url}")
    print(f"  vault      {store.root}")
    print(f"  写入权限   {'允许（审核 / 取代）' if allow_write else '只读'}")
    print("  Ctrl-C 停止。这个地址只在本次进程有效。")
    # flush: without it the URL sits in the block buffer whenever stdout is a
    # pipe or a captured log, and the only handle to the page is the thing you
    # cannot see. Learned the hard way.
    sys.stdout.flush()
    if open_browser:
        threading.Thread(
            target=lambda: webbrowser.open(url), daemon=True
        ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
