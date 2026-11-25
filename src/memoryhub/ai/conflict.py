"""Find memories that contradict each other, without deciding for the human.

The failure this module exists for is already on record: the vault held a
400-character entry saying "毕业论文（尚未冻结）：先确定题目" next to a 36-character
entry saying "毕业论文：《基于多 Agent 架构的企业财务共享中心工作流程优化研究》".
Both were ``approved``, neither superseded the other, and a real Claude session
read them and could not tell which one still holds. Two contradictory answers
from the same library is worse than a missing answer.

Three design decisions, each made against an alternative:

1. **The whole approved corpus goes in one prompt, not pairwise.** Pairwise
   scoring was measured on the real vault: the entry pair above ranks 31st of
   309 same-category pairs by character-bigram cosine (0.052). A similarity
   threshold high enough to be useful would have missed the one conflict that
   actually happened. Judging is semantic, so the comparison has to be too --
   and 50 approved entries are ~8.5k characters, well inside one call.

2. **The model may not write.** It proposes; ``memoryhub conflicts`` prints;
   ``memoryhub supersede`` writes, and only with two ids a human typed. There is
   no code path from a model response to a ``supersedes`` field.

3. **Every claim carries two verbatim quotes, one per side.** Same defence as
   ``organize.py``: a quote that cannot be located in the entry it cites means
   the model described something that is not there. An unlocatable proposal is
   downgraded to ``contradicts`` with no winner -- the pair is still worth a
   human look, but nothing in it may be presented as a verdict.

What this cannot do: judge whether a *new* fact is true. It only reports that
two stored facts disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import MemoryEntry, now_iso
from .organize import _fingerprint, quote_is_located
from .provider import AIError, Provider
from .schema import extract_json

#: Relations the model may report. ``unrelated`` is deliberately absent: it is
#: the default assumption, and asking the model to confirm it wastes output on
#: every non-conflict in the vault.
RELATIONS = ("supersedes", "contradicts", "duplicate")

#: Same guard as ``organize.BATCH_CHARS`` and for the same reason: an oversized
#: request fails with an opaque context-length error instead of a useful one.
BATCH_CHARS = 24000

#: Above this, one run stops being one cheap call. The refusal is explicit
#: rather than silent truncation -- dropping entries quietly would mean the
#: command reports "no conflicts" for a corpus it never looked at.
MAX_ENTRIES = 300

#: A response with more pairs than this is not a vault with 60 conflicts; it is
#: a model that started paraphrasing instead of comparing.
MAX_PAIRS = 50

MAX_REASON_CHARS = 400

SYSTEM_PROMPT = """\
你在比对一个人记忆库里已经批准的记忆条目，找出互相矛盾、或已被后者取代的条目。

判定关系只有三种：
- supersedes：两条讲同一件事，其中一条明确更新（时间更晚、结论更具体），旧的那条不再成立。winner 填仍然成立的那条 id。
- contradicts：两条直接矛盾，但没有证据说明哪条更新或更权威，无法判断该信谁。winner 留空。
- duplicate：同一件事被记了两遍，内容一致、不矛盾。winner 填内容更完整的那条。

以下情况**不要输出**：话题相近但互不矛盾；一条是另一条的补充细节；措辞不同但含义相同（那是 duplicate，不是 supersedes）；分类不同但各说各的。

证据规则（最重要）：
- 每条判定必须给出 evidence，键是 id，值是从该条目正文里**逐字复制**的片段（6-40 字）。
- 片段要能直接看出矛盾点。不许改写、不许拼接两条的内容、不许写概括。
- 定位不到的判定会被降级为 contradicts 且丢弃 winner —— 也就是说，编造证据等于这条判定作废。

输出：只输出一个 JSON 对象，不要 markdown 代码块，不要解释文字。
{"pairs": [{"a": "条目id", "b": "条目id", "relation": "supersedes", "winner": "条目id", "reason": "一句话说明为什么", "evidence": {"条目id": "逐字片段", "条目id": "逐字片段"}}]}

没有发现任何冲突时输出 {"pairs": []}。"""

USER_TEMPLATE = """\
以下是记忆库中全部已批准且未过期的条目（共 {count} 条）。找出其中互相矛盾或已被取代的对。

