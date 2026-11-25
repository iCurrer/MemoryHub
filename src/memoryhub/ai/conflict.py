"""Ask the model whether two specific claims disagree -- nothing more.

The first version sent all 50 approved entries in one prompt and asked it to
report every conflicting pair. On the real vault it failed in three ways that are
worth keeping as the design rationale, because each one names a structural cause:

1. **It missed the only real conflict.** The 400-character "个人工作台" entry says
   「毕业论文（尚未冻结）：先确定题目」 and a 36-character entry gives the settled
   title. Both were in the prompt. The model did not report them. An A/B run
   showed why: with only those two entries present it reports the pair but picks
   the *wrong* direction (treating the later ``created_at`` as newer); adding six
   unrelated entries makes the pair vanish entirely. **Eight entries of context
   were enough to erase it.** A prompt asking for independence cannot fix that --
   the fix is that no unrelated entry is ever in the prompt.

2. **Granularity.** Whole entries were compared to whole entries, so a summary
   entry that matches six single-fact entries was reported as six duplicates, each
   advising to archive the summary. ``claims.py`` splits first; this module then
   compares one claim to one claim.

3. **Time semantics.** ``created_at`` is when the memory was *recorded*, not when
   the fact happened. Any "newer" inference from it is wrong for a late-recorded
   old fact, which is exactly the paper-title case. So the date is not sent at
   all, and the prompt names the only admissible evidence of precedence: wording
   inside the claims themselves (「尚未」 vs 「已定」).

What stays from the first version: the model may not write. It returns a
judgement about two claims; ``memoryhub supersede`` is still the only path that
touches ``supersedes``/``valid_until``, and still requires two ids a human typed.

What this cannot do: judge whether a fact is true, or read the whole vault. It
sees one pair at a time and knows nothing about the other 39,998 pairs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import MemoryEntry, now_iso
from .claims import (
    DEFAULT_MAX_PAIRS,
    PER_ENTRY_PAIR,
    Candidate,
    Claim,
    PairingStats,
    candidates,
    collect,
)
from .organize import _fingerprint, quote_is_located
from .provider import AIError, Provider
from .schema import extract_json

#: Relations the model may report. ``none`` is a first-class answer: without an
#: explicit way to say "these are unrelated", a prompt that hands over two
#: fragments pressures the model into inventing a relationship between them.
RELATIONS = ("supersede", "contradicts", "duplicate", "none")

#: Spelling variants seen in real answers. Dropping a correct judgement because
#: the model wrote ``contradict`` instead of ``contradicts`` is a false negative
#: caused by this module, not by the model -- and it happened on the exact pair
#: this feature exists for.
RELATION_ALIASES = {
    "supersedes": "supersede",
    "superseded": "supersede",
    "replace": "supersede",
    "contradict": "contradicts",
    "conflict": "contradicts",
    "duplicates": "duplicate",
    "same": "duplicate",
    "unrelated": "none",
    "no_conflict": "none",
}

#: Labels for the CLI, keyed by the relation names above. ``supersedes`` is the
#: plural the version-1 proposals on disk still carry; the listing must render
#: those too rather than printing a raw field value at the reader.
RELATION_LABELS = {
    "supersede": "取代",
    "supersedes": "取代",
    "contradicts": "矛盾",
    "duplicate": "重复",
    "none": "无关",
}

#: When one entry pair yields several claim verdicts, the strongest wins. Order
#: matters: retiring a memory is a bigger claim than "these two say the same".
SEVERITY = {"supersede": 3, "contradicts": 2, "duplicate": 1, "none": 0}

MAX_REASON_CHARS = 400

#: Three consecutive transport failures is not a flaky pair, it is a broken
#: endpoint. Continuing would burn one doomed request per candidate.
FAILURE_BUDGET = 3


def unactionable(relation: str, scope: str) -> Optional[str]:
    """Why this proposal must not reach a reader, or ``None`` if it should.

    A ``duplicate`` that only overlaps in *some* claims is not actionable,
    because the one write this project has is ``supersede`` -- archive the whole
    entry. A 6-line 技术偏好 entry and a 6-line 身份 entry that share the single
    sentence 「喜欢通过实际项目学习」 were reported as duplicates of each other;
    acting on that would retire five unrelated facts to remove one repeated
    sentence. The overlap is real. The proposed remedy just does not exist.

    So the rule is about the *remedy*, not about the model: no proposal is
    emitted when the only way to apply it loses data. Redundancy is cheap;
    a wrong merge is not. The same predicate is used to prune proposals written
    before this rule existed (``memoryhub conflicts --prune``), because a rule
    that only applies to future output leaves the existing queue just as
    unanswerable.
    """
    if relation == "duplicate" and scope != "whole":
        return "只有一段重叠，而唯一的写法是整条归档——会连带隐藏别的内容"
    return None


JUDGE_SYSTEM = """\
你在核对同一个人的记忆库里，两段内容是否互相矛盾。

