"""MCP entry point (P5-01).

Kept in its own package so the transport never leaks into the core: ``storage``
knows nothing about JSON-RPC, and the server knows nothing about how memories are
stored beyond ``VaultStore``.
"""

from __future__ import annotations

from .server import (
    MAX_PROPOSAL_CHARS,
    PERMISSIONS,
    TOOLS_BY_PERMISSION,
    VISIBLE_STATUSES,
    build_server,
    get,
    listing,
    propose,
    search,
    serve,
    status,
)

__all__ = [
    "MAX_PROPOSAL_CHARS",
    "PERMISSIONS",
    "TOOLS_BY_PERMISSION",
    "VISIBLE_STATUSES",
    "build_server",
    "get",
    "listing",
    "propose",
    "search",
    "serve",
    "status",
]
