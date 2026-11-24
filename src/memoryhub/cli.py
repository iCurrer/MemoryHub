"""Command line interface (stdlib argparse, no third-party dependency)."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__, config
from .adapters import ADAPTERS, build
from .frontmatter import FrontmatterError
from .models import CATEGORIES, STATUSES, MemoryEntry, ValidationError, now_iso
from .storage.markdown_store import LockTimeout, VaultStore

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


def _unreadable_hint(errors: List[Tuple[Path, str]]) -> None:
    """One stderr line per command: unreadable files must never be silent."""
    if not errors:
        return
    print(
        f"warning: {len(errors)} file(s) could not be read and were skipped; "
        "run `memoryhub doctor` for details",
        file=sys.stderr,
    )


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    vault_arg = getattr(args, "vault", None)
    if vault_arg:
        vault = Path(vault_arg).expanduser().resolve()
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
    """Report problems in three levels.

    ``FAIL`` blocks the phase gate; ``WARN`` is a state the user can legitimately
    be in (no config yet, vault not under git). Phase 1 treated both as FAIL,
    which made the documented acceptance command impossible to pass on a fresh
    clone.
    """
    checks: List[Tuple[str, str, str]] = []

    def add(name: str, level: str, detail: str) -> None:
        checks.append((name, level, detail))

    version = sys.version_info
    python_ok = version[:2] >= MIN_PYTHON
    add(
        "python",
        "OK" if python_ok else "FAIL",
        f"{version.major}.{version.minor}.{version.micro} "
        f"(requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})",
    )

    cfg = config.config_path()
    add(
        "config file",
        "OK" if cfg.is_file() else "WARN",
        str(cfg) if cfg.is_file() else f"{cfg} (absent; run `memoryhub init`)",
    )

    vault = config.resolve_vault(getattr(args, "vault", None))
    add("vault root", "OK" if vault.is_dir() else "FAIL", str(vault))

    missing = [rel for rel in config.LAYOUT if not (vault / rel).is_dir()]
    add(
        "layout",
        "OK" if not missing else "FAIL",
        "complete" if not missing else "missing: " + ", ".join(missing),
    )

    git = shutil.which("git")
    has_repo = (vault / ".git").exists()
    if git is None:
        git_level, git_detail = "WARN", "git not on PATH; versioned backup unavailable"
    elif not has_repo:
        git_level, git_detail = "WARN", "vault is not a git repo; run `git init` for backup"
    else:
        git_level, git_detail = "OK", "ok"
    add("git", git_level, git_detail)

    store = VaultStore(vault)
    entries, unreadable = store.scan()
    add(
        "entries",
        "FAIL" if unreadable else "OK",
        f"{len(entries)} parsed, {len(unreadable)} unreadable",
    )
    for path, message in unreadable:
        print(f"  unreadable {path}: {message}", file=sys.stderr)

    sync_hits = [part for part in vault.parts if part.lower().startswith("onedrive")]
    add(
        "cloud sync",
        "WARN" if sync_hits else "OK",
        "vault is outside OneDrive" if not sync_hits else f"vault is under {sync_hits[0]}",
    )

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    warned = 0
    for name, level, detail in checks:
        if level == "FAIL":
            failed += 1
        elif level == "WARN":
            warned += 1
        print(f"[{level:<4}] {name:<{width}}  {detail}")

    print()
    if failed:
        print(f"{failed} check(s) failed, {warned} warning(s)")
        return 1
    if warned:
        print(f"all checks passed ({warned} warning(s))")
    else:
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
        if entry.trust == "review" and not args.force:
            print(
                f"error: {entry.category} entries require review (ADR-003); "
                "run `memoryhub approve` after reading them, or pass --force",
                file=sys.stderr,
            )
            return 1
        entry.status = "approved"

    store = _store(args)
    path = store.create(entry)
    print(f"{entry.id}  status={entry.status} trust={entry.trust}")
    print(f"  {path}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Collect from every adapter into ``inbox/pending`` (ADR-002: read-only)."""
    store = _store(args)
    selected = [args.adapter] if args.adapter else sorted(ADAPTERS)

    existing_entries, unreadable = store.scan()
    _unreadable_hint(unreadable)
    seen = {(e.source_adapter, e.source_path, e.body) for e in existing_entries}

    sources = imported = skipped = failed = 0
    for name in selected:
        adapter = build(name)
        try:
            discovered = adapter.discover()
        except OSError as exc:
            print(f"error: {name}: discovery failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"{name:<10} {len(discovered)} source(s)  read-only")
        for source in discovered:
            sources += 1
            try:
                entries = adapter.read(source)
            except (OSError, UnicodeDecodeError, ValidationError) as exc:
                print(f"error: {name}: cannot read {source.path}: {exc}", file=sys.stderr)
                failed += 1
                continue
            fresh = duplicate = 0
            for entry in entries:
                fingerprint = (entry.source_adapter, entry.source_path, entry.body)
                if fingerprint in seen:
                    duplicate += 1
                    skipped += 1
                    continue
                fresh += 1
                imported += 1
                if not args.dry_run:
                    store.create(entry)
                seen.add(fingerprint)
            print(f"  {source.label()}: {fresh} new, {duplicate} already present")

    print()
    print(f"sources   {sources}")
    if args.dry_run:
        print(f"entries   {imported} would be imported (dry run, nothing written)")
    else:
        print(f"entries   {imported} imported, {skipped} already present")
    if failed:
        print(f"{failed} source(s) failed", file=sys.stderr)
        return 1
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the read-only MCP tools over stdio.

    Imported lazily: the SDK pulls in a handful of packages, and the rest of the
    CLI should not pay for them (nor fail if they are missing).
    """
    try:
        from .mcp import serve
    except ImportError as exc:  # pragma: no cover - depends on the install
        print(f"error: the MCP server needs the `mcp` package: {exc}", file=sys.stderr)
        return 1
    serve(getattr(args, "vault", None))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    entries, errors = store.scan()
    shown = 0
    for entry in entries:
        if args.status and entry.status != args.status:
            continue
        if args.category and entry.category != args.category:
            continue
        # An explicit --status is a history query, so expiry does not apply.
        if not args.include_expired and not args.status and entry.is_expired():
            continue
        _print_entry_line(entry)
        shown += 1
    _unreadable_hint(errors)
    print(f"\n{shown} entries")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        entry = store.get(args.id)
    except (ValidationError, FrontmatterError) as exc:
        print(f"error: {args.id} cannot be read: {exc}", file=sys.stderr)
        return 1
    if entry is None:
        print(f"error: no such memory: {args.id}", file=sys.stderr)
        return 1
    print(entry.to_markdown())
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    store = _store(args)
    hits = store.search(args.query, include_expired=args.include_expired)
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
    if stats["unreadable"]:
        print(f"  unreadable {stats['unreadable']}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------- parser


def _vault_parent() -> argparse.ArgumentParser:
    """``--vault`` accepted both before and after the subcommand.

    The subparser copy uses ``SUPPRESS`` so that an omitted value does not
    overwrite one already parsed at the top level; without this,
    ``memoryhub --vault X init`` silently fell back to the default vault.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--vault",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="override the vault location for this call",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    vault_arg = _vault_parent()
    parser = argparse.ArgumentParser(
        prog="memoryhub",
        parents=[vault_arg],
        description="Cross-AI personal memory governance layer (local-first).",
    )
    parser.add_argument("--version", action="version", version=f"memoryhub {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init", parents=[vault_arg], help="create the vault layout and write config"
    )
    p_init.add_argument("--no-git", action="store_true", help="do not run git init")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser(
        "doctor", parents=[vault_arg], help="run environment and consistency checks"
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_new = sub.add_parser("new", parents=[vault_arg], help="create a memory proposal")
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
    p_new.add_argument(
        "--force",
        action="store_true",
        help="with --approve: also bypass the review tier (ADR-003 escape hatch)",
    )
    p_new.set_defaults(func=cmd_new)

    p_scan = sub.add_parser(
        "scan", parents=[vault_arg], help="collect from external sources into inbox"
    )
    p_scan.add_argument(
        "--adapter",
        choices=sorted(ADAPTERS),
        default=None,
        help="only run one adapter (default: all)",
    )
    p_scan.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing anything",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_mcp = sub.add_parser(
        "mcp",
        parents=[vault_arg],
        help="serve the read-only MCP tools over stdio (for Cursor / Claude Desktop)",
    )
    p_mcp.set_defaults(func=cmd_mcp)

    p_list = sub.add_parser("list", parents=[vault_arg], help="list memories")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.add_argument("--category", choices=CATEGORIES, default=None)
    p_list.add_argument(
        "--include-expired",
        action="store_true",
        help="also show entries whose valid_until has passed",
    )
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser(
        "show", parents=[vault_arg], help="print one memory as markdown"
    )
    p_show.add_argument("id")
    p_show.set_defaults(func=cmd_show)

    p_search = sub.add_parser(
        "search", parents=[vault_arg], help="substring search over body and tags"
    )
    p_search.add_argument("query")
    p_search.add_argument(
        "--include-expired",
        action="store_true",
        help="also match entries whose valid_until has passed",
    )
    p_search.set_defaults(func=cmd_search)

    p_ok = sub.add_parser(
        "approve", parents=[vault_arg], help="approve a pending proposal"
    )
    p_ok.add_argument("id")
    p_ok.set_defaults(func=lambda a: cmd_set_status(a, "approved"))

    p_no = sub.add_parser(
        "reject", parents=[vault_arg], help="reject a pending proposal"
    )
    p_no.add_argument("id")
    p_no.set_defaults(func=lambda a: cmd_set_status(a, "rejected"))

    p_stats = sub.add_parser(
        "stats", parents=[vault_arg], help="show vault statistics"
    )
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValidationError, FrontmatterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except LockTimeout as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: another memoryhub process is writing; retry in a moment", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
