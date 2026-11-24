"""Cheap secret and PII heuristics (P5-03).

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


# ---------------------------------------------------------------------- PII
#
# Two strengths on purpose. Treating every city name as "sensitive" would block
# half of a normal memoir ("在上海读书四年"), and a rule that fires constantly gets
# ignored. So:
#
#   strong  -> ``sensitive``  -> ADR-003 blocks it until a human lowers the flag
#   weak    -> ``personal``   -> flagged for review, still approvable
#
# The boundary is deliberately conservative: strong PII is the kind that identifies
# a person on its own (full address, phone, national ID, bank card).

_PII_STRONG: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("national id", re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                               r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")),
    ("phone number", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("bank card", re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4,7}(?!\d)")),
    # "北京市示例区示例路 390 号" and "示例区示例路 390 号" must both match:
    # the province/city prefix is optional, and a space before the number is normal.
    ("full address", re.compile(
        r"[\u4e00-\u9fa5]{2,}(?:省|市)?[\u4e00-\u9fa5]{0,10}(?:区|县)"
        r"[\u4e00-\u9fa5]{1,10}(?:路|街|道)[\u4e00-\u9fa5\d\s]{0,10}\d+\s*号"
    )),
)

_PII_WEAK: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("hometown or district", re.compile(r"[\u4e00-\u9fa5]{2,}(?:省|市|区|县)")),
    ("school", re.compile(r"[\u4e00-\u9fa5]{2,}(?:大学|学院|中学|小学)")),
)

LEVELS = ("normal", "personal", "sensitive", "secret")


def classify(text: str) -> Tuple[str, List[str]]:
    """Return ``(sensitivity_level, findings)`` for a piece of text.

    ``secret`` wins over ``sensitive``, which wins over ``personal``: the strongest
    signal decides, because a memory containing a credential is not made safe by
    the rest of the sentence.
    """
    secrets = findings(text)
    if secrets:
        return "secret", secrets
    if not isinstance(text, str):
        return "normal", []
    strong = [label for label, pattern in _PII_STRONG if pattern.search(text)]
    if strong:
        return "sensitive", strong
    weak = [label for label, pattern in _PII_WEAK if pattern.search(text)]
    if weak:
        return "personal", weak
    return "normal", []
