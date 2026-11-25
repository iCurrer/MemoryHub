"""Validate the model's structured output, without a schema library.

``docs/02-roadmap.md`` originally said Pydantic. That contradicts ADR-005's
"no third-party runtime dependency beyond what is already justified", and
pulling in Pydantic to check six fields would be the tail wagging the dog. The
checks below are the whole of what a schema library would buy here.

The contract is deliberately forgiving per item and strict per file:

- A malformed *item* is dropped with a reason, and the other items survive.
  One bad row should not throw away fifty good ones from the same call.
- A malformed *envelope* raises, because it means the prompt contract was
  violated and nothing in the response can be trusted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from ..models import CATEGORIES, CONFIDENCES

#: A single memory that exceeds this is almost certainly several facts glued
#: together, which is the exact failure this whole phase exists to fix.
MAX_BODY_CHARS = 1200

#: Quotes longer than this are usually the model echoing a whole paragraph to
#: make location easy, which defeats the purpose of a locator.
MAX_QUOTE_CHARS = 300

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


@dataclass(frozen=True)
class ProposedEntry:
    category: str
    body: str
    quote: str
    confidence: str = "unknown"


def extract_json(text: str) -> Any:
    """Parse the model's text into Python, tolerating a ```json fence.

    Models add fences even when told not to, and they add prose before the JSON.
    Rather than fight that in the prompt, take the outermost object or array.
    """
    cleaned = _FENCE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opening, closing in (("{", "}"), ("[", "]")):
        start = cleaned.find(opening)
        end = cleaned.rfind(closing)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"无法从模型输出中解析 JSON（前 200 字）：{text[:200]}")


def parse_proposals(payload: Any) -> Tuple[List[ProposedEntry], List[str]]:
    """Return ``(accepted, problems)``. ``problems`` explains every drop."""
    problems: List[str] = []
    if not isinstance(payload, dict):
        raise ValueError(f"顶层应为对象，实际是 {type(payload).__name__}")
    raw_items = payload.get("entries")
    if not isinstance(raw_items, list):
        raise ValueError("缺少 entries 数组")

    accepted: List[ProposedEntry] = []
    seen_bodies: Dict[str, int] = {}

    for index, raw in enumerate(raw_items):
        where = f"entries[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: 不是对象")
            continue

        category = str(raw.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            problems.append(f"{where}: 分类 {category!r} 不在 {CATEGORIES}")
            continue

        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            problems.append(f"{where}: body 为空")
            continue
        body = body.strip()
        if len(body) > MAX_BODY_CHARS:
            problems.append(
                f"{where}: body {len(body)} 字超过 {MAX_BODY_CHARS}，应拆成多条 —— 已丢弃"
            )
            continue

        quote = raw.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            # Not a nice-to-have: an entry with no locator cannot be checked
            # against the source, so it cannot be defended during review.
            problems.append(f"{where}: 缺少 quote（无法回指原文）")
            continue
        quote = quote.strip()
        if len(quote) > MAX_QUOTE_CHARS:
            problems.append(f"{where}: quote 过长（{len(quote)} 字），疑似整段回显")
            continue

        confidence = str(raw.get("confidence") or "unknown").strip().lower()
        if confidence not in CONFIDENCES:
            confidence = "unknown"

        key = " ".join(body.split())
        if key in seen_bodies:
            problems.append(f"{where}: 与 entries[{seen_bodies[key]}] 重复")
            continue
        seen_bodies[key] = index

        accepted.append(
            ProposedEntry(
                category=category, body=body, quote=quote, confidence=confidence
            )
        )

    return accepted, problems
