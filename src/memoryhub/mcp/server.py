"""MCP endpoint (P5-01, extended by P5-07).

Three read tools plus three context shortcuts and one proposal tool:

- ``memory_search``            substring search over approved, still-valid memories
- ``memory_get``               one memory by id, including its provenance
- ``memory_list``              approved, still-valid memories by category
- ``memory_profile``           who the user is, in one call
- ``memory_project_context``   everything the vault says about one project
- ``memory_recent_decisions``  the decisions that still govern the work
- ``memory_propose``           hand a candidate back for human review

The three context tools exist because "call ``memory_list`` three times and
merge the results yourself" is a instruction a client will get wrong: it will
forget ``decision``, or ask for ``projects`` (plural) and receive an empty list
without noticing. Each of them answers one question an AI actually asks at the
start of a session, and each says plainly when it found nothing.

``memory_propose`` can only ever create a ``pending`` entry -- no path through
this module reaches ``status=approved``, and ``memory_write`` does not exist at
any tier. Read-back to the clients' own memory files is ``sync``'s job, on the
command line, under human control.

Filtering rule (ADR-012): search and list return only ``status=approved``
entries whose ``valid_until`` has not passed. ``memory_get`` returns any status
so a human can ask about a pending proposal by id, and the payload always
includes ``status`` / ``trust`` so the caller is never misled.

Every read call is appended to ``logs/access.jsonl`` (see :mod:`memoryhub.access`)
so "who has read this memory" is answerable later. The log records the
``--label`` the user typed into that client's config, never MCP's self-reported
``clientInfo``: that field is unauthenticated and a tier keyed on it would be
security theatre (ADR-014).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

from .. import __version__
from .. import access as access_log
from ..config import resolve_vault
from ..models import CATEGORIES, MemoryEntry
from ..safety import classify
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


def profile(store: VaultStore, limit: int = 12) -> Dict[str, Any]:
    """Who the user is, in one call.

    Exists because a client that starts a session by asking "tell me about the
    user" will otherwise compose that answer out of three guessed category
    names, and get one of them wrong silently.
    """
    data = listing(store, "profile", limit)
    if not data["results"]:
        data["note"] = (
            "the vault has no approved profile memory yet; ask the user instead "
            "of assuming anything"
        )
    return data


def project_context(store: VaultStore, name: str, limit: int = 20) -> Dict[str, Any]:
    """Everything still valid that mentions ``name``, projects and decisions only.

    The match is a plain case-insensitive substring over body, id and tags --
    there is no project identity in the data model, so pretending to resolve one
    would be a lie dressed as a feature. ``matched_on`` says which field hit, so
    the caller can judge a weak match for itself.
    """
    needle = name.strip().lower()
    if not needle:
        return {"project": name, "total": 0, "returned": 0, "results": [],
                "note": "name is required"}
    entries, _errors = store.scan()
    hits = []
    matched: Dict[str, List[str]] = {}
    for entry in entries:
        if not _visible(entry) or entry.category not in ("project", "decision"):
            continue
        fields = []
        if needle in entry.body.lower():
            fields.append("body")
        if any(needle in tag.lower() for tag in entry.tags):
            fields.append("tags")
        if needle in entry.id.lower():
            fields.append("id")
        if fields:
            hits.append(entry)
            matched[entry.id] = fields
    ordered = _newest_first(hits)
    results = []
    for entry in ordered[:limit]:
        item = _payload(entry)
        item["matched_on"] = matched[entry.id]
        results.append(item)
    return {
        "project": name,
        "total": len(ordered),
        "returned": len(results),
        "truncated": len(ordered) > limit,
        "results": results,
        "note": "" if ordered else (
            f"nothing approved mentions {name!r}; this may be a project the "
            "vault has never recorded"
        ),
    }


def recent_decisions(store: VaultStore, limit: int = 10) -> Dict[str, Any]:
    """Decisions, newest decided first -- not newest edited.

    ``created_at`` is the right order here: a decision made in September and
    re-read in November is still a September decision.
    """
    entries, _errors = store.scan()
    hits = [entry for entry in entries if _visible(entry) and entry.category == "decision"]
    ordered = sorted(hits, key=lambda item: (item.created_at, item.id), reverse=True)
    return {
        "total": len(ordered),
        "returned": len(ordered[:limit]),
        "truncated": len(ordered) > limit,
        "results": [_payload(entry) for entry in ordered[:limit]],
    }


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

    level, flags = classify(body)
    entry = MemoryEntry.create(
        category=category,
        body=body.strip() + "\n",
        tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
        source_adapter="mcp",
        source_kind="doc",
        sensitivity=level,
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
#: The three context shortcuts are read-only, so every tier gets them.
_READ_TOOLS = (
    "memory_search",
    "memory_get",
    "memory_list",
    "memory_profile",
    "memory_project_context",
    "memory_recent_decisions",
)

TOOLS_BY_PERMISSION = {
    "read": _READ_TOOLS,
    "propose": _READ_TOOLS + ("memory_propose",),
    "admin": _READ_TOOLS + ("memory_propose", "memory_status"),
}


def _ids_of(payload: Dict[str, Any]) -> List[str]:
    """Pull the entry ids out of a search/list/get payload, for the access log."""
    results = payload.get("results")
    if isinstance(results, list):
        return [str(item.get("id", "")) for item in results if item.get("id")]
    single = payload.get("result")
    if isinstance(single, dict) and single.get("id"):
        return [str(single["id"])]
    return []


def build_server(
    vault: Path,
    permission: str = "propose",
    label: str = "",
) -> MCPServer:
    """Bind the tools allowed by ``permission`` to one vault.

    ``label`` is the name the user typed into this client's config
    (``--label claude-code``). It is only used for the access log: capability
    still keys on ``permission``, because a label is a name the caller chose for
    itself and must never widen what it can do.
    """
    if permission not in RANK:
        raise ValueError(f"unknown permission {permission!r}; expected {PERMISSIONS}")
    store = VaultStore(Path(vault))
    name = access_log.label_from(label)
    server: MCPServer = MCPServer(
        name="memoryhub",
        version=__version__,
        instructions=(
            INSTRUCTIONS
            + f"\n\nPermission level: {permission}."
            + (f"\nReading as: {name}." if name else "")
        ),
    )

    def logged(tool: str, payload: Dict[str, Any]) -> str:
        access_log.record(store.root, tool, _ids_of(payload), permission, name)
        return json.dumps(payload, ensure_ascii=False)

    @server.tool(
        description="Search the user's reviewed personal memory by substring. "
        "Returns provenance and validity for every hit."
    )
    def memory_search(query: str, limit: int = 10) -> str:
        return logged("memory_search", search(store, query, limit))

    @server.tool(
        description="Fetch one memory by id, including its status and provenance. "
        "Works for pending entries too, so you can show a proposal that awaits review."
    )
    def memory_get(entry_id: str) -> str:
        return logged("memory_get", get(store, entry_id))

    @server.tool(
        description="List reviewed memories, newest first. Optionally filter by "
        "category (profile/preference/project/knowledge/decision)."
    )
    def memory_list(category: str = "", limit: int = 20) -> str:
        return logged("memory_list", listing(store, category or None, limit))

    @server.tool(
        description="Who the user is: identity, background, the facts that do not "
        "change. Call this once at the start of a session instead of asking the "
        "user to re-introduce themselves."
    )
    def memory_profile(limit: int = 12) -> str:
        return logged("memory_profile", profile(store, limit))

    @server.tool(
        description="Everything the vault still holds about one project, including "
        "its decisions, matched by name. Empty means the vault has never recorded "
        "that project; say so rather than guessing."
    )
    def memory_project_context(name: str, limit: int = 20) -> str:
        return logged("memory_project_context", project_context(store, name, limit))

    @server.tool(
        description="Decisions that still govern the user's work, newest decided "
        "first. Each carries its date and provenance."
    )
    def memory_recent_decisions(limit: int = 10) -> str:
        return logged("memory_recent_decisions", recent_decisions(store, limit))

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
            return json.dumps(
                {**status(store, permission), "access": access_log.summary(store.root)},
                ensure_ascii=False,
            )

    return server


def serve(
    vault: Optional[str] = None,
    permission: str = "propose",
    label: str = "",
) -> None:
    """Run the server over stdio. stdout belongs to the protocol."""
    resolved = resolve_vault(vault)
    if not Path(resolved).is_dir():
        raise SystemExit(
            f"error: vault not found: {resolved}; run `memoryhub init` first"
        )
    build_server(resolved, permission, label).run("stdio")
