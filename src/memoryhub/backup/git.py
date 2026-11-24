"""Git operations for the vault (P5-04).

The vault is its own repository, separate from the code repo (ADR-007). Two rules
shape this module:

- **A local commit is always safe; a push is not.** Committing to a private disk is
  reversible and offline. Pushing sends a copy to a third party, so the safety gate
  lives on the push path only -- refusing to commit would just lose history.
- **The gate is about content, not intent.** If the vault holds anything marked
  ``sensitive`` / ``secret`` (or therefore ``blocked``), ``push`` refuses and lists
  the ids. ``--allow-sensitive`` exists for people who have consciously accepted
  the trade-off; it is not a default.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import MemoryEntry
from ..storage.markdown_store import VaultStore

#: Never commit build artefacts or a second copy of every memory.
GITIGNORE_LINES = (
    "# MemoryHub: the index and logs are derived/runtime state, not memory.",
    "database/",
    "logs/",
    "*.tmp*",
    ".memoryhub.lock",
)

SENSITIVE_LEVELS = ("sensitive", "secret")


class GitUnavailable(RuntimeError):
    """Raised when git is missing or a command fails."""


def _git(vault: Path, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git")
    if git is None:
        raise GitUnavailable("git not found on PATH")
    return subprocess.run(
        [git, "-C", str(vault), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def ensure_repo(vault: Path) -> bool:
    """Create the vault repo if missing. Returns True when it was created."""
    vault = Path(vault)
    if (vault / ".git").exists():
        return False
    result = _git(vault, "init", "-q")
    if result.returncode != 0:
        raise GitUnavailable(result.stderr.strip() or "git init failed")
    return True


def write_gitignore(vault: Path) -> bool:
    """Ensure ``.gitignore`` covers derived state without clobbering the user's.

    Returns True when the file changed.
    """
    path = Path(vault) / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    present = {line.strip() for line in existing}
    missing = [line for line in GITIGNORE_LINES if line.strip() not in present]
    if not missing:
        return False
    lines = list(existing)
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(missing)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def has_changes(vault: Path) -> bool:
    result = _git(vault, "status", "--porcelain")
    return bool(result.stdout.strip())


def commit(vault: Path, message: str) -> Optional[str]:
    """Stage everything and commit. Returns the short hash, or None if nothing changed."""
    if not has_changes(vault):
        return None
    add = _git(vault, "add", "-A")
    if add.returncode != 0:
        raise GitUnavailable(add.stderr.strip() or "git add failed")
    # ``-c user.name/-c user.email`` keep this working on a machine with no global
    # git identity, which is common for a fresh install.
    done = _git(
        vault,
        "-c",
        "user.name=MemoryHub",
        "-c",
        "user.email=memoryhub@localhost",
        "commit",
        "-q",
        "-m",
        message,
    )
    if done.returncode != 0:
        raise GitUnavailable(done.stderr.strip() or "git commit failed")
    sha = _git(vault, "rev-parse", "--short", "HEAD")
    return sha.stdout.strip() or None


def remotes(vault: Path) -> List[str]:
    result = _git(vault, "remote")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def push(vault: Path, remote: str = "origin") -> Tuple[bool, str]:
    if remote not in remotes(vault):
        return False, f"no such remote: {remote} (add one with `git remote add`)"
    branch = _git(vault, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    result = _git(vault, "push", "-u", remote, branch)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, f"pushed {branch} -> {remote}"


def sensitive_entries(store: VaultStore) -> List[MemoryEntry]:
    """Entries that must not leave the machine without a conscious decision."""
    entries, _errors = store.scan()
    return [
        entry
        for entry in entries
        if entry.sensitivity in SENSITIVE_LEVELS or entry.trust == "blocked"
    ]
