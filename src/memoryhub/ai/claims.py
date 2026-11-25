"""Split memories into claims, then pair up claims that might disagree.

Why the unit of judgement is a *claim* and not a whole entry
------------------------------------------------------------

The first version of P3-05 compared entries to entries. On the real vault that
produced a failure with a clear shape: one 400-character entry ("个人工作台")
bundles a project list, a preference list, an external-communication policy, a
reminder anchor and a recommendation blacklist. Six different single-fact
entries each matched *one sentence* of it, so the model reported the same pair
six times as "duplicate" and advised superseding the whole entry -- which would
have hidden OMaster, SoundMesh, photography and the preference list from every
query at once.

That is a granularity error, not a model error. A 400-character entry is not one
fact, so comparing it as one fact can only produce the wrong answer. Splitting
first makes the comparison unit match the thing being compared.

Why pairing is algorithmic here, when judging is not
---------------------------------------------------

The split between "find candidates" and "read them" is the same one the import
pipeline already uses (ADR-016): locating files is mechanical, deciding what they
mean is not. Sorting 40,000 claim pairs is mechanical. Deciding whether two
claims contradict each other is not, so that part is left entirely to the model
-- one pair per call, so no third claim can dilute the comparison.

The cost of getting pairing wrong is asymmetric and must stay visible: a pair
that never reaches the model cannot be reported, and the caller has no way to
know. So :func:`candidates` returns :class:`PairingStats` alongside the list, and
the CLI prints every number in it. "N pairs examined, M not sent" is a limit a
person can act on. "No conflicts found" is not.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from ..models import MemoryEntry
from .organize import _fingerprint

#: Shorter than this and the "claim" is a heading or a bullet stub ("目录："). A
#: fragment invites the model to complete it from imagination.
MIN_CLAIM_CHARS = 6

#: How many claim pairs one entry pair may contribute. Without a cap, one
#: eight-claim summary entry floods the candidate list and squeezes out every
#: other pair -- measured: 5 of the top 8 candidates involved the same entry.
PER_ENTRY_PAIR = 2

#: One call per pair. Above this the run stops being a coffee break, so the
#: caller picks the ceiling deliberately instead of the library picking it.
DEFAULT_MAX_PAIRS = 120

#: Claims that open with the same three characters are treated as being about
#: the same subject. This is not a similarity heuristic -- it is the only channel
#: that found the conflict the whole feature exists for. Measured on the real
#: vault: 「毕业论文（尚未冻结）：先确定题目」 against 「毕业论文：《基于多 Agent
#: 架构的企业财务共享中心工作流程优化研究》」 shares only 毕业/业论/论文 -- the
#: three most common bigrams in the corpus -- so IDF Dice ranks it 207th of 4,803
#: pairs, behind dozens of harmless near-duplicates. The subject is identical and
#: stated first, which is how these memories are written; similarity scoring
#: cannot see it, an anchor can.
ANCHOR_CHARS = 3

#: A subject bucket this large means the anchor is a generic opener ("用户",
#: "不喜欢"), not a subject. Only the best pairs in such a bucket are kept.
MAX_ANCHOR_BUCKET = 30

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
#: Sentence-ish boundaries that do not cross a comma. A comma is usually an
#: aside inside one claim; a stop/semicolon/arrow ends it.
_SPLIT = re.compile(r"[。；;|→]|\s{2,}")


@dataclass(frozen=True)
class Claim:
    """One assertion, small enough to be compared and quoted as a unit."""

    entry_id: str
    index: int
    text: str

    @property
    def key(self) -> str:
        return f"{self.entry_id}#{self.index}"


@dataclass
class Candidate:
    """Two claims worth asking the model about."""

    left: Claim
    right: Claim
    score: float


@dataclass
class PairingStats:
    """Every number the run has to be able to explain afterwards."""

    claims: int = 0
    #: Claim pairs with at least one shared bigram -- the pool that was ranked.
    scored: int = 0
    #: Claim pairs with no shared bigram at all: never considered.
    unshared: int = 0
    entry_pairs: int = 0
    #: Pairs kept back by :data:`PER_ENTRY_PAIR` so one entry cannot flood.
    crowded_out: int = 0
    #: Pairs dropped by the ``limit`` cut.
    over_limit: int = 0
    #: Pairs selected because both claims open with the same subject.
    anchored: int = 0
    selected: int = 0
    min_score: float = 0.0

    def lines(self) -> List[str]:
        return [
            f"切出 {self.claims} 段可比较的内容",
            f"其中 {self.scored} 段对有共同用词，涉及 {self.entry_pairs} 个条目对",
            f"未送审：{self.unshared} 段对毫无共同用词；"
            f"{self.crowded_out} 段对因同一条目对过多被让位；{self.over_limit} 段对超出本次上限",
            f"本次送审 {self.selected} 段对（其中 {self.anchored} 段因同一主题被保底纳入，"
            f"最低相似度 {self.min_score:.3f}）",
        ]


def split_claims(body: str) -> List[str]:
    """Split one entry's body into comparable assertions.

    Falls back to the whole body when splitting yields nothing usable -- a
    three-word entry is still a claim, and dropping it would silently remove it
    from every comparison.
    """
    pieces: List[str] = []
    for line in body.splitlines():
        stripped = _BULLET.sub("", line).strip().lstrip("#").strip()
        if not stripped:
            continue
        for piece in _SPLIT.split(stripped):
            piece = piece.strip(" -*\t")
            if len(_fingerprint(piece)) >= MIN_CLAIM_CHARS:
                pieces.append(piece)
    if not pieces:
        whole = body.strip()
        if len(_fingerprint(whole)) >= MIN_CLAIM_CHARS:
            pieces = [whole]
    return pieces


def collect(entries: Sequence[MemoryEntry]) -> List[Claim]:
    """Every entry's claims, in a stable order."""
    claims: List[Claim] = []
    for entry in sorted(entries, key=lambda e: (e.created_at, e.id)):
        for index, text in enumerate(split_claims(entry.body)):
            claims.append(Claim(entry_id=entry.id, index=index, text=text))
    return claims


