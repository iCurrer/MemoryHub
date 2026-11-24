"""Change history read from the vault's own git log (P4-03).

ADR-003 accepted a real cost: memories get auto-approved, so the user must be able
to review changes afterwards. That promise is only keepable if "what changed"
can be answered cheaply. We answer it from **git**, not from a private journal,
for three reasons:

- git is already the rollback mechanism ADR-003 relies on, so the log is real
  history rather than a second bookkeeping system that can disagree;
- nothing new to keep in sync, and no new file in the vault that is not memory;
- `git log` is available everywhere the backup feature will need it anyway.

The cost of that choice is explicit: a vault that was never committed has no
history, and this module says so instead of returning an empty list that looks
like "nothing changed".
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_ID_RE = re.compile(r"^(mem_\d{8}T\d{6}_[0-9a-f]+)\.md$")
_RELATIVE_RE = re.compile(r"^(\d+)\s*([dhw])$")

COMMIT_MARK = "__COMMIT__"


@dataclass
class Change:
    action: str  # A / M / D
    path: str
    entry_id: str = ""
    category: str = ""

    @property
    def label(self) -> str:
        return {"A": "added", "M": "updated", "D": "removed"}.get(self.action, self.action)


@dataclass
class Commit:
    digest: str
    date: str
    subject: str
    changes: List[Change] = field(default_factory=list)


class HistoryUnavailable(RuntimeError):
    """Raised when the log cannot be read (no git, or the vault is not a repo)."""


def since_to_git(spec: Optional[str]) -> Optional[str]:
    """Turn ``7d`` / ``12h`` / ``2w`` into something ``git log --since`` accepts.

    An ISO date or any other string git understands passes through untouched.
    """
    if not spec:
        return None
    match = _RELATIVE_RE.match(spec.strip())
    if not match:
        return spec.strip()
    amount, unit = match.groups()
    words = {"d": "days", "h": "hours", "w": "weeks"}[unit]
    return f"{amount} {words} ago"


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if git is None:
        raise HistoryUnavailable("git not found on PATH")
    return subprocess.run(
        [git, "-C", str(vault), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def is_repo(vault: Path) -> bool:
    return (Path(vault) / ".git").exists()


def collect(vault: Path, since: Optional[str] = None, limit: int = 50) -> List[Commit]:
    """Commits touching the vault, newest first, with their memory-level changes."""
    vault = Path(vault)
    if not is_repo(vault):
        raise HistoryUnavailable(
            f"vault is not a git repository: {vault}; run `git init` there to get history"
        )
    args = [
        "log",
        f"--max-count={limit}",
        "--name-status",
        # Deterministic statuses: a rename would emit two paths on one line and
        # hide the entry id, and it is not a distinction the review loop needs.
        "--no-renames",
        f"--pretty=format:{COMMIT_MARK}%h|%ad|%s",
        "--date=iso-strict",
    ]
    spec = since_to_git(since)
    if spec:
        args.append(f"--since={spec}")
    result = _git(vault, *args)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "does not have any commits" in message or "unknown revision" in message:
            raise HistoryUnavailable("the repository has no commits yet")
        raise HistoryUnavailable(message or "git log failed")

    commits: List[Commit] = []
    current: Optional[Commit] = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(COMMIT_MARK):
            digest, _, rest = line[len(COMMIT_MARK) :].partition("|")
            date, _, subject = rest.partition("|")
            current = Commit(digest=digest, date=date, subject=subject)
            commits.append(current)
            continue
        if current is None or "\t" not in raw:
            continue
        status, _, path = raw.partition("\t")
        action = status[:1]
        change = Change(action=action, path=path)
        match = _ID_RE.match(Path(path).name)
        if match:
            change.entry_id = match.group(1)
            change.category = Path(path).parent.name
        current.changes.append(change)
    return commits


def summarise(commits: List[Commit]) -> dict:
    counts = {"A": 0, "M": 0, "D": 0}
    touched = set()
    for commit in commits:
        for change in commit.changes:
            if change.action in counts:
                counts[change.action] += 1
            if change.entry_id:
                touched.add(change.entry_id)
    return {
        "commits": len(commits),
        "added": counts["A"],
        "updated": counts["M"],
        "removed": counts["D"],
        "entries": len(touched),
    }
