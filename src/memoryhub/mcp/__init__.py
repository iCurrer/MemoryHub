"""MCP entry point (P5-01).

Kept in its own package so the transport never leaks into the core: ``storage``
knows nothing about JSON-RPC, and the server knows nothing about how memories are
stored beyond ``VaultStore``.
"""

from __future__ import annotations

from .server import VISIBLE_STATUSES, build_server, get, listing, search, serve

__all__ = [
    "VISIBLE_STATUSES",
    "build_server",
    "get",
    "listing",
    "search",
    "serve",
]
