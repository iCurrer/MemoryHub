"""Domain model for a single memory entry."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import frontmatter

CATEGORIES = ("profile", "preference", "project", "knowledge", "decision")
STATUSES = ("pending", "approved", "rejected", "archived")
TRUST_LEVELS = ("auto", "review", "blocked")
SENSITIVITIES = ("normal", "personal", "sensitive", "secret")
#: ``memory`` and ``transcript`` are the phase 1 values, kept so existing vaults
#: keep loading. ADR-011 adds the finer kinds; new code must use those.
SOURCE_KINDS = (
    "instruction",
    "auto_memory",
    "session_distill",
    "profile",
    "doc",
    "memory",
    "transcript",
)
CONFIDENCES = ("high", "medium", "low", "unknown")
#: Where a source sits in its tool's instruction chain (ADR-011).
#: Empty means "this source has no layering" (a single memory file, a doc).
LAYERS = ("global", "project", "local", "nested")

#: ``mem_<yyyymmdd>T<hhmmss>_<hex>``. The suffix used to be 4 hex chars, which
#: collided under bulk import (32 duplicates per 2000 ids); it is now 8. The
#: pattern still accepts 4 so that existing vaults keep loading.
_ID_RE = re.compile(r"^mem_\d{8}T\d{6}_[0-9a-f]{4,32}$")

#: Keys that are managed by the model; anything else is preserved verbatim.
_KNOWN_KEYS = (
    "id",
    "category",
    "status",
    "trust",
    "sensitivity",
    "confidence",
    "source_adapter",
    "source_path",
    "source_kind",
    "source_hash",
    "precedence",
    "layer",
    "created_at",
    "updated_at",
    "valid_from",
    "valid_until",
    "supersedes",
    "superseded_by",
    "tags",
)

#: Fields a markdown file cannot be constructed without. Everything else has a
#: safe default, so a truncated file must fail as a ValidationError here rather
#: than as a TypeError from ``__init__`` half-way through a vault scan.
_REQUIRED_KEYS = ("id", "category")


class ValidationError(ValueError):
    """Raised when a memory entry violates the schema."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class MemoryEntry:
    id: str
    category: str
    body: str
    status: str = "pending"
    trust: str = "review"
    sensitivity: str = "normal"
    confidence: str = "unknown"
    source_adapter: str = "unknown"
    source_path: str = ""
    source_kind: str = "memory"
    source_hash: str = ""
    precedence: int = 0
    layer: str = ""
    created_at: str = ""
    updated_at: str = ""
    valid_from: str = ""
    valid_until: Optional[str] = None
    supersedes: List[str] = field(default_factory=list)
    superseded_by: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ ids

    @staticmethod
    def new_id(moment: Optional[datetime] = None) -> str:
        moment = moment or datetime.now().astimezone()
        return f"mem_{moment.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # --------------------------------------------------------------- policy

    def infer_trust(self) -> str:
        """Apply the three-tier trust rule (ADR-003).

        Sensitivity wins over everything; then categories that shape behaviour
        require a human decision; then low confidence does.

        ``project`` is not mentioned by ADR-003, so it keeps the historical
        ``auto`` behaviour. That gap is recorded as an open question in
        ``docs/01-decisions.md`` ADR-009 and must be closed by the owner, not
        by guessing here.
        """
        if self.sensitivity in ("sensitive", "secret"):
            return "blocked"
        if self.category in ("profile", "decision"):
            return "review"
        if self.confidence == "low":
            return "review"
        return "auto"

    # ----------------------------------------------------------- validation

    def validate(self) -> None:
        if not isinstance(self.id, str) or not _ID_RE.match(self.id):
            raise ValidationError(f"invalid id format: {self.id!r}")
        if self.category not in CATEGORIES:
            raise ValidationError(
                f"invalid category {self.category!r}; expected one of {CATEGORIES}"
            )
        if self.status not in STATUSES:
            raise ValidationError(
                f"invalid status {self.status!r}; expected one of {STATUSES}"
            )
        if self.trust not in TRUST_LEVELS:
            raise ValidationError(
                f"invalid trust {self.trust!r}; expected one of {TRUST_LEVELS}"
            )
        if self.sensitivity not in SENSITIVITIES:
            raise ValidationError(
                f"invalid sensitivity {self.sensitivity!r}; expected one of {SENSITIVITIES}"
            )
        if self.source_kind not in SOURCE_KINDS:
            raise ValidationError(
                f"invalid source_kind {self.source_kind!r}; expected one of {SOURCE_KINDS}"
            )
        if self.confidence not in CONFIDENCES:
            raise ValidationError(
                f"invalid confidence {self.confidence!r}; expected one of {CONFIDENCES}"
            )
        if not isinstance(self.precedence, int) or isinstance(self.precedence, bool):
            raise ValidationError(
                f"precedence must be an int, got {type(self.precedence).__name__}"
            )
        if self.precedence < 0:
            raise ValidationError("precedence must not be negative")
        if self.layer not in LAYERS and self.layer != "":
            raise ValidationError(
                f"invalid layer {self.layer!r}; expected one of {LAYERS} or empty"
            )
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValidationError("body must not be empty")
        for name in ("tags", "supersedes"):
            value = getattr(self, name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValidationError(
                    f"{name} must be a list of strings, got {type(value).__name__}"
                )
        if self.trust == "blocked" and self.status == "approved":
            raise ValidationError("a blocked entry cannot be approved")
        for name in ("created_at", "updated_at", "valid_from"):
            value = getattr(self, name)
            if not value:
                raise ValidationError(f"{name} is required (ISO 8601 with timezone)")
            if not isinstance(value, str):
                raise ValidationError(
                    f"{name} must be a string, got {type(value).__name__}"
                )
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(
                    f"{name} is not a valid ISO 8601 timestamp: {value!r}"
                ) from exc
        if self.superseded_by is not None:
            if not isinstance(self.superseded_by, str) or not _ID_RE.match(
                self.superseded_by
            ):
                raise ValidationError(f"invalid superseded_by: {self.superseded_by!r}")
        for ref in self.supersedes:
            if not _ID_RE.match(ref):
                raise ValidationError(f"invalid supersedes reference: {ref!r}")

    # ------------------------------------------------------------- validity

    def is_expired(self, at: Optional[str] = None) -> bool:
        """True when ``valid_until`` has passed (ADR-006).

        Queries default to hiding expired entries; history views pass
        ``include_expired``/``--include-expired`` explicitly.
        """
        if not self.valid_until:
            return False
        try:
            until = datetime.fromisoformat(self.valid_until)
            moment = (
                datetime.fromisoformat(at) if at else datetime.now().astimezone()
            )
        except ValueError:
            return False
        if until.tzinfo is None:
            until = until.astimezone()
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return until <= moment

    # ------------------------------------------------------------- ser/de

    def to_meta(self) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "status": self.status,
            "trust": self.trust,
            "sensitivity": self.sensitivity,
            "confidence": self.confidence,
            "source_adapter": self.source_adapter,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "source_hash": self.source_hash,
            "precedence": self.precedence,
            "layer": self.layer,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "tags": list(self.tags),
        }
        for key, value in self.extra.items():
            if key not in _KNOWN_KEYS:
                meta[key] = value
        return meta

    def to_markdown(self) -> str:
        self.validate()
        return frontmatter.join(self.to_meta(), self.body)

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryEntry":
        meta, body = frontmatter.split(text)
        missing = [key for key in _REQUIRED_KEYS if key not in meta]
        if missing:
            raise ValidationError(
                "missing required field(s): " + ", ".join(missing)
            )
        kwargs: Dict[str, Any] = {k: v for k, v in meta.items() if k in _KNOWN_KEYS}
        if kwargs.get("supersedes") is None:
            kwargs["supersedes"] = []
        if kwargs.get("tags") is None:
            kwargs["tags"] = []
        entry = cls(body=body, **kwargs)
        entry.extra = {k: v for k, v in meta.items() if k not in _KNOWN_KEYS}
        entry.validate()
        return entry

    # -------------------------------------------------------------- factory

    @classmethod
    def create(
        cls,
        *,
        category: str,
        body: str,
        tags: Optional[List[str]] = None,
        source_adapter: str = "manual",
        source_path: str = "",
        source_kind: str = "memory",
        source_hash: str = "",
        sensitivity: str = "normal",
        confidence: str = "unknown",
        supersedes: Optional[List[str]] = None,
        created_at: Optional[str] = None,
        precedence: int = 0,
        layer: str = "",
    ) -> "MemoryEntry":
        """Build a pending entry.

        ``created_at`` lets an adapter carry the source's own timestamp instead
        of "now", which matters for imported memories: their age is evidence.
        """
        stamp = now_iso()
        moment = created_at or stamp
        entry = cls(
            id=cls.new_id(),
            category=category,
            body=body,
            status="pending",
            sensitivity=sensitivity,
            confidence=confidence,
            source_adapter=source_adapter,
            source_path=source_path,
            source_kind=source_kind,
            source_hash=source_hash,
            precedence=precedence,
            layer=layer,
            created_at=moment,
            updated_at=stamp,
            valid_from=moment,
            supersedes=list(supersedes or []),
            tags=list(tags or []),
        )
        entry.trust = entry.infer_trust()
        entry.validate()
        return entry