{entries}"""


@dataclass
class ConflictPair:
    """One detected relation, with the evidence that has to hold it up."""

    a: str
    b: str
    relation: str
    reason: str
    #: id -> quote copied from that entry's body.
    evidence: Dict[str, str] = field(default_factory=dict)
    #: Both quotes were located in their entries. False means the model's
    #: description could not be checked, so no verdict is attached.
    verified: bool = False
    winner: Optional[str] = None

    @property
    def key(self) -> Tuple[str, str]:
        return tuple(sorted((self.a, self.b)))  # type: ignore[return-value]

    def to_record(self, *, model: str, detected_at: str = "") -> Dict[str, object]:
        """The on-disk proposal. Bodies are not copied in -- they are derivable
        from the vault by id, and a stale copy would be a second truth."""
        first, second = self.key
        return {
            "version": 1,
            "pair": [first, second],
            "relation": self.relation,
            "winner": self.winner,
            "reason": self.reason,
            "evidence": {key: self.evidence[key] for key in (first, second) if key in self.evidence},
            "verified": self.verified,
            "detected_at": detected_at or now_iso(),
            "model": model,
        }


@dataclass
class ConflictResult:
    pairs: List[ConflictPair] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    calls: int = 0
    chars_sent: int = 0

    @property
    def unverified(self) -> int:
        return sum(1 for pair in self.pairs if not pair.verified)


def _render(entry: MemoryEntry) -> str:
    return (
        f"### {entry.id} | {entry.category} | {entry.created_at[:10]}\n"
        f"{entry.body.strip()}\n"
    )


def pack(entries: Sequence[MemoryEntry], limit: int = BATCH_CHARS) -> List[List[MemoryEntry]]:
    """Split the corpus into calls that stay under ``limit`` characters.

    Entries are never split: half an entry is exactly the fragment the model
    would then "complete" from imagination.
    """
    batches: List[List[MemoryEntry]] = []
    current: List[MemoryEntry] = []
    size = 0
    for entry in sorted(entries, key=lambda e: (e.created_at, e.id)):
        block = len(_render(entry))
        if current and size + block > limit:
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += block
    if current:
        batches.append(current)
    return batches


def detect(
    entries: Sequence[MemoryEntry],
    provider: Provider,
    *,
    max_entries: int = MAX_ENTRIES,
) -> ConflictResult:
    """Ask the model which approved entries disagree with which."""
    result = ConflictResult()
    if len(entries) < 2:
        return result
    if len(entries) > max_entries:
        raise ValueError(
            f"已批准条目 {len(entries)} 条，超过单次比对上限 {max_entries} 条。"
            "用 --category 收窄，或先归档过期内容。"
            "（静默截断会让命令报告「没有冲突」，而它根本没看全。）"
        )

    bodies = {entry.id: _fingerprint(entry.body) for entry in entries}

    for index, batch in enumerate(pack(entries), start=1):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    count=len(batch),
                    entries="\n".join(_render(entry) for entry in batch),
                ),
            },
        ]
        raw = provider.chat(messages)
        result.calls += 1
        result.chars_sent += sum(len(_render(entry)) for entry in batch)

        try:
            payload = extract_json(raw)
        except ValueError as exc:
            result.problems.append(f"第 {index} 批：{exc}")
            continue
        try:
            pairs, problems = parse_conflicts(payload, bodies)
        except ValueError as exc:
            result.problems.append(f"第 {index} 批：{exc}")
            continue
        result.problems.extend(f"第 {index} 批 {problem}" for problem in problems)

        seen = {pair.key for pair in result.pairs}
        for pair in pairs:
            if pair.key in seen:
                result.problems.append(
                    f"{pair.a} / {pair.b}: 同一对出现了两次，保留第一次判定"
                )
                continue
            seen.add(pair.key)
            if len(result.pairs) >= MAX_PAIRS:
                result.problems.append(
                    f"达到单次上限 {MAX_PAIRS} 对，其余已丢弃（库该人工梳理了）"
                )
                break
            result.pairs.append(pair)

    return result


def parse_conflicts(
    payload: object, bodies: Dict[str, str]
) -> Tuple[List[ConflictPair], List[str]]:
    """Validate the model's answer against the entries it was given.

    ``bodies`` maps id -> normalised body text, and doubles as the membership
    test: an id the model invented is not in it. Per-item failures are dropped
    with a reason so one bad pair does not discard a good one.
    """
    problems: List[str] = []
    if not isinstance(payload, dict):
        raise ValueError(f"顶层应为对象，实际是 {type(payload).__name__}")
    raw_items = payload.get("pairs")
    if not isinstance(raw_items, list):
        raise ValueError("缺少 pairs 数组")

    accepted: List[ConflictPair] = []
    for index, raw in enumerate(raw_items):
        where = f"pairs[{index}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: 不是对象")
            continue

        a = str(raw.get("a") or "").strip()
        b = str(raw.get("b") or "").strip()
        if a not in bodies or b not in bodies:
            problems.append(f"{where}: id 不在本次送出的条目里（{a!r} / {b!r}）")
            continue
        if a == b:
            problems.append(f"{where}: a 与 b 是同一条")
            continue

        relation = str(raw.get("relation") or "").strip().lower()
        if relation not in RELATIONS:
            problems.append(f"{where}: 关系 {relation!r} 不在 {RELATIONS}")
            continue

        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{where}: 缺少 reason")
            continue
        reason = " ".join(reason.split())
        if len(reason) > MAX_REASON_CHARS:
            problems.append(f"{where}: reason 过长（{len(reason)} 字），已截断")
            reason = reason[:MAX_REASON_CHARS]

        winner = raw.get("winner")
        winner = str(winner).strip() if isinstance(winner, str) else ""
        if relation in ("supersedes", "duplicate"):
            if winner not in (a, b):
                problems.append(
                    f"{where}: {relation} 的 winner 必须是 a 或 b 之一，"
                    f"实际 {winner!r} —— 已降级为 contradicts"
                )
                relation, winner = "contradicts", ""
        else:
            winner = ""  # contradicts never names a winner

        evidence, verified = _check_evidence(raw.get("evidence"), a, b, bodies)
        if not verified:
            problems.append(
                f"{where}: evidence 在原文里定位不到（{a} / {b}）—— 已降级为 contradicts"
            )
            relation, winner = "contradicts", ""

        accepted.append(
            ConflictPair(
                a=a,
                b=b,
                relation=relation,
                reason=reason,
                evidence=evidence,
                verified=verified,
                winner=winner or None,
            )
        )

    return accepted, problems


def _check_evidence(
    raw: object, a: str, b: str, bodies: Dict[str, str]
) -> Tuple[Dict[str, str], bool]:
    """Return ``(cleaned_evidence, both_quotes_located)``.

    Keys are normalised to the two ids, so an answer whose ``evidence`` uses
    "a"/"b" instead of ids is not silently trusted -- it simply fails to
    locate, which is the correct outcome for a response that ignored the
    contract.
    """
    if not isinstance(raw, dict):
        return {}, False

    cleaned: Dict[str, str] = {}
    located = True
    for entry_id in (a, b):
        quote = raw.get(entry_id)
        if not isinstance(quote, str) or not quote.strip():
            located = False
            continue
        quote = " ".join(quote.split())
        cleaned[entry_id] = quote
        if not quote_is_located(quote, bodies[entry_id]):
            located = False
    return cleaned, located


def findings_line(pair: ConflictPair) -> str:
    """One line for the listing: who, what, and whether it can be trusted."""
    mark = "" if pair.verified else "  [证据未核实]"
    arrow = f" → {pair.winner}" if pair.winner else ""
    return f"{RELATION_LABELS[pair.relation]:<10}{pair.a}  {pair.b}{arrow}{mark}"


RELATION_LABELS = {
    "supersedes": "取代",
    "contradicts": "矛盾",
    "duplicate": "重复",
}


def looks_mixed(entry: MemoryEntry) -> bool:
    """Heuristic used by the CLI to warn before archiving a long entry.

    The real case: a 400-character entry held the paper title *and* the working
    directory *and* a preferred tool list. Archiving it to retire one stale
    clause would also hide the other three from every AI that queries the
    vault -- a supersede that loses data is not a fix.
    """
    body = entry.body.strip()
    if len(body) > 200:
        return True
    return len(re.split(r"\n[-*]\s|\n### ", body)) > 3