def _bigrams(text: str) -> frozenset:
    fingerprint = _fingerprint(text)
    return frozenset(fingerprint[i : i + 2] for i in range(len(fingerprint) - 1))


def candidates(
    claims: Sequence[Claim],
    *,
    limit: int = DEFAULT_MAX_PAIRS,
    per_entry_pair: int = PER_ENTRY_PAIR,
) -> Tuple[List[Candidate], PairingStats]:
    """Rank claim pairs by IDF-weighted Dice, keep the top ``limit``.

    Dice rather than cosine: the failure case is a 15-character claim nested in a
    400-character one, and cosine divides by both lengths, so the short claim's
    own evidence gets diluted by everything around it. Dice divides by the sum,
    which favours "the short one is almost entirely about the long one" --
    exactly the shape of a contradiction between a summary and a fact.

    A bigram present in more than half the claims is skipped while accumulating:
    it carries almost no information and would generate the bulk of the pairs.
    """
    stats = PairingStats(claims=len(claims))
    if len(claims) < 2:
        return [], stats

    grams = [_bigrams(claim.text) for claim in claims]
    document_frequency: Counter = Counter()
    for bag in grams:
        for gram in bag:
            document_frequency[gram] += 1

    total = len(claims)

    def idf(gram: str) -> float:
        return math.log((total + 1) / (document_frequency[gram] + 1)) + 1.0

    weight = [sum(idf(gram) for gram in bag) for bag in grams]

    postings: Dict[str, List[int]] = defaultdict(list)
    for index, bag in enumerate(grams):
        for gram in bag:
            postings[gram].append(index)

    shared: Dict[Tuple[int, int], float] = defaultdict(float)
    # Skipping very common bigrams is pure economy: they generate most of the
    # pairs and carry almost no information. In a small corpus that economy
    # backfires -- every shared bigram is "too common" and nothing gets paired --
    # so the cut only applies once there is something to save.
    ceiling = total if total < 16 else max(2, total // 2)
    for gram, members in postings.items():
        if len(members) < 2 or len(members) > ceiling:
            continue
        value = idf(gram)
        for offset, first in enumerate(members):
            for second in members[offset + 1 :]:
                if claims[first].entry_id == claims[second].entry_id:
                    continue
                key = (first, second) if first < second else (second, first)
                shared[key] += value

    stats.scored = len(shared)
    same_entry = 0
    counts: Counter = Counter(claim.entry_id for claim in claims)
    for count in counts.values():
        same_entry += count * (count - 1) // 2
    stats.unshared = total * (total - 1) // 2 - same_entry - stats.scored

    scores: Dict[Tuple[int, int], Candidate] = {}
    for (first, second), numerator in shared.items():
        denominator = weight[first] + weight[second]
        if denominator <= 0:
            continue
        scores[(first, second)] = Candidate(
            left=claims[first],
            right=claims[second],
            score=2 * numerator / denominator,
        )

    grouped: Dict[Tuple[str, str], List[Candidate]] = defaultdict(list)
    for candidate in scores.values():
        group = tuple(sorted((candidate.left.entry_id, candidate.right.entry_id)))
        grouped[group].append(candidate)

    stats.entry_pairs = len(grouped)
    pool: List[Candidate] = []
    for items in grouped.values():
        items.sort(key=lambda item: (-item.score, item.left.key, item.right.key))
        pool.extend(items[:per_entry_pair])
        stats.crowded_out += max(0, len(items) - per_entry_pair)

    # Subject channel. Deliberately exempt from ``per_entry_pair``: the whole
    # point is that every claim about one subject gets compared, and it is
    # exactly the entry pair with many claims -- the summary entry -- where that
    # matters most.
    buckets: Dict[str, List[int]] = defaultdict(list)
    for index, claim in enumerate(claims):
        fingerprint = _fingerprint(claim.text)
        if len(fingerprint) >= ANCHOR_CHARS:
            buckets[fingerprint[:ANCHOR_CHARS]].append(index)

    anchored: List[Candidate] = []
    seen: set = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        bucket: List[Candidate] = []
        for offset, first in enumerate(members):
            for second in members[offset + 1 :]:
                if claims[first].entry_id == claims[second].entry_id:
                    continue
                candidate = scores.get((first, second))
                if candidate is not None:
                    bucket.append(candidate)
        bucket.sort(key=lambda item: (-item.score, item.left.key, item.right.key))
        keep = MAX_ANCHOR_BUCKET if len(members) <= MAX_ANCHOR_BUCKET else 8
        for candidate in bucket[:keep]:
            key = frozenset((candidate.left.key, candidate.right.key))
            if key in seen:
                continue
            seen.add(key)
            anchored.append(candidate)

    anchored.sort(key=lambda item: (-item.score, item.left.key, item.right.key))
    pool.sort(key=lambda item: (-item.score, item.left.key, item.right.key))
    rest = [
        candidate
        for candidate in pool
        if frozenset((candidate.left.key, candidate.right.key)) not in seen
    ]

    if len(anchored) >= limit:
        chosen = anchored[:limit]
        stats.over_limit = len(anchored) - limit + len(rest)
    else:
        room = limit - len(anchored)
        chosen = anchored + rest[:room]
        stats.over_limit = max(0, len(rest) - room)
    stats.anchored = sum(
        1 for candidate in chosen
        if frozenset((candidate.left.key, candidate.right.key)) in seen
    )
    stats.selected = len(chosen)
    stats.min_score = min((candidate.score for candidate in chosen), default=0.0)
    return chosen, stats
