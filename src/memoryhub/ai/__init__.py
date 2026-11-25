"""AI-assisted organisation (phase 3, P3-01/P3-02/P3-03/P3-05).

Why this package exists at all: the rule-based splitter and classifier measured
54.7% on the real vault, 7.7% whenever no keyword fired (``docs/04-classify-audit.md``). Splitting a memory into
one-fact-per-entry and naming that fact are semantic jobs; regex cannot do them.

Where the boundary sits (this is the design, not an implementation detail):

- **Finding** which files are sources stays algorithmic. ``Adapter.discover()``
  is deterministic, cheap, testable, and involves no semantics.
- **Pairing** which claims might disagree stays algorithmic too
  (``claims.py``): sorting 40,000 pairs by shared vocabulary decides nothing.
- **Reading** a source becomes semantic. ``Adapter.read()``'s rule-based
  splitting is bypassed in AI mode; the raw text goes to the model instead.
- **Judging** whether two claims disagree is entirely semantic, and entirely the
  model's: one pair per call, so no unrelated entry can dilute the comparison.
- **Writing** stays algorithmic and human-gated. The model returns proposals,
  the validator accepts or rejects each one, and nothing reaches ``vault/``
  without approval (phase 3 hard constraint, ``docs/02-roadmap.md``).

The anti-hallucination rule is the one thing that cannot be dropped: every
proposal must carry a ``quote`` copied from the source, and a proposal whose
quote cannot be located in the source is discarded. Without it the model can
invent a plausible memory and the next AI will read it as fact.
"""

from __future__ import annotations

from .claims import (
    DEFAULT_MAX_PAIRS,
    PER_ENTRY_PAIR,
    Candidate,
    Claim,
    PairingStats,
    candidates,
    collect,
    split_claims,
)
from .conflict import (
    RELATIONS,
    ClaimVerdict,
    ConflictPair,
    ConflictResult,
    aggregate,
    detect,
    judge,
    looks_mixed,
    parse_verdict,
)
from .organize import organize, OrganizeResult
from .provider import AIConfig, AIError, Provider
from .schema import ProposedEntry, parse_proposals

__all__ = [
    "AIConfig",
    "AIError",
    "Candidate",
    "Claim",
    "ClaimVerdict",
    "ConflictPair",
    "ConflictResult",
    "DEFAULT_MAX_PAIRS",
    "OrganizeResult",
    "PER_ENTRY_PAIR",
    "PairingStats",
    "ProposedEntry",
    "Provider",
    "RELATIONS",
    "aggregate",
    "candidates",
    "collect",
    "detect",
    "judge",
    "looks_mixed",
    "organize",
    "parse_proposals",
    "parse_verdict",
    "split_claims",
]
