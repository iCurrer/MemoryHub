#!/usr/bin/env python3
"""Pre-commit boundary guard for the MemoryHub code repository.

Two failure modes this exists to prevent, both of which happened once:

1. user data committed into the public code repository;
2. personal identifiers (home address, phone, national ID, profile UUID)
   leaking through a commit — including through commit messages, which this
   hook cannot see and which is why ``--message`` scanning is separate.

Run manually:

    python tools/lint_staged.py            # staged changes only
    python tools/lint_staged.py --all      # every tracked file

Exit code 1 means "do not commit".
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]

#: Directories that only exist inside a vault. Any path under these is data.
FORBIDDEN_PREFIXES = (
    "vault/",
    "inbox/",
    "sources/",
    "exports/",
    "database/",
    "logs/",
    "backups/",
)

#: File shapes that are runtime state, never source.
FORBIDDEN_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".db-journal", ".lock")

#: (rule name, regex, human description)
RULES: Tuple[Tuple[str, str, str], ...] = (
    (
        "address",
        r"[\u4e00-\u9fff]{2,6}(?:路|街|道|巷|弄|胡同)\s*\d+\s*号",
        "street address with a house number",
    ),
    (
        "home",
        r"(?:[A-Za-z]:[\\/]Users[\\/][^\\/\s\"']+|/Users/[A-Za-z][^/\s\"']*|/home/[a-z][^/\s\"']*)",
        "absolute home directory (contains a username)",
    ),
    (
        "uuid",
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "UUID (profile / session identifiers are stable, not random)",
    ),
    (
        "phone",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "mainland mobile number",
    ),
    (
        "national-id",
        r"(?<!\d)\d{17}[\dXx](?!\d)",
        "national ID number",
    ),
    (
        "secret",
        r"sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}",
        "API token or access key",
    ),
    (
        "school-name",
        r"上海外国语大学|贤达经济人文学院",
        "real institution name (test fixtures must use placeholders)",
    ),
)

ALLOWLIST = ROOT / "tools" / "pii-allowlist.txt"

#: Put this on a single line to mark its value as deliberately synthetic.
SYNTHETIC_MARKER = "pii:synthetic"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else ""


def staged_files() -> List[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [line for line in out.splitlines() if line.strip()]


def tracked_files() -> List[str]:
    out = _git("ls-files")
    return [line for line in out.splitlines() if line.strip()]


def load_allowlist() -> List[Tuple[str, str]]:
    """Return ``(rule, glob)`` pairs from the allowlist file."""
    if not ALLOWLIST.is_file():
        return []
    pairs = []
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        pairs.append((parts[0], parts[1]))
    return pairs


def _glob_match(path: str, pattern: str) -> bool:
    """Minimal glob: supports ``**``, ``*`` and exact prefixes."""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-3]) or path == pattern[:-3].rstrip("/")
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def _allowed(rule: str, path: str, allow: List[Tuple[str, str]]) -> bool:
    return any(r == rule and _glob_match(path, g) for r, g in allow)


def read_text(path: Path) -> str:
    """Return file text, or '' for binaries."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in data[:4096]:
        return ""
    return data.decode("utf-8", errors="ignore")


def check_path(path: str) -> List[str]:
    problems = []
    lowered = path.lower()
    if any(lowered.startswith(prefix) or f"/{prefix}" in f"/{lowered}" for prefix in FORBIDDEN_PREFIXES):
        problems.append(f"{path}: vault data path — memories never belong in this repo")
    if lowered.endswith(FORBIDDEN_SUFFIXES):
        problems.append(f"{path}: runtime state file (index, database or lock)")
    return problems


def check_content(path: str, allow: List[Tuple[str, str]]) -> List[str]:
    text = read_text(ROOT / path)
    if not text:
        return []
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        # Line-level opt-out for values that must *look* plausible to test a
        # detector. Kept per line rather than per file so a real address pasted
        # into the same fixture file still fails.
        if SYNTHETIC_MARKER in line:
            continue
        for rule, pattern, description in RULES:
            if _allowed(rule, path, allow):
                continue
            if re.search(pattern, line):
                problems.append(f"{path}:{number}: {description} — {line.strip()[:90]}")
                break
    return problems


def check_commit_message(message: str) -> List[str]:
    """Same rules, no allowlist. Commit messages are published verbatim."""
    problems = []
    for number, line in enumerate(message.splitlines(), 1):
        for rule, pattern, description in RULES:
            if re.search(pattern, line):
                problems.append(f"commit message:{number}: {description} — {line.strip()[:90]}")
                break
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="scan tracked files instead of staged")
    parser.add_argument("--message-file", help="also scan a commit message file")
    parser.add_argument(
        "--message-only",
        action="store_true",
        help="scan only the commit message (used by the commit-msg hook)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.message_only:
        files: List[str] = []
    else:
        files = tracked_files() if args.all else staged_files()
    allow = load_allowlist()

    problems: List[str] = []
    for path in files:
        problems.extend(check_path(path))
        if not any(path == p.split(":")[0] for p in problems):
            problems.extend(check_content(path, allow))

    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8", errors="ignore")
        problems.extend(check_commit_message(message))

    if problems:
        print("boundary check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nfix the content, or add an explicit exemption to "
            "tools/pii-allowlist.txt (rule + glob) if it is a synthetic fixture.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"boundary check passed ({len(files)} file(s), {len(RULES)} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