给你两段片段，标为 A 和 B，每段旁边附上它所属记忆的全文作为背景。
**只判断 A 与 B 这两段本身**；背景只用来理解指代（"它"、"这个项目"指什么），
不要拿 A 去和 B 所在记忆里的其他内容比较，也不要评价这两条记忆整体。

四选一：
- contradict：A 与 B 对同一件事给出不相容的说法，且从措辞看不出哪条更新。
- supersede：A 与 B 说的是同一件事，其中一段明确取代另一段。
- duplicate：A 与 B 说的是同一件事，措辞不同但不矛盾。
- none：两段各说各的；或一段是另一段的补充细节；或只是话题相近。
  **大部分对都应该是 none，选它不算失败。不要为了给出结论而硬凑关系。**

判断"是不是同一件事"时，这两条最容易判错：
- **状态与结论是同一件事。** 一段说某事"尚未确定/待定/计划中/先做 X"，另一段给出了这件事的
  具体结论或结果，那是同一件事的两种状态，属于 contradict 或 supersede。
  不要把它解释成"描述层面不同""一个是流程一个是结论"而判 none —— 这正是最常见的漏报。
  例：`毕业论文（尚未冻结）：先确定题目` 与 `毕业论文：《基于多 Agent 架构的…研究》`
  讲的是同一件事，一个说题目没定，一个给出了题目，**它们矛盾**。
- **不同方面才是不同的事。** 两段分别讲同一项目的不同侧面（一段说目标、一段说目录；
  一段说研究方法和一段说参考文献格式），那是不同的事，判 none。

判断新旧时最容易出错的一点：
- 记忆里的日期是**录入时间**，不是这件事发生的时间。晚录入的可能是一段很早的旧回忆。
  **绝对不要用日期判断谁更新。**
- 只有片段本身的措辞能证明先后时才判 supersede：例如"已确定/已定/已放弃/已取消/改为/最终"
  对比"尚未/待定/计划/打算/先确定"。
- 判不了就说 contradict 并且 newer 留空。说出"两条冲突但不知道该信谁"比猜错方向有用得多。

引用规则（最重要）：
- quote_a 必须是 A 中**逐字复制**的一段（6-40 字），不要改写、不要拼接。
- quote_b 同理，必须来自 B。
- 判 supersede 时，direction 填能看出先后关系的那几个字，且必须逐字来自你选定为较新的那一段。
- 任何一条引用在原文里定位不到，这条判定就作废。编造引用等于这条判定没发生。

只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字：
{"relation": "none", "newer": "", "quote_a": "", "quote_b": "", "direction": "", "reason": ""}

字段说明：newer 只取 "a" 或 "b"（仅 supersede 时填，其余留空）；
reason 一句话说明为什么，判 none 时也可以留空。"""

JUDGE_USER = """\
条目 A（分类 {a_category}）：
{a_body}

其中要判断的片段 A：
〈{a_claim}〉

条目 B（分类 {b_category}）：
{b_body}

