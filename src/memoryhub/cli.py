"""Command line interface (stdlib argparse, no third-party dependency)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, config
from .models import CATEGORIES, STATUSES, MemoryEntry, ValidationError, now_iso
from .storage.markdown_store import VaultStore

MIN_PYTHON = (3, 11)


def _store(args: argparse.Namespace) -> VaultStore:
    return VaultStore(config.resolve_vault(getattr(args, "vault", None)))


def _print_entry_line(entry: MemoryEntry) -> None:
    head = " ".join(entry.body.split())
    if len(head) > 64:
        head = head[:61] + "..."
    tags = ",".join(entry.tags)
    tagcell = f"[{tags}]" if tags else ""
    print(
        f"{entry.id}  {entry.status:<9} {entry.category:<10} "
        f"trust={entry.trust:<7} {tagcell:<14} {head}"
    )


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    if args.vault:
        vault = Path(args.vault).expanduser().resolve()
    else:
        vault = config.resolve_vault()
    store = VaultStore(vault)
    created = store.init_layout()
    written = config.save({"vault": config.as_posix(vault), "initialized_at": now_iso()})

    print(f"vault    {vault}")
    for path in created:
        print(f"  + {path.relative_to(vault).as_posix()}/")
    if not created:
        print("  (layout already present)")
    print(f"config   {written}")

    git = shutil.which("git")
    if args.no_git:
        print("git      skipped (--no-git)")
    elif git is None:
        print("git      not found on PATH; versioned backup unavailable")
    elif (vault / ".git").exists():
        print("git      repository already exists")
    else:
        result = subprocess.run(
            [git, "init"], cwd=str(vault), capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            print("git      initialised (no commit yet)")
        else:
            print(f"git      init failed: {result.stderr.strip()}")

    print("\nnext:")
    print(f"  set MEMORYHUB_VAULT={vault}   (or rely on the written config)")
    print("  memoryhub new --category preference --body '...' --tag demo")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: List[tuple[str, bool, str]] = []

    version = sys.version_info
    checks.append(
        (
            "python",
            version[:2] >= MIN_PYTHON,
            f"{version.major}.{version.minor}.{version.micro} "
            f"(requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
        )
    )

    cfg = config.config_path()
    checks.append(("config file", cfg.is_file(), str(cfg)))

    vault = config.resolve_vault(getattr(args, "vault", None))
    checks.append(("vault root", vault.is_dir(), str(vault)))

    store = VaultStore(vault)
    missing = [rel for rel in config.LAYOUT if not (vault / rel).is_dir()]
    checks.append(
        (
            "layout",
            not missing,
            "complete" if not missing else "missing: " + ", ".join(missing),
        )
    )

    git = shutil.which("git")
    has_repo = (vault / ".git").exists()
    checks.append(
        (
            "git",
            git is not None and has_repo,
            "ok" if git and has_repo else ("git missing" if not git else "vault is not a repo"),
        )
    )

    malformed = 0
    total = 0
    for _ in store.iter_entries():
        total += 1
    for path in store.entry_paths():
        try:
            store.load(path)
        except Exception:  # noqa: BLE001 - doctor must never crash
            malformed += 1
    checks.append(("entries", malformed == 0, f"{total} parsed, {malformed} malformed"))

    sync_hits = [part for part in vault.parts if part.lower().startswith("onedrive")]
    checks.append(
        (
            "cloud sync",
            not sync_hits,
            "vault is outside OneDrive" if not sync_hits else f"vault is under {sync_hits[0]}",
        )
    )

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        flag = "OK  " if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{flag}] {name:<{width}}  {detail}")

    print()
    if failed:
        print(f"{failed} check(s) failed")
        return 1
    print("all checks passed")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    body = args.body
    if args.file:
        body = Path(args.file).expanduser().read_text(encoding="utf-8")
    if not body or not body.strip():
        print("error: --body or --file is required and must not be empty", file=sys.stderr)
        return 2

    entry = MemoryEntry.create(
        category=args.category,
        body=body.strip() + "\n",
        tags=args.tag or [],
        source_adapter=args.adapter,
        source_path=args.source_path or "",
        source_kind=args.kind,
        sensitivity=args.sensitivity,
    )
    if args.approve:
        if entry.trust == "blocked":
            print("error: entry is blocked by policy and cannot be approved", file=sys.stderr)
            return 1
        entry.status = "approved"

    store = _store(args)
    path = store.save(entry)
    print(f"{entry.id}  status={entry.status} trust={entry.trust}")
    print(f"  {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    shown = 0
    for entry in store.iter_entries():
        if args.status and entry.status != args.status:
            continue
        if args.category and entry.category != args.category:
            continue
        _print_entry_line(entry)
        shown += 1
    print(f"\n{shown} entries")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    entry = store.get(args.id)
    if entry is None:
        print(f"error: no such memory: {args.id}", file=sys.stderr)
        return 1
    print(entry.to_markdown())
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    store = _store(args)
    hits = store.search(args.query)
    for entry in hits:
        _print_entry_line(entry)
    print(f"\n{len(hits)} matches for {args.query!r}")
    return 0


def cmd_set_status(args: argparse.Namespace, status: str) -> int:
    store = _store(args)
    try:
        entry = store.set_status(args.id, status)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{entry.id}  status={entry.status}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store = _store(args)
    stats = store.stats()
    print(f"vault    {store.root}")
    print(f"total    {stats['total']}")
    for status, count in sorted(stats["by_status"].items()):
        print(f"  {status:<10} {count}")
    for category, count in sorted(stats["by_category"].items()):
        print(f"  {category:<10} {count}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryhub",
        description="Cross-AI personal memory governance layer (local-first).",
    )
    parser.add_argument("--version", action="version", version=f"memoryhub {__version__}")
    parser.add_argument(
        "--vault", metavar="PATH", help="override the vault location for this call"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the vault layout and write config")
    p_init.add_argument("--vault", metavar="PATH", default=None, help="vault location")
    p_init.add_argument("--no-git", action="store_true", help="do not run git init")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="run environment and consistency checks")
    p_doctor.set_defaults(func=cmd_doctor)

    p_new = sub.add_parser("new", help="create a memory proposal")
    p_new.add_argument("--category", required=True, choices=CATEGORIES)
    p_new.add_argument("--body", default="", help="memory text")
    p_new.add_argument("--file", default=None, help="read memory text from a file")
    p_new.add_argument("--tag", action="append", default=[], help="repeatable tag")
    p_new.add_argument("--adapter", default="manual", help="source adapter name")
    p_new.add_argument("--source-path", dest="source_path", default=None)
    p_new.add_argument(
        "--kind",
        default="memory",
        choices=("instruction", "memory", "transcript", "doc"),
        help="what the source actually is; AGENTS.md style files are 'instruction'",
    )
    p_new.add_argument(
        "--sensitivity", default="normal", choices=("normal", "personal", "sensitive", "secret")
    )
    p_new.add_argument(
        "--approve", action="store_true", help="write directly as approved (refused if blocked)"
    )
    p_new.set_defaults(func=cmd_new)

    p_list = sub.add_parser("list", help="list memories")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.add_argument("--category", choices=CATEGORIES, default=None)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="print one memory as markdown")
    p_show.add_argument("id")
    p_show.set_defaults(func=cmd_show)

    p_search = sub.add_parser("search", help="substring search over body and tags")
    p_search.add_argument("query")
    p_search.set_defaults(func=cmd_search)

    p_ok = sub.add_parser("approve", help="approve a pending proposal")
    p_ok.add_argument("id")
    p_ok.set_defaults(func=lambda a: cmd_set_status(a, "approved"))

    p_no = sub.add_parser("reject", help="reject a pending proposal")
    p_no.add_argument("id")
    p_no.set_defaults(func=lambda a: cmd_set_status(a, "rejected"))

    p_stats = sub.add_parser("stats", help="show vault statistics")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
