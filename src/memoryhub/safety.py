"""Cheap secret heuristics (first slice of P5-03).

This is **not** a detector and must not be described as one. It exists because
opening a ``memory_propose`` write path without any guard would let a
prompt-injected client push a credential into the vault as an ordinary memory,
and ordinary memories are auto-approvable at the ``auto`` trust tier.

It catches shapes that are cheap and unambiguous. Anything it misses is covered
by ADR-003's rule that AI output stays ``pending`` until a human approves it.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer credential", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|password|passwd|secret|access[_-]?token|"
            r"auth[_-]?token)\b\s*[:=]\s*\S{8,}"
        ),
    ),
)


def findings(text: str) -> List[str]:
    """Labels for every secret shape found. Empty list means "nothing obvious"."""
    if not isinstance(text, str):
        return []
    return [label for label, pattern in _PATTERNS if pattern.search(text)]


def looks_sensitive(text: str) -> bool:
    return bool(findings(text))
