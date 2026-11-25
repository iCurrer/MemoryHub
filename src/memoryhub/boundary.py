"""Repository boundary enforcement.

MemoryHub deals with two repositories that must never merge:

===========  ==========================  ==============================
repo         contents                    destination
===========  ==========================  ==============================
code repo    adapters, CLI, docs         public (GitHub)
vault repo   the user's memories         local disk, optional private remote
===========  ==========================  ==============================

The rules in ``AGENTS.md`` say so, but prose does not stop a ``git add -A``
in the wrong directory. This module turns the rule into something that can
fail: every entry point that touches a vault calls :func:`violations` first.

Nothing here inspects memory *content* — that is :mod:`memoryhub.safety`.
This module only answers "are these two trees touching each other".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional

#: Directory names that only ever exist inside a vault. Their presence in a
#: code repository means data leaked into the wrong repo.
VAULT_ONLY_DIRS = (
    "vault",
    "inbox",
    "sources",
    "exports",
    "database",
    "logs",
)

#: Substrings that identify the code repository by name. A vault remote URL
#: containing any of these is a wiring mistake, not a backup.
CODE_REPO_HINTS = ("memoryhub", "memory-hub", "memory_hub")


def _has_git_marker(path: Path) -> bool:
    """True when *path* is a git repository root (worktree or bare)."""
    marker = path / ".git"
    return marker.exists()


def git_toplevel(path: Path) -> Optional[Path]:
    """Return the repository root containing *path*, if any.

    Uses the presence of ``.git`` rather than shelling out to git so the check
    works on a fresh machine and inside tests with no git binary configured.
    A worktree whose ``.git`` is a file (submodules, linked worktrees) counts.
    """
    current = path
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if _has_git_marker(candidate):
            return candidate
    return None


def looks_like_code_repo(path: Path) -> bool:
    """Heuristic: a directory that carries this project's source tree."""
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return "name = \"memoryhub\"" in text or "name = 'memoryhub'" in text


def violations(vault: Path) -> List[str]:
    """Return every reason *vault* is an unsafe location. Empty means safe.

    Checked, in order:

    1. the vault must not sit inside another git working tree — that is how a
       ``git add -A`` in the parent sweeps user memories into a public repo;
    2. the vault must not sit inside this project's source tree;
    3. the vault must not *contain* a repository root the tool would then
       descend into.
    """
    vault = vault.expanduser().resolve()
    problems: List[str] = []

    parent_root = git_toplevel(vault.parent) if vault.parent != vault else None
    own_root = vault if _has_git_marker(vault) else None

    if parent_root is not None and parent_root != own_root:
        problems.append(
            f"vault lives inside another git repository ({parent_root}). "
            "A `git add -A` there would commit your memories."
        )

    for ancestor in vault.parents:
        if looks_like_code_repo(ancestor):
            problems.append(
                f"vault is inside the MemoryHub source tree ({ancestor}). "
                "Code and memories must not share a repository."
            )
            break

    return problems


def is_inside_code_repo(path: Path) -> bool:
    """True when *path* lives under a MemoryHub source tree.

    Import adapters use this to skip the project's own ``AGENTS.md`` /
    ``CLAUDE.md``. Running ``scan`` from inside the repo would otherwise file
    the engineering contract as the user's memories — observed once, nine
    entries of pure noise.
    """
    current = Path(path).resolve().parent
    for candidate in [current, *current.parents]:
        if looks_like_code_repo(candidate):
            return True
    return False


def remote_violations(urls: List[str]) -> List[str]:
    """Flag vault remotes that point at the code repository."""
    problems: List[str] = []
    for url in urls:
        lowered = url.lower()
        if any(hint in lowered for hint in CODE_REPO_HINTS):
            problems.append(
                f"vault remote points at the code repository: {url}"
            )
    return problems


def list_remotes(vault: Path) -> List[str]:
    """Return configured remote URLs for *vault*. Never raises."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(vault),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    urls = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1])
    return sorted(set(urls))


#: Installed into ``<vault>/.git/hooks/pre-push``. Refuses to publish memories
#: to anything that looks like the public code repository.
PRE_PUSH_HOOK = """#!/bin/sh
# Installed by `memoryhub init`. Removes the most likely way to leak a vault:
# pointing its remote at the public code repository.
#
# git passes: pre-push <remote-name> <remote-url>
url="${2:-}"
if [ -z "$url" ]; then
  url=$(git config --get remote."${1:-origin}".url 2>/dev/null)
fi
case "$url" in
  *[Mm]emory[Hh]ub*|*memory-hub*|*memory_hub*)
    echo "pre-push: refusing to push memories to the code repository ($url)" >&2
    echo "          vault remotes must be private and vault-specific." >&2
    exit 1
    ;;
esac
exit 0
"""


def install_pre_push_hook(vault: Path) -> Optional[Path]:
    """Write the guard hook. Returns the path, or None when git is absent."""
    hooks = vault / ".git" / "hooks"
    if not hooks.is_dir():
        return None
    path = hooks / "pre-push"
    path.write_text(PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass
    return path
