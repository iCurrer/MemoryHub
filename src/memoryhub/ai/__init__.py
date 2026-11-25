"""AI-assisted organisation (phase 3, P3-01/P3-02/P3-03).

Why this package exists at all: the rule-based splitter and classifier measured
54.7% on the real vault, 7.7% whenever no keyword fired (``docs/04-classify-audit.md``). Splitting a memory into
one-fact-per-entry and naming that fact are semantic jobs; regex cannot do them.

Where the boundary sits (this is the design, not an implementation detail):

- **Finding** which files are sources stays algorithmic. ``Adapter.discover()``
  is deterministic, cheap, testable, and involves no semantics.
- **Reading** a source becomes semantic. ``Adapter.read()``'s rule-based
  splitting is bypassed in AI mode; the raw text goes to the model instead.
- **Writing** stays algorithmic and human-gated. The model returns proposals,
  the validator accepts or rejects each one, and nothing reaches ``vault/``
  without approval (phase 3 hard constraint, ``docs/02-roadmap.md``).

The anti-hallucination rule is the one thing that cannot be dropped: every
proposal must carry a ``quote`` copied from the source, and a proposal whose
quote cannot be located in the source is discarded. Without it the model can
invent a plausible memory and the next AI will read it as fact.
"""

from __future__ import annotations

from .organize import organize, OrganizeResult
from .provider import AIConfig, AIError, Provider
from .schema import ProposedEntry, parse_proposals

__all__ = [
    "AIConfig",
    "AIError",
    "OrganizeResult",
    "ProposedEntry",
    "Provider",
    "organize",
    "parse_proposals",
]
