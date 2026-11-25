"""Who read what (P5-07).

The vault knows where every memory *came from* and it does not know who has
*looked at* it. That asymmetry is why "is this memory still trusted?" is
impossible to answer a month later: an entry three clients read yesterday and
an entry nobody has ever read are indistinguishable on disk.

So: an append-only JSONL log, one line per MCP read call.

Three things are deliberately **not** recorded:

- **The query text.** A search string is often more revealing than the answer
  ("我的期望薪资"). This log answers *who read this memory*, not *what did they
  ask*.
- **``clientInfo`` from MCP.** It is self-reported and unauthenticated, so it
  would be security theatre (ADR-014). What is recorded instead is the
  ``--label`` the user typed into that client's own config, or
  ``"unlabeled"`` -- the command line is the only boundary worth trusting.
- **Identical repeats within one call.** One line describes one call, with the
  ids it returned; the tally is computed at read time, not written here.

The log is a hint, not an audit trail. It is truncated at
:data:`ROTATE_AT` bytes (one previous generation kept), every write is
best-effort, and a failure to log must never fail a memory read -- the read is
the product, the log is an observation about it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import now_iso

#: ``logs/access.jsonl`` under the vault root.
ACCESS_FILE = "logs/access.jsonl"

#: Rotate to ``access.jsonl.1`` past this size. One generation is kept: the
#: question this file answers ("who has been reading this?") is about the recent
#: past, and an unbounded log inside a git-backed vault is a liability.
ROTATE_AT = 1024 * 1024

#: A single call can return a page of results; cap the ids per line so one wide
#: ``memory_list`` cannot grow the log by tens of kilobytes.
MAX_IDS_PER_LINE = 50


def path_for(root: Path) -> Path:
    return Path(root) / ACCESS_FILE


def record(
    root: Path,
    tool: str,
    entry_ids: Iterable[str] = (),
    permission: str = "",
    label: str = "",
) -> bool:
    """Append one line describing a read call. Never raises.

    Returns whether the line was written, so a test can assert the failure mode
    rather than guessing at it.
    """
    ids = [str(item) for item in entry_ids][:MAX_IDS_PER_LINE]
    line = {
        "at": now_iso(),
        "tool": tool,
        "label": label_from(label) or "unlabeled",
        "permission": permission,
        "count": len(ids),
        "ids": ids,
    }
    target = path_for(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return True
    except OSError:
        # Read-only vault, full disk, another process holding the file: none of
        # these are reasons to fail the read that just succeeded.
        return False


def _rotate_if_needed(target: Path, rotate_at: Optional[int] = None) -> None:
    # Read the module constant at call time, not at definition time: a test that
    # wants a small log should be able to shrink ROTATE_AT and see rotation.
    limit = ROTATE_AT if rotate_at is None else rotate_at
    try:
        if target.stat().st_size < limit:
            return
    except FileNotFoundError:
        return
    previous = target.with_suffix(target.suffix + ".1")
    try:
        os.replace(target, previous)
    except OSError:
        # Keep appending to the big file rather than losing the new line.
        pass


def lines(root: Path) -> List[Dict[str, Any]]:
    """Every readable line, oldest first. Garbage lines are skipped."""
    out: List[Dict[str, Any]] = []
    for path in (path_for(root).with_suffix(".jsonl.1"), path_for(root)):
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict) and "tool" in item:
                out.append(item)
    return out


def recent(root: Path, limit: int = 50) -> List[Dict[str, Any]]:
    items = lines(root)
    return items[-limit:][::-1] if limit > 0 else []


def tally(root: Path) -> Dict[str, Dict[str, Any]]:
    """``entry_id -> {reads, tools, labels, last_at}`` across both generations."""
    out: Dict[str, Dict[str, Any]] = {}
    for item in lines(root):
        tool = str(item.get("tool", ""))
        label = str(item.get("label", ""))
        at = str(item.get("at", ""))
        for entry_id in item.get("ids", []) or []:
            entry_id = str(entry_id)
            slot = out.setdefault(
                entry_id,
                {"reads": 0, "tools": [], "labels": [], "last_at": ""},
            )
            slot["reads"] += 1
            if tool and tool not in slot["tools"]:
                slot["tools"].append(tool)
            if label and label not in slot["labels"]:
                slot["labels"].append(label)
            if at > slot["last_at"]:
                slot["last_at"] = at
    return out


def for_entry(root: Path, entry_id: str) -> Dict[str, Any]:
    slot = tally(root).get(entry_id)
    if slot is None:
        return {"entry": entry_id, "reads": 0, "tools": [], "labels": [], "last_at": ""}
    return {"entry": entry_id, **slot}


def summary(root: Path) -> Dict[str, Any]:
    """Call count, distinct labels and the newest timestamp -- cheap to embed."""
    items = lines(root)
    labels: List[str] = []
    for item in items:
        label = str(item.get("label", ""))
        if label and label not in labels:
            labels.append(label)
    return {
        "calls": len(items),
        "labels": labels,
        "last_at": str(items[-1].get("at", "")) if items else "",
        "read_entries": len(tally(root)),
    }


def label_from(label: Optional[str]) -> str:
    """Normalise a user-supplied label; drop anything that is not one token.

    Lower-cased so ``Claude Code`` and ``claude-code`` do not become two
    identities in the tally.
    """
    text = (label or "").strip()
    if not text:
        return ""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in text)
    return cleaned.strip("-").lower()[:32]
