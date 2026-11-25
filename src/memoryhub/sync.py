"""Push the vault back into the tools that read it (P5-06 / ADR-019).

The loop the whole project exists for is: three AIs write memories -> one vault
-> every AI reads the same truth. The first two arrows worked from phase 1; the
last one did not, and without it "one shared memory" is just a second place to
look.

Three constraints decide the shape of this file:

1. **Never rewrite the owner's file.** Each target gets exactly one marked
   region (``managed.py``). The block is a projection: delete it and the file is
   byte-identical to before. Anything else -- reformatting, merging, "tidying" --
   is a change the user did not ask for and cannot review.
2. **Reader and writer share the markers.** Import strips the block first.
   Without that, each sync becomes input for the next import and the vault fills
   with copies of itself.
3. **Default is a plan, not a write.** ``memoryhub sync`` prints what would
   change; ``--write`` commits, and takes a timestamped backup first.

What this deliberately does not do: choose *which* entries a given tool should
see. Every target gets the same approved set. Per-tool filtering sounds helpful
and is how two tools end up disagreeing again.
"""

from __future__ import annotations

import difflib
import glob as _glob
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import managed
from .models import MemoryEntry, now_iso

#: Reading order in the rendered block. Profile first: a model that reads only
#: the top of the block should still know who it is talking to.
CATEGORY_ORDER = ("profile", "decision", "preference", "project", "fact")

CATEGORY_LABELS = {
    "profile": "身份与档案",
    "decision": "决定与铁律",
    "preference": "偏好",
    "project": "项目",
    "fact": "事实",
}

#: Long entries are not truncated -- dropping the tail of a memory is worse than
#: a longer block -- but the total is reported so a runaway vault is visible.
WARN_BLOCK_CHARS = 40000


@dataclass(frozen=True)
class Target:
    """One file that gets a managed block."""

    key: str
    label: str
    #: ``~`` is expanded; a trailing glob picks the newest match.
    pattern: str
    #: Creating a missing file is a bigger claim than editing one, so it is
    #: declared per target instead of happening implicitly.
    creates: bool = False
    note: str = ""

    def resolve(self) -> Tuple[Optional[Path], List[Path]]:
        """Return ``(chosen, all_matches)``. Empty matches means "not installed"."""
        expanded = os.path.expanduser(self.pattern)
        if any(char in self.pattern for char in "*?["):
            # ``Path.glob`` only expands the final component, and WorkBuddy's
            # path needs ``user-*/MEMORY.md``: the wildcard is a directory.
            matches = [Path(name) for name in _glob.glob(expanded) if Path(name).is_file()]
            matches.sort(key=lambda path: path.stat().st_mtime)
            return (matches[-1] if matches else None), matches
        path = Path(expanded)
        return (path if path.is_file() else None), ([path] if path.is_file() else [])


def default_targets() -> List[Target]:
    """The three tools this vault was actually built from.

    Paths mirror ``adapters``: the same environment overrides, so a machine that
    reads from a custom location also writes there. A write path that disagrees
    with the read path is how a sync silently goes nowhere.
    """
    claude_home = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    wb_home = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))

    return [
        Target(
            key="claude",
            label="Claude Code 全局记忆",
            pattern=str(claude_home / "CLAUDE.md"),
            note="每次会话都会加载",
        ),
        Target(
            key="workbuddy",
            label="WorkBuddy 用户记忆",
            pattern=str(wb_home / "user-*" / "MEMORY.md"),
            note="会话开始时注入",
        ),
        Target(
            key="codex",
            label="Codex 全局指令",
            pattern=str(codex_home / "AGENTS.md"),
            creates=True,
            note="原文件不存在时新建",
        ),
    ]


# --------------------------------------------------------------------- render


def _render_entry(body: str) -> List[str]:
    """Render one entry as either a bullet or a bold-titled paragraph.

    Not a list item in both cases: entries imported from a ``CLAUDE.md`` section
    already *contain* bullet lists, and wrapping them would nest two levels for
    no gain and break the markdown. A one-line fact is a bullet; anything longer
    keeps its own structure under a bold first line.
    """
    lines = [line.rstrip() for line in body.strip().splitlines() if line.strip()]
    if not lines:
        return []
    first = lines[0]
    if len(lines) == 1 and not first.lstrip().startswith("#"):
        return [f"- {first.strip()}"]
    title = first.lstrip("#").strip()
    rest = lines[1:]
    out = [f"**{title}**", ""] if title else []
    out += rest
    return out