其中要判断的片段 B：
〈{b_claim}〉"""


@dataclass
class ClaimVerdict:
    """One model answer about one claim pair, validated against the text."""

    left: Claim
    right: Claim
    relation: str
    reason: str
    #: claim.key -> verbatim quote from that claim.
    quotes: Dict[str, str] = field(default_factory=dict)
    #: Both quotes were located inside their claims. False means the answer
    #: described something that is not there, so no verdict may be attached.
    verified: bool = False
    #: Entry id the model says is still current. None unless verified.
    winner_entry: Optional[str] = None


@dataclass
class ConflictPair:
    """A proposal about two *entries*, assembled from one or more claim verdicts."""

    a: str
    b: str
    relation: str
    reason: str
    evidence: Dict[str, str] = field(default_factory=dict)
    verified: bool = False
    winner: Optional[str] = None
    #: ``whole`` when every claim on the affected side is involved, ``partial``
    #: when only some are. A partial conflict must not be archived wholesale --
    #: that is the summary-entry failure this module exists for.
    scope: str = "whole"
    #: Claim-level detail, so the reader sees which sentence contradicts which.
    claims: List[Dict[str, object]] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return tuple(sorted((self.a, self.b)))  # type: ignore[return-value]

    def to_record(self, *, model: str, detected_at: str = "") -> Dict[str, object]:
        """The on-disk proposal. Bodies are not copied in -- they are derivable
        from the vault by id, and a stale copy would be a second truth."""
        first, second = self.key
        return {
            "version": 2,
            "pair": [first, second],
            "relation": self.relation,
            "scope": self.scope,
            "winner": self.winner,
            "reason": self.reason,
            "evidence": {key: self.evidence[key] for key in (first, second) if key in self.evidence},
            "claims": self.claims,
            "verified": self.verified,
            "detected_at": detected_at or now_iso(),
            "model": model,
        }


@dataclass
class ConflictResult:
    pairs: List[ConflictPair] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    verdicts: List[ClaimVerdict] = field(default_factory=list)
    stats: PairingStats = field(default_factory=PairingStats)
    calls: int = 0
    chars_sent: int = 0
    #: Pairs the model reported and this module refused to turn into a
    #: proposal, with the reason. Reported, never silent: "the model found 12
    #: things and you are being shown 3" is information the reader needs.
    suppressed: List[str] = field(default_factory=list)

    @property
    def unverified(self) -> int:
        return sum(1 for pair in self.pairs if not pair.verified)

    @property
    def considered(self) -> int:
        """Claim pairs the model actually looked at and said nothing about."""
        return max(0, self.stats.selected - len(self.verdicts))


def _render_judge(candidate: Candidate, entries: Dict[str, MemoryEntry]) -> str:
    left_entry = entries[candidate.left.entry_id]
    right_entry = entries[candidate.right.entry_id]
    return JUDGE_USER.format(
        a_category=left_entry.category,
        a_body=left_entry.body.strip(),
        a_claim=candidate.left.text.strip(),
        b_category=right_entry.category,
        b_body=right_entry.body.strip(),
        b_claim=candidate.right.text.strip(),
    )


def _match_quotes(
    payload: object, candidate: Candidate
) -> Tuple[Dict[str, str], bool]:
    """Map the two quotes onto the two claims.

    Returns ``(quotes_by_claim_key, swapped)``; an empty dict means at least one
    quote could not be located in *either* claim.

    A swapped pair is accepted because which side is "a" depends on the internal
    ordering of the two entries, and getting that label backwards says nothing
    about whether the quotes are real. What is *not* tolerated is a quote that
    exists in neither claim: that is the failure this check exists for.
    """
    if not isinstance(payload, dict):
        return {}, False
    for swapped in (False, True):
        fields = ("quote_b", "quote_a") if swapped else ("quote_a", "quote_b")
        quotes: Dict[str, str] = {}
        located = True
        for claim, field_name in zip((candidate.left, candidate.right), fields):
            raw = payload.get(field_name)
            if not isinstance(raw, str) or not raw.strip():
                located = False
                break
            quote = " ".join(raw.split())
            quotes[claim.key] = quote
            if not quote_is_located(quote, _fingerprint(claim.text)):
                located = False
                break
        if located:
            return quotes, swapped
    return {}, False


def parse_verdict(
    payload: object, candidate: Candidate
) -> Tuple[Optional[ClaimVerdict], List[str]]:
    """Validate one answer against the two claims it was given.

    The quote test is the only automatic defence against a fluid answer, and it
    is applied at claim granularity on purpose: a quote that only exists
    *elsewhere in the entry* means the model compared something other than the
    two fragments it was asked about, which is precisely the failure mode of the
    previous version.
    """
    problems: List[str] = []
    if not isinstance(payload, dict):
        raise ValueError(f"顶层应为对象，实际是 {type(payload).__name__}")

    relation = str(payload.get("relation") or "").strip().lower()
    relation = RELATION_ALIASES.get(relation, relation)
    if relation not in RELATIONS:
        raise ValueError(f"relation {relation!r} 不在 {RELATIONS}")

    reason = payload.get("reason")
    reason = " ".join(reason.split()) if isinstance(reason, str) else ""
    if len(reason) > MAX_REASON_CHARS:
        problems.append(f"reason 过长（{len(reason)} 字），已截断")
        reason = reason[:MAX_REASON_CHARS]

    if relation == "none":
        # "These two are unrelated" asserts an absence. Nothing downstream acts
        # on it, so demanding evidence would only train the model to invent a
        # relationship in order to have something to quote.
        return (
            ClaimVerdict(
                left=candidate.left,
                right=candidate.right,
                relation="none",
                reason=reason,
            ),
            problems,
        )

    quotes, swapped = _match_quotes(payload, candidate)
    if swapped:
        problems.append("quote_a/quote_b 的 A/B 标反了，已按内容自动对调")
    if not quotes:
        problems.append(
            "引用在对应片段里定位不到 —— 这条判定降级为「矛盾」，不标方向"
        )
        return (
            ClaimVerdict(
                left=candidate.left,
                right=candidate.right,
                relation="contradicts",
                reason=reason,
            ),
            problems,
        )

    winner: Optional[str] = None
    if relation == "supersede":
        newer = str(payload.get("newer") or "").strip().lower()
        if swapped:
            # The quotes revealed that the answer labelled the sides the other way
            # round, so "newer: b" means the claim this module calls ``left``.
            # Flipping only the quotes would leave the direction pointing at the
            # wrong entry -- the exact error (a wrong winner) that made the first
            # version dangerous.
            newer = {"a": "b", "b": "a"}.get(newer, newer)
        direction = payload.get("direction")
        direction = " ".join(direction.split()) if isinstance(direction, str) else ""
        if newer not in ("a", "b") or not direction:
            problems.append(
                "判了取代但没说清哪条更新（newer/direction 缺失）—— 降级为「矛盾」"
            )
            relation = "contradicts"
        else:
            side = candidate.left if newer == "a" else candidate.right
            if not quote_is_located(direction, _fingerprint(side.text)):
                problems.append(
                    "direction 引用不在被判为较新的那段里 —— 降级为「矛盾」"
                )
                relation = "contradicts"
            else:
                winner = side.entry_id

    return (
        ClaimVerdict(
            left=candidate.left,
            right=candidate.right,
            relation=relation,
            reason=reason,
            quotes=quotes,
            verified=True,
            winner_entry=winner,
        ),
        problems,
    )


def judge(
    candidate: Candidate,
    entries: Dict[str, MemoryEntry],
    provider: Provider,
) -> Tuple[Optional[ClaimVerdict], List[str]]:
    """One pair, one call. No other claim is in the request."""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": _render_judge(candidate, entries)},
    ]
    raw = provider.chat(messages)
    payload = extract_json(raw)
    return parse_verdict(payload, candidate)


def aggregate(
    verdicts: Sequence[ClaimVerdict],
    claim_counts: Dict[str, int],
) -> List[ConflictPair]:
    """Fold claim verdicts into one proposal per entry pair.

    ``scope`` is the field that keeps the summary-entry failure from recurring:
    when only some claims on a side conflict, the proposal is explicitly partial,
    and the CLI says so next to the ``supersede`` command it prints.
    """
    grouped: Dict[Tuple[str, str], List[ClaimVerdict]] = {}
    for verdict in verdicts:
        if verdict.relation == "none":
            continue
        group = tuple(sorted((verdict.left.entry_id, verdict.right.entry_id)))
        grouped.setdefault(group, []).append(verdict)

    pairs: List[ConflictPair] = []
    for (a, b), items in grouped.items():
        items.sort(
            key=lambda item: (-SEVERITY[item.relation], item.left.key, item.right.key)
        )
        strongest = items[0]
        relation = strongest.relation if strongest.verified else "contradicts"
        winner = strongest.winner_entry if strongest.verified else None
        if winner is not None and relation not in ("supersede", "duplicate"):
            winner = None

        evidence: Dict[str, str] = {}
        for verdict in items:
            for claim_key, quote in verdict.quotes.items():
                entry_id = claim_key.split("#", 1)[0]
                if entry_id in (a, b) and entry_id not in evidence:
                    evidence[entry_id] = quote

        involved: Dict[str, set] = {a: set(), b: set()}
        for verdict in items:
            involved[verdict.left.entry_id].add(verdict.left.index)
            involved[verdict.right.entry_id].add(verdict.right.index)
        partial = any(
            len(involved[entry_id]) < claim_counts.get(entry_id, 0)
            for entry_id in (a, b)
        )

        pairs.append(
            ConflictPair(
                a=a,
                b=b,
                relation=relation,
                reason=strongest.reason
                or (items[1].reason if len(items) > 1 else ""),
                evidence=evidence,
                verified=all(item.verified for item in items),
                winner=winner,
                scope="partial" if partial else "whole",
                claims=[
                    {
                        "left": item.left.key,
                        "right": item.right.key,
                        "relation": item.relation,
                        "verified": item.verified,
                        "quote_left": item.quotes.get(item.left.key, ""),
                        "quote_right": item.quotes.get(item.right.key, ""),
                        "reason": item.reason,
                    }
                    for item in items
                ],
            )
        )

    pairs.sort(key=lambda pair: (-SEVERITY[pair.relation], pair.a, pair.b))
    return pairs


def detect(
    entries: Sequence[MemoryEntry],
    provider: Provider,
    *,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    per_entry_pair: int = PER_ENTRY_PAIR,
    skip_pairs: Sequence[Tuple[str, str]] = (),
    on_progress=None,
    on_planned=None,
) -> ConflictResult:
    """Pair claims, then ask the model about each pair separately.

    ``skip_pairs`` are entry pairs a human already dismissed as unrelated. They
    are skipped before the call -- re-asking a question that was answered costs
    money and teaches the reader to ignore the queue.

    ``on_planned(stats, count)`` runs after pairing and before the first call --
    the CLI uses it to print what the run costs and to ask for confirmation, so a
    person can decline *before* the money is spent and before any text leaves the
    machine. Returning ``False`` cancels.

    ``on_progress(done, total, verdict)`` is called after every pair; the CLI
    uses it to keep a long run visible instead of a silent minute.
    """
    result = ConflictResult()
    if len(entries) < 2:
        return result

    claims = collect(entries)
    chosen, stats = candidates(claims, limit=max_pairs, per_entry_pair=per_entry_pair)
    if skip_pairs:
        skip = {tuple(sorted(pair)) for pair in skip_pairs}
        chosen = [
            candidate
            for candidate in chosen
            if tuple(sorted((candidate.left.entry_id, candidate.right.entry_id)))
            not in skip
        ]
    result.stats = stats
    if not chosen:
        return result

    if on_planned is not None and on_planned(stats, len(chosen)) is False:
        result.problems.append("已取消：配对已完成，但没有发送任何内容")
        return result

    by_id = {entry.id: entry for entry in entries}
    claim_counts: Dict[str, int] = {}
    for claim in claims:
        claim_counts[claim.entry_id] = claim_counts.get(claim.entry_id, 0) + 1

    failures = 0
    for index, candidate in enumerate(chosen, start=1):
        try:
            verdict, problems = judge(candidate, by_id, provider)
        except AIError as exc:
            failures += 1
            result.problems.append(
                f"{candidate.left.key} / {candidate.right.key}: {exc}"
            )
            if failures >= FAILURE_BUDGET:
                result.problems.append(
                    f"连续 {failures} 次调用失败，已中止 —— 先修好端点再重跑，"
                    f"剩余 {len(chosen) - index} 段对没有送审"
                )
                break
            continue
        except ValueError as exc:
            result.problems.append(
                f"{candidate.left.key} / {candidate.right.key}: {exc}"
            )
            continue

        failures = 0
        result.calls += 1
        result.chars_sent += len(_render_judge(candidate, by_id))
        result.problems.extend(
            f"{candidate.left.key} / {candidate.right.key}: {problem}"
            for problem in problems
        )
        if verdict is not None:
            result.verdicts.append(verdict)
        if on_progress is not None:
            on_progress(index, len(chosen), verdict)

    result.pairs = aggregate(result.verdicts, claim_counts)
    kept: List[ConflictPair] = []
    for pair in result.pairs:
        why = unactionable(pair.relation, pair.scope)
        if why is not None:
            result.suppressed.append(f"{pair.a} / {pair.b}: {why}")
            continue
        kept.append(pair)
    result.pairs = kept
    return result


def findings_line(pair: ConflictPair) -> str:
    """One line for the listing: what, how deep, and whether it can be trusted."""
    mark = "" if pair.verified else "  [证据未核实]"
    scope = "  仅部分片段" if pair.scope == "partial" else ""
    arrow = f" → {pair.winner}" if pair.winner else ""
    return (
        f"{RELATION_LABELS.get(pair.relation, pair.relation):<6}"
        f"{pair.a}  {pair.b}{arrow}{scope}{mark}"
    )


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
