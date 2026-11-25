"""Rule-based category inference.

Why rules and not a model: the import path must work with no API key, no
network, and no cost, and it must give the same answer twice. A classifier that
cannot be explained also cannot be reviewed — and review is the whole point
(ADR-003). Phase 3 adds an LLM layer on top; this stays underneath it as the
floor.

Scoring, not first-match. The first version returned whichever rule appeared
first in the table, and it filed "## 当前重点项目：MemoryHub — 仓库 github.com/…"
as a *decision* because the paragraph happened to contain "不得" once. Weighted
evidence fixes that: three weak signals for the right category now outvote one
weak signal for the wrong one, and the reason string shows the arithmetic.

The contract is deliberately narrow:

- :func:`classify` returns ``(category, reason)``. The reason lists the signals
  that fired and their weights, so `memoryhub recategorize` prints "why".
- No signal returns the caller's default with reason ``"no rule matched"``.
  Guessing silently would be worse than inheriting the import's judgement.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: Signals per category, with weight. 3 = decisive on its own, 2 = strong,
#: 1 = weak (a word that often appears in another category's prose too).
SIGNALS: Tuple[Tuple[str, Tuple[Tuple[str, int], ...]], ...] = (
    (
        "decision",
        (
            ("绝不", 3),
            ("禁止", 3),
            ("铁律", 3),
            ("红线", 3),
            ("硬约束", 3),
            ("不可推翻", 3),
            ("必须", 2),
            ("务必", 2),
            ("一律", 2),
            ("只能", 2),
            ("只写", 2),
            ("只做", 2),
            ("只认", 2),
            ("不写", 2),
            ("不做", 2),
            ("为准", 2),
            ("规则", 1),
            ("约定", 1),
            ("不得", 1),
            ("不准", 1),
            ("不许", 1),
            ("不要", 1),
            ("不能", 1),
            ("不再", 1),
        ),
    ),
    (
        "profile",
        (
            ("称呼", 2),
            ("姓名", 2),
            ("生日", 2),
            ("籍贯", 2),
            ("老家", 2),
            ("学校", 2),
            ("学院", 2),
            ("大学", 2),
            ("专业", 2),
            ("年级", 2),
            ("住址", 2),
            ("身份", 2),
            ("名字", 1),
            ("年龄", 1),
            ("出生", 1),
            ("城市", 1),
            ("届", 1),
            ("自我介绍", 1),
        ),
    ),
    (
        "preference",
        (
            ("偏好", 2),
            ("倾向", 2),
            ("习惯", 2),
            ("更喜欢", 2),
            ("更愿意", 2),
            ("不喜欢", 2),
            ("优先", 1),
            ("喜欢", 1),
            ("讨厌", 1),
            ("尽量", 1),
            ("推荐用", 1),
        ),
    ),
    (
        "project",
        (
            ("github.com", 3),
            ("gitlab.com", 3),
            ("仓库", 2),
            ("项目", 2),
            ("里程碑", 2),
            ("开发计划", 2),
            ("进度", 1),
            ("待办", 1),
            ("repo", 1),
        ),
    ),
)

#: Tie-break order when two categories score the same. ``decision`` first: a
#: sentence that reads like a rule is safer filed as a rule.
PRIORITY = ("decision", "profile", "preference", "project")

#: Weight at which a single signal settles the category on its own.
DECISIVE_WEIGHT = 3

#: Bodies shorter than this are usually a single noun phrase ("称呼：主人"),
#: and a short string can match a keyword by accident.
MIN_BODY_FOR_RULES = 4


def score(body: str) -> Dict[str, List[Tuple[str, int]]]:
    """Per-category list of ``(signal, weight)`` hits. Public for tests."""
    lowered = body.lower()
    hits: Dict[str, List[Tuple[str, int]]] = {}
    for category, signals in SIGNALS:
        found = [(word, weight) for word, weight in signals if word in lowered]
        if found:
            hits[category] = found
    return hits


def classify(body: str, default: str = "profile") -> Tuple[str, str]:
    """Return ``(category, reason)`` for a memory body.

    >>> classify("真实姓名绝不外泄")[0]
    'decision'
    >>> classify("称呼：主人")[0]
    'profile'
    >>> classify("## 当前重点项目 - 仓库 github.com/x/y")[0]
    'project'
    """
    text = body.strip()
    # Keep the heading *text* but drop the ``#`` markers: "## 当前重点项目" is a
    # strong project signal, and throwing it away made real entries unclassifiable.
    prose = re.sub(r"(?m)^#{1,6}\s*", "", text).strip()
    if len(prose) < MIN_BODY_FOR_RULES:
        return default, "body too short for rules"

    hits = score(prose)
    if not hits:
        return default, "no rule matched"

    # A weight-3 signal is decisive: a body containing ``github.com`` is a
    # project, however many "必须"/"不得" it also contains. Without this, long
    # mixed paragraphs tie and the tie-break alone decided the answer.
    decisive = {
        category: [hit for hit in found if hit[1] >= DECISIVE_WEIGHT]
        for category, found in hits.items()
    }
    decisive = {category: found for category, found in decisive.items() if found}
    pool = decisive or hits

    totals = {category: sum(weight for _word, weight in found) for category, found in pool.items()}
    best = max(totals.values())
    winners = [category for category, total in totals.items() if total == best]
    if len(winners) > 1:
        winners.sort(key=lambda name: PRIORITY.index(name) if name in PRIORITY else len(PRIORITY))
    chosen = winners[0]

    detail = " + ".join(f"{word}({weight})" for word, weight in pool[chosen])
    reason = f"{detail} = {best}"
    if decisive:
        reason += " [decisive]"
    outranked = [
        f"{category}={total}"
        for category, total in sorted(totals.items(), key=lambda item: -item[1])
        if category != chosen
    ]
    if outranked:
        reason += f" (outranked {' '.join(outranked)})"
    return chosen, reason


def explain(body: str, default: str = "profile") -> str:
    """One-line justification, used by the CLI table."""
    _category, reason = classify(body, default)
    return reason