def _rank(entry: MemoryEntry) -> Tuple[int, str, str]:
    try:
        group = CATEGORY_ORDER.index(entry.category)
    except ValueError:
        group = len(CATEGORY_ORDER)
    return group, entry.created_at, entry.id


def select(entries: Sequence[MemoryEntry]) -> List[MemoryEntry]:
    """The entries that count as current truth, in a stable order."""
    live = [
        entry
        for entry in entries
        if entry.status == "approved" and not entry.is_expired() and not entry.superseded_by
    ]
    return sorted(live, key=_rank)


def render_block(entries: Sequence[MemoryEntry], *, synced_at: str = "") -> str:
    """The marked region. Regenerating it from the same vault gives the same text.

    Ordering is applied here rather than trusted from the caller: a block that
    reshuffles when the underlying directory listing changes produces a diff on
    every sync, and a diff nobody asked for trains people to ignore diffs.
    """
    stamp = synced_at or now_iso()
    ordered = sorted(entries, key=_rank)
    lines = [
        f"{managed.BEGIN} 由 `memoryhub sync` 生成，请勿手改；导入时会跳过本区块 -->",
        "## 长期记忆（MemoryHub 同步）",
        "",
        f"> {len(ordered)} 条当前有效的记忆 · 同步于 {stamp} · "
        '检索全文：`memoryhub search "<关键词>"` 或 MCP `memory_search`',
    ]

    grouped: Dict[str, List[MemoryEntry]] = {}
    for entry in ordered:
        grouped.setdefault(entry.category, []).append(entry)

    for category in list(CATEGORY_ORDER) + sorted(set(grouped) - set(CATEGORY_ORDER)):
        bucket = grouped.get(category)
        if not bucket:
            continue
        lines += ["", f"### {CATEGORY_LABELS.get(category, category)}", ""]
        for entry in bucket:
            rendered = _render_entry(entry.body)
            if rendered:
                lines += rendered + [""]
    lines += ["", managed.END]
    return "\n".join(lines)


# ----------------------------------------------------------------------- plan


@dataclass
class Plan:
    target: Target
    path: Optional[Path]
    exists: bool
    before: str = ""
    after: str = ""
    block: str = ""
    note: str = ""
    error: str = ""

    @property
    def changes(self) -> bool:
        return bool(self.after) and self.after != self.before

    @property
    def diff(self) -> str:
        before = self.before.splitlines(keepends=True) or []
        after = self.after.splitlines(keepends=True) or []
        return "".join(
            difflib.unified_diff(
                before, after, fromfile=str(self.path or self.target.pattern),
                tofile=str(self.path or self.target.pattern), n=2,
            )
        )


def plan(
    entries: Sequence[MemoryEntry],
    targets: Sequence[Target],
    *,
    synced_at: str = "",
) -> List[Plan]:
    """Compute every target's future content without touching disk."""
    block = render_block(select(entries), synced_at=synced_at)
    plans: List[Plan] = []
    for target in targets:
        chosen, matches = target.resolve()
        note = target.note
        if len(matches) > 1:
            note = f"{len(matches)} 个候选，用最新的 {chosen.name if chosen else '?'}"
        if chosen is None:
            if not target.creates:
                plans.append(
                    Plan(target=target, path=None, exists=False,
                         note="未安装或路径不存在，跳过", error="")
                )
                continue
            path = Path(os.path.expanduser(target.pattern))
            plans.append(
                Plan(target=target, path=path, exists=False, before="",
                     after=managed.replace_block("", block), block=block, note=note)
            )
            continue
        before = chosen.read_text(encoding="utf-8", errors="replace")
        after = managed.replace_block(before, block)
        plans.append(
            Plan(target=target, path=chosen, exists=True, before=before,
                 after=after, block=block, note=note)
        )
    return plans


# ---------------------------------------------------------------------- write


def backup(path: Path) -> Path:
    """Copy next to the original, named so a human can find it later."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-memoryhub-{stamp}")
    shutil.copy2(path, target)
    return target


def write(item: Plan) -> Tuple[Path, Optional[Path]]:
    """Write one plan atomically. Returns ``(path, backup_or_None)``.

    Temp-file-then-replace because these files are read by other processes at
    arbitrary moments: a half-written CLAUDE.md would be loaded as a truncated
    instruction set.
    """
    if item.path is None or not item.changes:
        raise ValueError("nothing to write")
    path = item.path
    saved: Optional[Path] = None
    if path.exists():
        saved = backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(item.after)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path, saved


def verify(path: Path, block: str) -> bool:
    """Post-write check: the block is there, and it is ours."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    found = managed.find_block(text)
    return found is not None and found.strip() == block.strip()
