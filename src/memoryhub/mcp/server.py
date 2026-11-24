"""Read-only MCP endpoint (P5-01).

Three tools, all read-only, on purpose:

- ``memory_search``  substring search over approved, still-valid memories
- ``memory_get``     one memory by id, including its provenance
- ``memory_list``    approved, still-valid memories by category

Why no ``memory_write`` / ``memory_propose`` yet: ADR-002 forbids write-back and
ADR-003 requires human review, so a client must not be able to push memory into
the vault over MCP. Read-only is the smallest surface that is still useful, and
every result carries provenance and validity so the client can say "this is
where that came from, and this is whether it still holds".

Filtering rule (ADR-012): search and list return only ``status=approved``
entries whose ``valid_until`` has not passed. ``memory_get`` returns any status
so a human can ask about a pending proposal by id, and the payload always
includes ``status`` / ``trust`` so the caller is never misled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from .. import __version__
from ..config import resolve_vault
from ..models import CATEGORIES, MemoryEntry
from ..safety import findings
from ..storage.markdown_store import VaultStore

#: Only reviewed entries are visible to other AI tools (ADR-003).
VISIBLE_STATUSES = ("approved",)

#: A proposal bigger than this is not a memory, it is a document.
MAX_PROPOSAL_CHARS = 4000

#: Capability tiers, decided by whoever launches the server.
#:
#: Deliberately **not** keyed on client identity: MCP's ``clientInfo`` is
#: self-reported and unauthenticated, so "Cursor gets propose, Claude gets read"
#: would be a security theatre. On stdio the only trustworthy boundary is the
#: command line / environment of the process the user started. No tier grants a
#: write that skips review -- ``memory_write`` does not exist at any level.
PERMISSIONS = ("read", "propose", "admin")
RANK = {name: index for index, name in enumerate(PERMISSIONS)}

INSTRUCTIONS = (
    "MemoryHub serves the user's own, human-reviewed personal memory. "
    "Every result carries source_adapter/source_path/created_at so you can tell "
    "the user where a fact came from, and valid_until/superseded_by so you can "
    "tell whether it still holds. Prefer memory_search over guessing.\n\n"
    "memory_propose hands a candidate memory back to MemoryHub. Use it when the "
    "user states a durable preference, a correction, a project constraint, or a "
    "decision that would be worth remembering next session. Proposals are NOT "
    "live: they land in a review queue and the user approves them. Propose one "
    "fact per call, in the user's own words, and never include credentials."
)


def _payload(entry: MemoryEntry) -> Dict[str, Any]:
    """Provenance is the product: never return a bare string."""
    return {
        "id": entry.id,
        "body": entry.body.strip(),
        "category": entry.category,
        "tags": list(entry.tags),
        "status": entry.status,
        "trust": entry.trust,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "valid_from": entry.valid_from,
        "valid_until": entry.valid_until,
        "supersedes": list(entry.supersedes),
        "superseded_by": entry.superseded_by,
        "source_adapter": entry.source_adapter,
        "source_kind": entry.source_kind,
        "source_path": entry.source_path,
        "layer": entry.layer,
        "precedence": entry.precedence,
    }


def _visible(entry: MemoryEntry) -> bool:
    return entry.status in VISIBLE_STATUSES and not entry.is_expired()


def _newest_first(entries: List[MemoryEntry]) -> List[MemoryEntry]:
    return sorted(entries, key=lambda item: (item.updated_at, item.id), reverse=True)


def search(store: VaultStore, query: str, limit: int = 10) -> Dict[str, Any]:
    needle = query.strip()
    if not needle:
        return {"query": query, "returned": 0, "total": 0, "results": []}
    hits = [entry for entry in store.search(needle) if _visible(entry)]
    ordered = _newest_first(hits)
    return {
        "query": needle,
        "total": len(ordered),
        "returned": len(ordered[:limit]),
        "truncated": len(ordered) > limit,
        "results": [_payload(entry) for entry in ordered[:limit]],
    }


def listing(
    store: VaultStore,
    category: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    entries, _errors = store.scan()
    hits = [entry for entry in entries if _visible(entry)]
    if category:
        hits = [entry for entry in hits if entry.category == category]
    ordered = _newest_first(hits)
    return {
        "category": category,
        "total": len(ordered),
        "returned": len(ordered[:limit]),
        "truncated": len(ordered) > limit,
        "results": [_payload(entry) for entry in ordered[:limit]],
    }


def get(store: VaultStore, entry_id: str) -> Dict[str, Any]:
    entry = store.get(entry_id)
    if entry is None:
        return {"found": False, "id": entry_id}
    return {"found": True, "result": _payload(entry)}


def propose(
    store: VaultStore,
    body: str,
    category: str = "knowledge",
    tags: str = "",
) -> Dict[str, Any]:
    """Accept a candidate memory from an AI client.

    It can only ever create a ``pending`` entry: no path through this function
    reaches ``status=approved``. A body that looks like it carries a credential
    is stored as ``sensitivity=sensitive``, which ADR-003 turns into ``blocked``
    -- visible to the user, impossible to approve without lowering the flag.
    """
    if not isinstance(body, str) or not body.strip():
        return {"created": False, "reason": "body is required"}
    if len(body) > MAX_PROPOSAL_CHARS:
        return {
            "created": False,
            "reason": f"body is {len(body)} chars, over the {MAX_PROPOSAL_CHARS} limit",
        }
    if category not in CATEGORIES:
        return {
            "created": False,
            "reason": f"unknown category {category!r}; expected one of {list(CATEGORIES)}",
        }

    flags = findings(body)
    entry = MemoryEntry.create(
        category=category,
        body=body.strip() + "\n",
        tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
        source_adapter="mcp",
        source_kind="doc",
        sensitivity="sensitive" if flags else "normal",
    )
    store.create(entry)
    return {
        "created": True,
        "id": entry.id,
        "status": entry.status,
        "trust": entry.trust,
        "needs_review": True,
        "flagged": flags,
        "note": (
            "stored in the review queue; it is not visible to memory_search "
            "until the user approves it"
        ),
    }


def status(store: VaultStore, permission: str = "propose") -> Dict[str, Any]:
    """Queue depth and index health, for a client that wants to orient itself."""
    entries, errors = store.scan()
    counts: Dict[str, int] = {}
    blocked = 0
    for entry in entries:
        counts[entry.status] = counts.get(entry.status, 0) + 1
        if entry.trust == "blocked":
            blocked += 1
    return {
        "permission": permission,
        "counts": counts,
        "blocked": blocked,
        "unreadable": len(errors),
        "expired": sum(1 for entry in entries if entry.is_expired()),
        "tools": list(TOOLS_BY_PERMISSION[permission]),
    }


#: Which tools each tier exposes. Registered tools are what clients discover via
#: ``tools/list``, so an absent tool is a capability the server truly does not have.
TOOLS_BY_PERMISSION = {
    "read": ("memory_search", "memory_get", "memory_list"),
    "propose": ("memory_search", "memory_get", "memory_list", "memory_propose"),
    "admin": (
        "memory_search",
        "memory_get",
        "memory_list",
        "memory_propose",
        "memory_status",
    ),
}


def build_server(vault: Path, permission: str = "propose") -> MCPServer:
    """Bind the tools allowed by ``permission`` to one vault."""
    if permission not in RANK:
        raise ValueError(f"unknown permission {permission!r}; expected {PERMISSIONS}")
    store = VaultStore(Path(vault))
    server: MCPServer = MCPServer(
        name="memoryhub",
        version=__version__,
        instructions=INSTRUCTIONS + f"\n\nPermission level: {permission}.",
    )

    @server.tool(
        description="Search the user's reviewed personal memory by substring. "
        "Returns provenance and validity for every hit."
    )
    def memory_search(query: str, limit: int = 10) -> str:
        return json.dumps(search(store, query, limit), ensure_ascii=False)

    @server.tool(
        description="Fetch one memory by id, including its status and provenance. "
        "Works for pending entries too, so you can show a proposal that awaits review."
    )
    def memory_get(entry_id: str) -> str:
        return json.dumps(get(store, entry_id), ensure_ascii=False)

    @server.tool(
        description="List reviewed memories, newest first. Optionally filter by "
        "category (profile/preference/project/knowledge/decision)."
    )
    def memory_list(category: str = "", limit: int = 20) -> str:
        return json.dumps(
            listing(store, category or None, limit), ensure_ascii=False
        )

    if RANK[permission] >= RANK["propose"]:

        @server.tool(
            description="Propose a memory for the user to review. It lands in a "
            "review queue (never live immediately) and is not searchable until "
            "approved. One fact per call; never include credentials."
        )
        def memory_propose(body: str, category: str = "knowledge", tags: str = "") -> str:
            return json.dumps(
                propose(store, body, category, tags), ensure_ascii=False
            )

    if RANK[permission] >= RANK["admin"]:

        @server.tool(
            description="Report how many memories await review and whether the "
            "index is healthy. Read-only."
        )
        def memory_status() -> str:
            return json.dumps(status(store, permission), ensure_ascii=False)

    return server


def serve(vault: Optional[str] = None, permission: str = "propose") -> None:
    """Run the server over stdio. stdout belongs to the protocol."""
    resolved = resolve_vault(vault)
    if not Path(resolved).is_dir():
        raise SystemExit(
            f"error: vault not found: {resolved}; run `memoryhub init` first"
        )
    build_server(resolved, permission).run("stdio")
