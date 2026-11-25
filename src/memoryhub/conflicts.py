"""Conflict proposals on disk, and the one write that resolves them (P3-05).

Two halves, deliberately separated:

- ``ai/conflict.py`` asks the model what disagrees with what. Its output is a
  *proposal* and nothing else.
- This module reads and writes those proposals, and performs the single
  transition that changes the vault: ``supersede(old, by=new)`` -- which only
  ever runs from two ids a human typed.

The record format is JSON, not Markdown, and it sits in ``inbox/conflicts/``.
That directory is scanned by the store (``VaultStore._readable_dirs``), which
globs ``*.md``: a ``.json`` there is invisible to ``list``/``search``/``doctor``
by construction. Writing the proposal as Markdown would make every conflict
record look like a broken memory entry and turn ``doctor`` red for a non-problem.

Resolved proposals move to ``logs/conflicts/`` with the decision appended. The
proposal is not a memory, but *why* a memory was retired is exactly the kind of
thing this project keeps (ADR-006: 旧结论不删除，关闭有效期并建立取代链).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import MemoryEntry, ValidationError, now_iso
from .storage.markdown_store import VaultStore, atomic_write_text

CONFLICT_DIR = ("inbox", "conflicts")
RESOLVED_DIR = ("logs", "conflicts")

_ID_RE = re.compile(r"^mem_[0-9A-Za-z_]+$")


def record_path(root: Path, a: str, b: str) -> Path:
    """Stable path for a pair, independent of the order the model reported it."""
    if not _ID_RE.match(a) or not _ID_RE.match(b):
        raise ValidationError(f"refusing to build a path from ids: {a!r} / {b!r}")
    first, second = sorted((a, b))
    return Path(root).joinpath(*CONFLICT_DIR) / f"{first}__{second}.json"


def save_record(root: Path, record: Dict[str, object]) -> Path:
    """Write one proposal. Re-running detection overwrites its own proposal.

    Overwrite is correct here because a proposal has no author state: it is a
    function of (two entries, model, prompt). Anything a human decided is in the
    vault's supersede chain, not in this file.
    """
    pair = record.get("pair") or []
    if not isinstance(pair, list) or len(pair) != 2:
        raise ValidationError("record.pair 必须是两个 id")
    path = record_path(root, str(pair[0]), str(pair[1]))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return path


def load_records(root: Path) -> Tuple[List[Dict[str, object]], List[str]]:
    """Read every proposal; an unparseable file is reported, never skipped quietly."""
    records: List[Dict[str, object]] = []
    problems: List[str] = []
    directory = Path(root).joinpath(*CONFLICT_DIR)
    if not directory.is_dir():
        return records, problems
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if not isinstance(data, dict):
            problems.append(f"{path.name}: 顶层不是对象")
            continue
        pair = data.get("pair")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(item, str) for item in pair)
        ):
            problems.append(f"{path.name}: pair 必须是两个 id")
            continue
        records.append(data)
    records.sort(key=lambda item: str(item.get("detected_at") or ""), reverse=True)
    return records, problems


def find_record(root: Path, a: str, b: str) -> Optional[Path]:
    try:
        path = record_path(root, a, b)
    except ValidationError:
        return None
    return path if path.is_file() else None


def resolve_record(
    root: Path,
    a: str,
    b: str,
    *,
    keep: str,
    retired: str,
    note: str = "",
) -> Optional[Path]:
    """Move an applied proposal to ``logs/`` with the decision recorded.

    Returns ``None`` when there was no proposal on disk (a human can supersede
    without ever running detection -- that path must not depend on a file).
    """
    path = find_record(root, a, b)
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    record["resolution"] = {
        "keep": keep,
        "superseded": retired,
        "note": note,
        "resolved_at": now_iso(),
    }

    target = Path(root).joinpath(*RESOLVED_DIR) / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        target, json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    path.unlink(missing_ok=True)
    return target


# ------------------------------------------------------------------ the write


def apply_supersede(
    store: VaultStore, old_id: str, new_id: str, *, note: str = ""
) -> Tuple[MemoryEntry, MemoryEntry]:
    """Retire ``old_id`` in favour of ``new_id`` (ADR-006).

    Returns ``(retired_old, updated_new)``.

    Guards, each of which exists because the naive version loses something:

    - Both entries must exist. A typo must not create a dangling reference --
      ``supersedes`` was unchecked until now (HANDOFF 已知问题 #6).
    - ``new`` must be approved and not expired. Pointing at a pending entry
      would archive the old one while the replacement stays invisible to every
      MCP client: the fact would disappear from the library.
    - ``old`` must be approved and must not already be superseded. Silently
      re-pointing a chain would rewrite history that a previous decision set.
    - No cycles. ``a supersedes b`` and ``b supersedes a`` is a library that can
      no longer be traversed.

    Order matters: ``supersedes`` is written first, ``valid_until`` second. If
    the second write fails the new entry merely lists a predecessor it has not
    retired yet -- recoverable. The reverse order would archive an entry with
    nothing pointing to its replacement.
    """
    if not _ID_RE.match(old_id or "") or not _ID_RE.match(new_id or ""):
        raise ValidationError(f"invalid id: {old_id!r} / {new_id!r}")
    if old_id == new_id:
        raise ValidationError("一条记忆不能取代它自己")
    old = store.get(old_id)
    new = store.get(new_id)
    if old is None:
        raise KeyError(f"no such memory: {old_id}")
    if new is None:
        raise KeyError(f"no such memory: {new_id}")

    if old.superseded_by:
        # Checked before the status guard: an entry retired by a previous
        # supersede is archived, and "已被 X 取代" is the true diagnosis --
        # "只有 approved 才能进链" would send the reader looking for the wrong
        # problem.
        raise ValidationError(
            f"{old_id} 已被 {old.superseded_by} 取代。改判需要先手动清掉那个字段"
        )
    if old.status != "approved":
        raise ValidationError(
            f"{old_id} 当前状态是 {old.status}，只有 approved 的条目才进取代链"
        )
    if new.status != "approved":
        raise ValidationError(
            f"{new_id} 当前状态是 {new.status}，不能作为取代方 —— "
            "否则旧条目归档了，而新条目对其他 AI 仍不可见"
        )
    if new.is_expired():
        raise ValidationError(f"{new_id} 的 valid_until 已过期，不能再取代别的条目")

    walker: Optional[MemoryEntry] = new
    seen = set()
    while walker is not None:
        if walker.id == old_id:
            raise ValidationError(
                f"{new_id} 已经（直接或间接）被 {old_id} 取代，再写会成环"
            )
        if walker.id in seen:
            break
        seen.add(walker.id)
        walker = store.get(walker.superseded_by) if walker.superseded_by else None

    if old_id not in new.supersedes:
        new.supersedes.append(old_id)
    if note:
        new.extra["supersede_note"] = note
    new.updated_at = now_iso()
    store.save(new)

    retired = store.stamp_validity(old_id, superseded_by=new_id, valid_until=now_iso())
    return retired, new
