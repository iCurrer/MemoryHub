"""Command line interface (stdlib argparse, no third-party dependency)."""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__, boundary, config, conflicts, review, sync
from .adapters import ADAPTERS, build
from .ai import (
    DEFAULT_MAX_PAIRS,
    PER_ENTRY_PAIR,
    AIConfig,
    AIError,
    Provider,
    detect,
    looks_mixed,
    organize,
)
from .ai.conflict import RELATION_LABELS
from .backup import (
    GitUnavailable,
    HistoryUnavailable,
    collect,
    commit as git_commit,
    ensure_repo,
    push as git_push,
    sensitive_entries,
    summarise,
    write_gitignore,
)
from .classify import classify as classify_category
from .frontmatter import FrontmatterError
from .models import CATEGORIES, STATUSES, MemoryEntry, ValidationError, now_iso
from .safety import classify
from .storage.index import Index, IndexUnavailable
from .storage.markdown_store import LockTimeout, VaultStore
from .watcher import import_entries, watch

MIN_PYTHON = (3, 11)


def _store(args: argparse.Namespace) -> VaultStore:
    """Resolve the vault and refuse to invent one.

    Without this check every command silently wrote into the hard-coded default
    (`D:/my-memory`), which is how a test run once polluted a real path.
    """
    root = config.resolve_vault(getattr(args, "vault", None))
    if not root.is_dir():
        print(
            f"error: vault not found: {root}\n"
            f"       run `memoryhub init --vault {root}` first",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return VaultStore(root)


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

    unsafe = boundary.violations(vault)
    if unsafe:
        print(f"refusing to initialise a vault at {vault}", file=sys.stderr)
        for problem in unsafe:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nthe code repository and the vault repository must stay separate. "
            "Pick a path outside any git working tree, e.g. D:/my-memory.",
            file=sys.stderr,
        )
        return 2

    store = VaultStore(vault)
    created = store.init_layout()
    # Merge, never replace: re-running `init` used to rewrite the whole config
    # and silently drop a hand-written `[ai]` section (known issue #5).
    settings = config.load()
    settings.update({"vault": config.as_posix(vault), "initialized_at": now_iso()})
    written = config.save(settings)

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
        hook = boundary.install_pre_push_hook(vault)
        print(f"         pre-push guard {'installed' if hook else 'unavailable'}")
    else:
        result = subprocess.run(
            [git, "init"], cwd=str(vault), capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            print("git      initialised (no commit yet)")
            hook = boundary.install_pre_push_hook(vault)
            print(f"         pre-push guard {'installed' if hook else 'unavailable'}")
        else:
            print(f"git      init failed: {result.stderr.strip()}")

    print("boundary vault is outside the code repository")

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
        "complete"
        if not missing
        # The real failure this produced: a vault created before `exports/`
        # joined LAYOUT stayed "incomplete" forever, and nothing said that
        # `init` is idempotent and would create only the missing directory.
        else "missing: " + ", ".join(missing) + " (run `memoryhub init` again)",
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

    approved = sum(1 for entry in entries if entry.status == "approved")
    if not entries:
        add("mcp visibility", "WARN", "vault is empty; nothing to query yet")
    elif approved == 0:
        add(
            "mcp visibility",
            "WARN",
            f"0 of {len(entries)} entries approved — MCP search/list will return "
            "nothing until you review some (`memoryhub review`)",
        )
    else:
        add("mcp visibility", "OK", f"{approved} of {len(entries)} entries approved")

    report = Index(vault).check(store)
    index_level = {"ok": "OK", "absent": "WARN", "stale": "WARN", "broken": "FAIL"}[
        report["state"]
    ]
    add("index", index_level, report["detail"])

    sync_hits = [part for part in vault.parts if part.lower().startswith("onedrive")]
    add(
        "cloud sync",
        "WARN" if sync_hits else "OK",
        "vault is outside OneDrive" if not sync_hits else f"vault is under {sync_hits[0]}",
    )

    isolation = boundary.violations(vault)
    add(
        "repo isolation",
        "OK" if not isolation else "FAIL",
        "vault is outside the code repository" if not isolation else isolation[0],
    )

    remotes = boundary.list_remotes(vault)
    remote_problems = boundary.remote_violations(remotes)
    if not remotes:
        remote_level, remote_detail = "OK", "no remote (local only)"
    elif remote_problems:
        remote_level, remote_detail = "FAIL", remote_problems[0]
    else:
        remote_level, remote_detail = "OK", ", ".join(remotes)
    add("vault remote", remote_level, remote_detail)

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
    if getattr(args, "ai", False):
        return _scan_with_ai(store, args)

    selected = [args.adapter] if args.adapter else sorted(ADAPTERS)

    existing_entries, unreadable = store.scan()
    _unreadable_hint(unreadable)
    seen = {(e.source_adapter, e.source_path, e.body) for e in existing_entries}

    #: Pending entries from the sources about to be re-read. ``--refresh``
    #: archives the ones whose body no longer comes back from the adapter -- that
    #: is how a change in splitting granularity replaces old coarse entries
    #: instead of leaving both generations in the queue.
    stale_candidates: List[MemoryEntry] = []
    if args.refresh and not args.dry_run:
        stale_candidates = [e for e in existing_entries if e.status == "pending"]

    sources = imported = skipped = failed = archived = 0
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
            fresh, duplicate = import_entries(
                store, entries, seen, write=not args.dry_run
            )
            imported += fresh
            skipped += duplicate
            print(f"  {source.label()}: {fresh} new, {duplicate} already present")

            if args.refresh and not args.dry_run and fresh:
                new_bodies = {entry.body for entry in entries}
                source_path = Path(source.path).as_posix()
                outdated = [
                    entry
                    for entry in stale_candidates
                    if entry.source_adapter == name
                    and entry.source_path == source_path
                    and entry.body not in new_bodies
                ]
                for entry in outdated:
                    store.set_status(entry.id, "archived")
                if outdated:
                    archived += len(outdated)
                    print(
                        f"    archived {len(outdated)} pending entr"
                        f"{'y' if len(outdated) == 1 else 'ies'} superseded by these"
                    )

    print()
    print(f"sources   {sources}")
    if args.dry_run:
        print(f"entries   {imported} would be imported (dry run, nothing written)")
    else:
        print(f"entries   {imported} imported, {skipped} already present")
        if archived:
            print(f"archived  {archived} superseded (recoverable with `list --status archived`)")
    if failed:
        print(f"{failed} source(s) failed", file=sys.stderr)
        return 1
    return 0


def _scan_with_ai(store: VaultStore, args: argparse.Namespace) -> int:
    """Read sources verbatim and let the user's own model split and categorise.

    Only one thing changes versus the default path: ``read``. ``discover()`` stays
    algorithmic because finding files is deterministic and cheap; splitting and
    naming a fact are semantic, and the rule-based versions of those measured
    52.8% on the real vault (``docs/04-classify-audit.md``).

    Sending memory off the machine is a privacy event, so it is announced and
    confirmed rather than implied by a flag. The content is NOT redacted before
    sending -- saying otherwise would be a lie that makes people trust the wrong
    thing.
    """
    try:
        cfg = AIConfig.from_config()
        if cfg is None:
            print(
                "error: 没有配置 AI，--ai 不可用。\n"
                f"       在 {config.config_path()} 里加 [ai] 的 base_url 与 model：\n"
                '           [ai]\n'
                '           base_url = "https://api.deepseek.com/v1"\n'
                '           model = "deepseek-chat"\n'
                "       不配置也能用：默认路径按规则切分，功能完整。",
                file=sys.stderr,
            )
            return 1
        cfg.api_key()
    except AIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    selected = [args.adapter] if getattr(args, "adapter", None) else sorted(ADAPTERS)
    plan: List[Tuple[str, object, str]] = []
    total_chars = 0
    failed = 0

    for name in selected:
        adapter = build(name)
        try:
            discovered = adapter.discover()
        except OSError as exc:
            print(f"error: {name}: discovery failed: {exc}", file=sys.stderr)
            failed += 1
            continue
        for source in discovered:
            try:
                text = Path(source.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"error: {name}: cannot read {source.path}: {exc}", file=sys.stderr)
                failed += 1
                continue
            if not text.strip():
                continue
            plan.append((name, source, text))
            total_chars += len(text)

    if not plan:
        print("没有可整理的内容。")
        return 0 if not failed else 1

    print(f"AI 整理：{len(plan)} 个文件，共 {total_chars:,} 字")
    print(f"  端点  {cfg.base_url}")
    print(f"  模型  {cfg.model}")
    print("  注意  内容会离开本机，发送前不做脱敏。")
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print(
                "error: 需要确认。非交互环境下加 --yes，表示已知悉内容会被发送到该端点。",
                file=sys.stderr,
            )
            return 1
        if input("继续？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消，未发送任何内容。")
            return 1
    print()

    provider = Provider(cfg)
    seen_entries, unreadable = store.scan()
    _unreadable_hint(unreadable)
    seen = {(e.source_adapter, e.source_path, e.body) for e in seen_entries}

    dry_run = getattr(args, "dry_run", False)
    imported = skipped = unverified = 0
    problems: List[str] = []

    for name, source, text in plan:
        print(f"  {source.label()}  ", end="", flush=True)
        try:
            result = organize(text, provider, source=source)
        except AIError as exc:
            print("failed")
            print(f"error: {exc}", file=sys.stderr)
            failed += 1
            continue
        fresh, duplicate = import_entries(store, result.entries, seen, write=not dry_run)
        imported += fresh
        skipped += duplicate
        unverified += result.unverified
        problems.extend(f"{source.label()}: {problem}" for problem in result.problems)
        print(
            f"{len(result.entries)} 条 → {fresh} new, {duplicate} dup, "
            f"{result.unverified} 待核实"
        )

    print()
    if dry_run:
        print(f"entries   {imported} would be imported (dry run, nothing written)")
    else:
        print(f"entries   {imported} imported, {skipped} already present")
        if unverified:
            print(f"unverified {unverified} 条 quote 定位不到，审核时优先看这些")
    if problems:
        print()
        print(f"{len(problems)} 个问题：")
        for line in problems[:20]:
            print(f"  {line}")
        if len(problems) > 20:
            print(f"  ... 其余 {len(problems) - 20} 条省略")
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
    serve(getattr(args, "vault", None), args.permission)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Poll the sources and import whatever settled down (P2-06)."""
    store = _store(args)
    quiet = 0.0 if args.once else args.quiet
    print(f"watching  {store.root}")
    print(f"interval  {args.interval}s   quiet {quiet}s   {'once' if args.once else 'until Ctrl-C'}")
    try:
        watch(
            store,
            interval=args.interval,
            quiet_seconds=quiet,
            iterations=1 if args.once else None,
            on_change=lambda result: print(
                f"  + {result.imported} imported, {result.duplicate} already present"
            ),
        )
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Build, rebuild, or report the SQLite index (P2-02)."""
    store = _store(args)
    index = Index(store.root)
    if args.rebuild:
        count = index.rebuild(store)
        print(f"rebuilt  {index.path}")
        print(f"entries  {count}")
        return 0
    rebuilt = index.ensure_fresh(store)
    report = index.check(store)
    print(f"index    {index.path}")
    print(f"state    {report['state']}  ({report['detail']})")
    if rebuilt:
        print("note     index was stale and has been rebuilt")
    return 0


def _counterpart(store: VaultStore, entry: MemoryEntry) -> Optional[MemoryEntry]:
    """Newest approved entry from the same source file, if any."""
    entries, _errors = store.scan()
    candidates = [
        other
        for other in entries
        if other.id != entry.id
        and other.status == "approved"
        and other.source_path == entry.source_path
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.updated_at)


def cmd_diff(args: argparse.Namespace) -> int:
    """Show a proposal next to the entry it would replace (P4-01)."""
    store = _store(args)
    try:
        entry = store.get(args.id)
    except (ValidationError, FrontmatterError) as exc:
        print(f"error: {args.id} cannot be read: {exc}", file=sys.stderr)
        return 1
    if entry is None:
        print(f"error: no such memory: {args.id}", file=sys.stderr)
        return 1

    against: Optional[MemoryEntry] = None
    if args.against:
        try:
            against = store.get(args.against)
        except (ValidationError, FrontmatterError) as exc:
            print(f"error: {args.against} cannot be read: {exc}", file=sys.stderr)
            return 1
        if against is None:
            print(f"error: no such memory: {args.against}", file=sys.stderr)
            return 1
    else:
        against = _counterpart(store, entry)

    def describe(label: str, item: MemoryEntry) -> None:
        print(f"{label:<9}{item.id}")
        print(f"{'':<9}{item.status}  {item.category}  trust={item.trust}  "
              f"updated {item.updated_at}")
        print(f"{'':<9}source {item.source_adapter} / {item.source_kind} / "
              f"{item.source_path or '(none)'}")
        if item.tags:
            print(f"{'':<9}tags   {', '.join(item.tags)}")

    describe("proposal", entry)
    if against is None:
        print()
        print("no approved entry from the same source yet -- nothing to compare against")
        print()
        print(entry.body, end="")
        return 0

    print()
    describe("against", against)
    print()
    diff = difflib.unified_diff(
        against.body.splitlines(),
        entry.body.splitlines(),
        fromfile=f"{against.id} ({against.status})",
        tofile=f"{entry.id} ({entry.status})",
        lineterm="",
        n=3,
    )
    lines = list(diff)
    if not lines:
        print("bodies are identical; only metadata differs")
        return 0
    for line in lines:
        print(line)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve one entry, or a filtered batch (P4-02)."""
    store = _store(args)
    if not args.all:
        if not args.id:
            print("error: give an id, or pass --all to approve a batch", file=sys.stderr)
            return 2
        return cmd_set_status(args, "approved")

    entries, errors = store.scan()
    _unreadable_hint(errors)
    targets = [entry for entry in entries if entry.status == "pending"]
    if args.category:
        targets = [entry for entry in targets if entry.category == args.category]
    if args.tag:
        targets = [entry for entry in targets if args.tag in entry.tags]

    blocked = [entry for entry in targets if entry.trust == "blocked"]
    approvable = [entry for entry in targets if entry.trust != "blocked"]

    if args.dry_run:
        for entry in approvable:
            print(f"would approve  {entry.id}  {entry.category:<10} {entry.body.splitlines()[0][:48]}")
        print(f"\n{len(approvable)} would be approved, {len(blocked)} blocked")
        return 0

    approved = 0
    for entry in approvable:
        store.set_status(entry.id, "approved")
        approved += 1
        print(f"approved  {entry.id}  {entry.category:<10} {entry.body.splitlines()[0][:48]}")
    if blocked:
        print(
            f"\n{len(blocked)} blocked entry(ies) skipped: lower the sensitivity first",
            file=sys.stderr,
        )
    print(f"\n{approved} approved, {len(blocked)} blocked")
    return 0


def _one_line(text: str, limit: int = 60) -> str:
    head = " ".join(text.split())
    return head if len(head) <= limit else head[: limit - 3] + "..."


def _relation_label(record: dict) -> str:
    relation = str(record.get("relation") or "?")
    return RELATION_LABELS.get(relation, relation)


def _print_record(store: VaultStore, record: dict, *, detail: bool = False) -> None:
    first, second = (str(part) for part in (record.get("pair") or ["?", "?"])[:2])
    evidence = record.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    advice = ""
    if record.get("verified") is False:
        advice = "  [证据未核实，模型原话在原文里定位不到]"
    print(f"{_relation_label(record)}{advice}")
    for entry_id in (first, second):
        entry = store.get(entry_id)
        if entry is None:
            print(f"  {entry_id}  （条目已不存在，提案作废：删掉 inbox/conflicts 下对应文件）")
            continue
        print(f"  {entry_id}  {entry.category:<10} {entry.status:<9} {entry.created_at[:10]}")
        body = entry.body if detail else _one_line(entry.body, 72)
        for line in body.splitlines() or [""]:
            print(f"    {line}")
        quote = evidence.get(entry_id)
        if detail and quote:
            print(f"    证据（逐字）: {quote}")

    claims = record.get("claims")
    if isinstance(claims, list) and claims and detail:
        print("  冲突的片段:")
        for item in claims:
            if not isinstance(item, dict):
                continue
            left = str(item.get("quote_left") or "").strip() or "(无引用)"
            right = str(item.get("quote_right") or "").strip() or "(无引用)"
            mark = "" if item.get("verified") else "  [未核实]"
            print(f"    〈{left}〉")
            print(f"    〈{right}〉  {_relation_label({'relation': item.get('relation')})}{mark}")
            reason = str(item.get("reason") or "").strip()
            if reason:
                print(f"      {reason}")

    print(f"  为什么: {record.get('reason') or ''}")
    relation = str(record.get("relation") or "")
    partial = str(record.get("scope") or "") == "partial"
    if partial and relation == "duplicate":
        print(
            "  范围: 这条记忆里只有部分片段与对方重复，其余内容与对方无关 —— "
            "不需要动整条记忆。"
        )
    elif partial:
        print(
            "  范围: 只有部分片段冲突 —— 这条记忆里其他内容仍然有效，"
            "直接 supersede 会把它们一起藏起来。先拆分更稳。"
        )
    winner = record.get("winner")
    if winner:
        print(f"  建议保留: {winner}")
        lost = second if winner == first else first
        command = f"memoryhub supersede {lost} --by {winner}"
        print(f"  落实: {command}" + ("   ← 需要你确认范围" if partial else ""))
    elif str(record.get("relation") or "") == "duplicate":
        print(
            "  处置: 同一件事记了两遍，没有对错。留着只是检索时各命中一次；"
            "要合并就删掉一条，别用 supersede（那会把旧内容标成历史）。"
        )
    else:
        print("  无人可判胜负，需要你自己决定保留哪条，再发一条新的记忆")
    if detail:
        print(f"  检测于 {record.get('detected_at')}  模型 {record.get('model')}")
    print()


def _record_line(store: VaultStore, record: dict) -> list:
    """Two lines per proposal: what, then why. Bodies belong behind ``--show``.

    With 24 proposals the old version printed every body and scrolled the first
    screen away -- which is how a listing stops being a listing.
    """
    first, second = (str(part) for part in (record.get("pair") or ["?", "?"])[:2])
    marks = []
    if str(record.get("scope") or "") == "partial":
        marks.append("仅部分片段")
    if record.get("verified") is False:
        marks.append("证据未核实")
    head = f"{_relation_label(record):<4} {first}  {second}"
    if marks:
        head += "  [" + " / ".join(marks) + "]"
    reason = " ".join(str(record.get("reason") or "").split())
    if len(reason) > 96:
        reason = reason[:93] + "..."
    return [head, f"     {reason}"]


def cmd_conflicts(args: argparse.Namespace) -> int:
    """Inspect, or generate, conflict proposals (P3-05)."""
    store = _store(args)
    if args.show:
        return _conflicts_show(store, args)
    if args.ai:
        return _conflicts_detect(store, args)
    return _conflicts_list(store)


def _conflicts_list(store: VaultStore) -> int:
    records, problems = conflicts.load_records(store.root)
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if not records:
        print("没有冲突提案。")
        print(
            "hint: memoryhub conflicts --ai 让模型比对已批准条目（内容会离开本机，先确认配置）"
        )
        return 0
    for record in records:
        for line in _record_line(store, record):
            print(line)
    print()
    print(f"{len(records)} 条提案，未写入任何 supersedes。")
    print("看某条的完整正文、逐字证据和该跑的命令：memoryhub conflicts --show <id>")
    return 0


def _conflicts_show(store: VaultStore, args: argparse.Namespace) -> int:
    wanted = args.show
    records, problems = conflicts.load_records(store.root)
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    hits = [record for record in records if wanted in (record.get("pair") or [])]
    if not hits:
        print(f"没有涉及 {wanted} 的提案。")
        return 0
    for record in hits:
        _print_record(store, record, detail=True)
    return 0


def _conflicts_detect(store: VaultStore, args: argparse.Namespace) -> int:
    """Run the model over the approved corpus and write proposals.

    Cost and privacy are both announced before the call: the whole approved
    corpus leaves the machine, and the answer is a suggestion, not a verdict.
    """
    try:
        cfg = AIConfig.from_config()
        if cfg is None:
            print(
                "error: 没有配置 AI，--ai 不可用。\n"
                f"       在 {config.config_path()} 里加 [ai] 的 base_url 与 model（见 memoryhub import --help）",
                file=sys.stderr,
            )
            return 1
        cfg.api_key()
    except AIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    entries, errors = store.scan()
    _unreadable_hint(errors)
    pool = [
        entry
        for entry in entries
        if entry.status == "approved" and not entry.is_expired()
    ]
    if args.category:
        pool = [entry for entry in pool if entry.category == args.category]
    if len(pool) < 2:
        print(f"只有 {len(pool)} 条已批准且未过期的记忆，无法比对。")
        return 0

    print(f"冲突检测：{len(pool)} 条已批准记忆，共 {sum(len(e.body) for e in pool):,} 字")
    print(f"  端点  {cfg.base_url}")
    print(f"  模型  {cfg.model}")
    print("  产出  只是提案，不会写入 supersedes。")
    print()

    def planned(stats, count: int) -> bool:
        """Everything the run costs and everything it will not look at."""
        for line in stats.lines():
            print(f"  {line}")
        if stats.over_limit or stats.crowded_out:
            print(
                "  注意  被让位/超限的段对**没有送审**，其中的矛盾不会被报告。"
                "\n        调大 --max-pairs 可以扩大范围（每多一段对多一次调用）。"
            )
        print()
        print(f"  即将发送 {count} 段对，每对单独一次调用。正文会离开本机，发送前不做脱敏。")
        if getattr(args, "yes", False):
            return True
        if not sys.stdin.isatty():
            print(
                "error: 需要确认。非交互环境下加 --yes，表示已知悉内容会被发送到该端点。",
                file=sys.stderr,
            )
            return False
        return input("继续？[y/N] ").strip().lower() in ("y", "yes")

    total = {"count": 0}

    def progress(done: int, count: int, verdict) -> None:
        total["count"] = count
        mark = "." if verdict is None or verdict.relation == "none" else "!"
        sys.stdout.write(mark)
        if done % 50 == 0 or done == count:
            sys.stdout.write(f"  {done}/{count}\n")
        sys.stdout.flush()

    provider = Provider(cfg)
    try:
        result = detect(
            pool,
            provider,
            max_pairs=args.max_pairs,
            per_entry_pair=args.per_pair,
            on_progress=progress,
            on_planned=planned,
        )
    except AIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not result.stats.selected:
        print("\n没有可比较的段对（条目太少，或彼此毫无共同用词）。")
        return 0
    if result.calls == 0:
        for problem in result.problems:
            print(f"error: {problem}", file=sys.stderr)
        print("没有发送任何内容。")
        return 1

    print()
    print(
        f"{result.calls} 次调用，{result.chars_sent:,} 字发送 → "
        f"{len(result.verdicts) - sum(1 for v in result.verdicts if v.relation == 'none')} 段对判为有关系，"
        f"合并成 {len(result.pairs)} 个条目对"
    )
    if not result.pairs:
        print(
            "模型没有报告冲突。这不等于库里没有矛盾——它只看了上面列出的那些段对。"
        )
    for problem in result.problems[:20]:
        print(f"  ! {problem}")
    if len(result.problems) > 20:
        print(f"  ... 其余 {len(result.problems) - 20} 条省略")
    if result.pairs:
        print()
    for pair in result.pairs:
        record = pair.to_record(model=cfg.model)
        tag = "" if pair.verified else "  [证据未核实]"
        if pair.scope == "partial":
            tag += "  [只涉及部分片段：别整条归档]"
        if getattr(args, "dry_run", False):
            print(f"would write  {conflicts.record_path(store.root, pair.a, pair.b).name}  "
                  f"{_relation_label(record)}{tag}")
            continue
        path = conflicts.save_record(store.root, record)
        print(f"wrote  {path.name}  {_relation_label(record)}{tag}")

    if getattr(args, "dry_run", False):
        print("\ndry run：没有写任何文件（模型调用已经发生，钱已经花了）")
    elif result.pairs:
        print(f"\n{len(result.pairs)} 条提案在 inbox/conflicts/，用 `memoryhub conflicts` 看。")
    return 0


def cmd_supersede(args: argparse.Namespace) -> int:
    """Retire one memory in favour of another. The only write in this workflow."""
    store = _store(args)
    old = store.get(args.old_id)
    new = store.get(args.by)
    if old is None:
        print(f"error: 找不到被取代条目 {args.old_id}", file=sys.stderr)
        return 1
    if new is None:
        print(f"error: 找不到取代方条目 {args.by}", file=sys.stderr)
        return 1

    print(f"被取代  {old.id}  {old.category}  {old.status}")
    for line in old.body.splitlines():
        print(f"        {line}")
    if looks_mixed(old):
        print(
            f"  注意  这条正文 {len(old.body)} 字，可能不止一件事。归档后其他 AI "
            "就查不到它剩下的内容了——先把还有效的部分拆成新记忆，再取代更稳。"
        )
    print(f"取代方  {new.id}  {new.category}  {new.status}")
    print(f"        {_one_line(new.body, 100)}")
    print()

    if args.dry_run:
        print("dry run：没有写任何文件。")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "error: 归档是不可逆的状态变更，非交互环境需要 --yes。",
                file=sys.stderr,
            )
            return 1
        if input("确认归档？[y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消，什么都没写。")
            return 1

    try:
        retired, keeper = conflicts.apply_supersede(
            store, old.id, new.id, note=args.note or ""
        )
    except (KeyError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    moved = conflicts.resolve_record(
        store.root,
        old.id,
        new.id,
        keep=keeper.id,
        retired=retired.id,
        note=args.note or "",
    )
    print(f"archived  {retired.id}  valid_until={retired.valid_until}  "
          f"superseded_by={retired.superseded_by}")
    print(f"supersedes {keeper.id} → {', '.join(keeper.supersedes)}")
    if moved:
        print(f"提案已归档到 {moved.relative_to(store.root)}")
    print("\n从现在起，检索与 MCP 只会返回后者；前者仍可用 --include-expired 查到。")
    return 0



    store = _store(args)
    # Refusing here is the point: without a terminal the loop would read EOF,
    # decide nothing, and look like it worked. Scripts have `approve --all`.
    if not sys.stdin.isatty():
        print("error: `review` needs a terminal (stdin is not a TTY)", file=sys.stderr)
        print(
            "hint: for scripts use `memoryhub list --status pending` then `memoryhub approve <id>`",
            file=sys.stderr,
        )
        return 2
    outcome = review.run(
        store,
        sys.stdin,
        sys.stdout,
        sys.stderr,
        limit=args.limit,
        reset_skips=args.reset,
    )
    return 1 if outcome.errors else 0


def cmd_review(args: argparse.Namespace) -> int:
    """Review pending proposals one screen at a time (P4-05)."""
    store = _store(args)
    # Refusing here is the point: without a terminal the loop would read EOF,
    # decide nothing, and look like it worked. Scripts have `approve --all`.
    if not sys.stdin.isatty():
        print("error: `review` needs a terminal (stdin is not a TTY)", file=sys.stderr)
        print(
            "hint: for scripts use `memoryhub list --status pending` then `memoryhub approve <id>`",
            file=sys.stderr,
        )
        return 2
    outcome = review.run(
        store,
        sys.stdin,
        sys.stdout,
        sys.stderr,
        limit=args.limit,
        reset_skips=args.reset,
    )
    return 1 if outcome.errors else 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Project the vault into each tool's managed block (P5-06 / ADR-019).

    Default is a plan. Writing needs ``--write`` because these are files other
    tools load on every session -- a bad block travels further than a bad
    command, and there is no undo other than the backup.
    """
    targets = sync.default_targets()
    if args.to and args.to != "all":
        wanted = [part.strip() for part in args.to.split(",") if part.strip()]
        known = {target.key for target in targets}
        unknown = [key for key in wanted if key not in known]
        if unknown:
            print(
                f"error: 未知目标 {', '.join(unknown)}；可选 "
                f"{', '.join(sorted(known))}（或 all）",
                file=sys.stderr,
            )
            return 1
        targets = [target for target in targets if target.key in wanted]

    if args.list:
        # Before opening the vault: "--list" answers "where would this go",
        # which is exactly the question someone asks when the vault is missing.
        for target in targets:
            path, matches = target.resolve()
            state = "存在" if path else "缺失"
            suffix = ""
            if len(matches) > 1:
                suffix = f"（{len(matches)} 个候选，用最新的）"
            print(f"{target.key:<10}{target.label:<18}{state}  {path or target.pattern}{suffix}")
        return 0

    entries, errors = _store(args).scan()
    _unreadable_hint(errors)

    live = sync.select(entries)
    plans = sync.plan(entries, targets)
    block_size = len(plans[0].block) if plans else 0

    print(f"同步 {len(live)} 条有效记忆，每份区块约 {block_size:,} 字 → {len(plans)} 个目标")
    print()
    if block_size > sync.WARN_BLOCK_CHARS:
        print(
            f"  注意  区块 {block_size:,} 字，超过建议的 {sync.WARN_BLOCK_CHARS:,} 字。"
            "它会出现在每一次会话的上下文里。"
        )

    pending = []
    for item in plans:
        head = f"{item.target.key:<10}{item.target.label:<18}"
        if item.path is None and not item.changes:
            print(f"{head}跳过  {item.note}")
            continue
        if not item.changes:
            print(f"{head}已是最新  {item.path}")
            continue
        pending.append(item)
        if not item.exists:
            print(f"{head}将新建  {item.path}")
        else:
            added = len(item.after.splitlines()) - len(item.before.splitlines())
            print(f"{head}将写入  {item.path}  （区块 {added:+d} 行）")
        if args.diff:
            print(item.diff)

    if not pending:
        print("\n全部已是最新，无需写入。")
        return 0
    if not args.write:
        print("\ndry run：没有写任何文件。确认后加 --write。")
        return 0

    failed = 0
    print()
    for item in pending:
        try:
            path, saved = sync.write(item)
        except OSError as exc:
            print(f"error: 写入 {item.path} 失败：{exc}", file=sys.stderr)
            failed += 1
            continue
        if not sync.verify(path, item.block):
            print(f"warning: {path} 已写入，但回读时没找到完整区块", file=sys.stderr)
            failed += 1
            continue
        tail = f"  备份 {saved.name}" if saved else "  （新建，无备份）"
        print(f"wrote  {path}{tail}")

    if failed:
        print(f"\n{failed} 个目标有问题。原始文件都在备份里，没被删除。", file=sys.stderr)
    else:
        print("\n下次 import 会跳过这些区块，不会把它们当成新记忆采回来。")
    return 1 if failed else 0


def cmd_log(args: argparse.Namespace) -> int:
    """Summarise what changed in the vault, from its own git history (P4-03)."""
    store = _store(args)
    try:
        commits = collect(store.root, since=args.since, limit=args.limit)
    except HistoryUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "not a git repository" in str(exc):
            print("hint: `git -C <vault> init` then commit, or use `memoryhub backup` once it exists", file=sys.stderr)
        return 1

    if not commits:
        scope = f" since {args.since}" if args.since else ""
        print(f"no commits{scope}")
        return 0

    for commit in commits:
        print(f"{commit.date[:10]}  {commit.digest}  {commit.subject}")
        for change in commit.changes:
            if not change.entry_id:
                continue
            where = f"  {change.category:<10} " if change.category else ""
            print(f"          {change.label:<8}{where}{change.entry_id}")
        non_memory = sum(1 for change in commit.changes if not change.entry_id)
        if non_memory:
            print(f"          ({non_memory} non-memory file change(s) not listed)")
        print()

    stats = summarise(commits)
    scope = f" since {args.since}" if args.since else ""
    print(
        f"{stats['commits']} commit(s){scope}: "
        f"{stats['added']} added, {stats['updated']} updated, {stats['removed']} removed "
        f"({stats['entries']} distinct entries)"
    )
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    """Change how sensitive an entry is, re-deriving its trust tier (ADR-003).

    This is the exit from ``blocked``: an imported credit card number can be marked
    ``secret`` first and released later. It is also the only way to *raise*
    sensitivity after import, which matters because adapters cannot know what is
    personal -- they only know what a file says.
    """
    store = _store(args)
    try:
        entry = store.get(args.id)
    except (ValidationError, FrontmatterError) as exc:
        print(f"error: {args.id} cannot be read: {exc}", file=sys.stderr)
        return 1
    if entry is None:
        print(f"error: no such memory: {args.id}", file=sys.stderr)
        return 1

    previous = (entry.sensitivity, entry.trust, entry.status)
    entry.sensitivity = args.level
    entry.trust = entry.infer_trust()
    if entry.trust == "blocked" and entry.status == "approved":
        print(
            f"error: {entry.id} is approved; raising sensitivity to {args.level!r} "
            "would leave an approved-but-blocked entry. Reject or archive it first "
            f"(`memoryhub reject {entry.id}`), then set the level and re-approve.",
            file=sys.stderr,
        )
        return 1
    entry.updated_at = now_iso()
    store.save(entry)
    print(
        f"{entry.id}  sensitivity {previous[0]} -> {entry.sensitivity}  "
        f"trust {previous[1]} -> {entry.trust}  (was {previous[2]})"
    )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Find entries whose sensitivity looks wrong, optionally raise it (P5-03).

    Adapters cannot know what is private -- they only know what a file says. This
    re-reads every body through the same classifier the proposal path uses, so an
    imported address cannot stay ``normal`` just because nobody looked.
    """
    store = _store(args)
    entries, errors = store.scan()
    _unreadable_hint(errors)

    order = {"normal": 0, "personal": 1, "sensitive": 2, "secret": 3}
    findings_list = []
    for entry in entries:
        level, flags = classify(entry.body)
        if order[level] > order[entry.sensitivity]:
            findings_list.append((entry, level, flags))

    for entry, level, flags in findings_list:
        print(f"{entry.id}  {entry.sensitivity} -> {level}  "
              f"[{', '.join(flags)}]  {entry.status}")
    if not findings_list:
        print(f"no unmarked sensitive content in {len(entries)} entries")
        return 0

    print(f"\n{len(findings_list)} of {len(entries)} entries look under-classified")
    if not args.apply:
        print("run with --apply to raise them (this may move entries to blocked)")
        return 0

    changed = 0
    for entry, level, flags in findings_list:
        if entry.status == "approved" and level in ("sensitive", "secret"):
            print(f"skip {entry.id}: approved; reject or archive it first", file=sys.stderr)
            continue
        entry.sensitivity = level
        entry.trust = entry.infer_trust()
        entry.updated_at = now_iso()
        store.save(entry)
        changed += 1
    print(f"{changed} entries re-classified")
    return 0


def cmd_recategorize(args: argparse.Namespace) -> int:
    """Suggest a category from content, show the diff, write only with --apply.

    Adapters file an entry by where it came from (a persona file is a profile).
    That is a statement about the source, not about the sentence. This re-reads
    each body through the rule table in :mod:`memoryhub.classify` and reports
    what it would change -- dry by default, because a silent rewrite of the
    user's own filing decision is exactly the kind of surprise ADR-009 bans.
    """
    store = _store(args)
    entries, errors = store.scan()
    _unreadable_hint(errors)

    considered = 0
    unmatched = 0
    changes = []
    for entry in entries:
        if args.status and entry.status != args.status:
            continue
        if args.adapter and entry.source_adapter != args.adapter:
            continue
        considered += 1
        suggested, reason = classify_category(entry.body, default=entry.category)
        if reason.startswith("no rule"):
            unmatched += 1
        if suggested != entry.category:
            changes.append((entry, suggested, reason))

    for entry, suggested, reason in changes:
        head = " ".join(entry.body.split())[:56]
        print(f"{entry.id[:24]}  {entry.category:<10} -> {suggested:<10}  {head}")
        print(f"{'':<24}  {reason}")

    if not changes:
        print(f"no category changes among {considered} entries")
        if unmatched:
            print(
                f"{unmatched} of them matched no rule and kept their import category; "
                "file those by hand"
            )
        return 0

    counts: dict = {}
    for entry, suggested, _reason in changes:
        counts[f"{entry.category}->{suggested}"] = counts.get(f"{entry.category}->{suggested}", 0) + 1
    print()
    print(f"{len(changes)} of {considered} entries would change: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if unmatched:
        print(f"({unmatched} entries matched no rule and keep their import category)")
    if not args.apply:
        print("dry run; add --apply to write these categories")
        return 0

    written = 0
    for entry, suggested, _reason in changes:
        entry.category = suggested
        entry.updated_at = now_iso()
        store.save(entry)
        written += 1
    print(f"{written} entries re-filed")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Commit the vault, and optionally push it to a remote (P5-04).

    The commit always happens: a local commit is offline and reversible. The push
    is gated on content, because once it leaves the machine it cannot be recalled.
    """
    store = _store(args)
    try:
        created = ensure_repo(store.root)
        if write_gitignore(store.root):
            print("gitignore updated (database/, logs/, temp files are now excluded)")
        if created:
            print(f"initialised git repository in {store.root}")

        sha = git_commit(store.root, args.message)
        if sha is None:
            print("nothing to commit")
        else:
            print(f"committed {sha}")
    except GitUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.remote:
        return 0

    flagged = sensitive_entries(store)
    if flagged and not args.allow_sensitive:
        print(
            f"\nrefusing to push: {len(flagged)} entr(ies) are sensitive or blocked",
            file=sys.stderr,
        )
        for entry in flagged:
            print(f"  {entry.id}  {entry.sensitivity}  {entry.status}", file=sys.stderr)
        print(
            "\noptions: run `memoryhub audit` to classify, `memoryhub sensitivity <id> "
            "--level normal` to release one, or pass --allow-sensitive to push anyway",
            file=sys.stderr,
        )
        return 1

    try:
        ok, detail = git_push(store.root, args.remote)
    except GitUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print(f"error: push failed: {detail}", file=sys.stderr)
        print("hint: the local commit is safe; remote failure is retryable", file=sys.stderr)
        return 1
    print(detail)
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
    hits: List[MemoryEntry] = []
    index = Index(store.root)
    try:
        index.ensure_fresh(store)
        entries = (store.get(entry_id) for entry_id in index.search_ids(args.query))
        hits = [entry for entry in entries if entry is not None]
        if not args.include_expired:
            hits = [entry for entry in hits if not entry.is_expired()]
    except IndexUnavailable as exc:
        # The index is a cache; never let it take search down with it.
        print(f"warning: index unavailable ({exc}); falling back to a full scan", file=sys.stderr)
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
        epilog=(
            "主线七条：init 建库 · import 导入（--ai 交给模型整理）· review 逐条审核 · "
            "approve 按 id/批量通过 · sync 回写给各 AI · mcp 对外服务 · doctor 体检。\n"
            "其余命令不占主屏，仍然可用：memoryhub <命令> --help。\n"
            "回写只在 sync 里发生，且只动受管区块（区块外一个字节不碰）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"memoryhub {__version__}")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        # Keep the usage line in step with the six-command table below; the
        # default metavar would enumerate all 20 verbs and undo the whole point.
        metavar="{init,import,approve,review,sync,mcp,doctor}",
    )

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
        "import",
        aliases=["scan"],
        parents=[vault_arg],
        help="collect from sources into inbox; --ai lets your model split and categorise",
    )
    p_scan.add_argument(
        "--adapter",
        choices=sorted(ADAPTERS),
        default=None,
        help="only run one adapter (default: all)",
    )
    p_scan.add_argument(
        "--ai",
        action="store_true",
        help="use your own model to split, categorise and summarise. Needs [ai] in "
        "config. Content is sent to that endpoint WITHOUT local redaction.",
    )
    p_scan.add_argument(
        "--yes",
        action="store_true",
        help="with --ai: skip the confirmation prompt (for scripts)",
    )
    p_scan.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing anything",
    )
    p_scan.add_argument(
        "--refresh",
        action="store_true",
        help="archive pending entries a source no longer produces (after a "
        "splitting change), instead of leaving two generations in the queue",
    )
    p_scan.set_defaults(func=cmd_scan)

    p_sens = sub.add_parser(
        "sensitivity",
        parents=[vault_arg],
        help="change an entry's sensitivity level (re-derives trust)",
    )
    p_sens.add_argument("id")
    p_sens.add_argument(
        "--level", required=True, choices=("normal", "personal", "sensitive", "secret")
    )
    p_sens.set_defaults(func=cmd_sensitivity)

    p_backup = sub.add_parser(
        "backup",
        parents=[vault_arg],
        help="commit the vault; optionally push it to a remote",
    )
    p_backup.add_argument("--message", default="memoryhub: backup", help="commit message")
    p_backup.add_argument(
        "--remote", default=None, help="git remote to push to (omit for a local commit only)"
    )
    p_backup.add_argument(
        "--allow-sensitive",
        action="store_true",
        help="push even though sensitive/blocked entries exist",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_audit = sub.add_parser(
        "audit",
        parents=[vault_arg],
        help="find entries whose sensitivity looks too low (P5-03)",
    )
    p_audit.add_argument("--apply", action="store_true", help="raise the levels")
    p_audit.set_defaults(func=cmd_audit)

    p_review = sub.add_parser(
        "recategorize",
        parents=[vault_arg],
        help="suggest categories from content; dry run unless --apply",
    )
    p_review.add_argument("--apply", action="store_true", help="write the changes")
    p_review.add_argument(
        "--status", choices=STATUSES, default=None, help="only entries in this status"
    )
    p_review.add_argument(
        "--adapter", default=None, help="only entries from this adapter"
    )
    p_review.set_defaults(func=cmd_recategorize)

    p_log = sub.add_parser(
        "log", parents=[vault_arg], help="summarise recent changes from the vault's git history"
    )
    p_log.add_argument("--since", default=None, help="window: 7d / 12h / 2w / an ISO date")
    p_log.add_argument("--limit", type=int, default=50, help="max commits to read")
    p_log.set_defaults(func=cmd_log)

    p_index = sub.add_parser(
        "index", parents=[vault_arg], help="build or report the SQLite index"
    )
    p_index.add_argument("--rebuild", action="store_true", help="force a full rebuild")
    p_index.set_defaults(func=cmd_index)

    p_watch = sub.add_parser(
        "watch", parents=[vault_arg], help="poll sources and import changes"
    )
    p_watch.add_argument("--interval", type=float, default=2.0, help="poll interval (s)")
    p_watch.add_argument(
        "--quiet", type=float, default=3.0, help="wait this long after a change (s)"
    )
    p_watch.add_argument(
        "--once", action="store_true", help="single pass with no debounce, for tests/cron"
    )
    p_watch.set_defaults(func=cmd_watch)

    p_mcp = sub.add_parser(
        "mcp",
        parents=[vault_arg],
        help="serve the MCP tools over stdio (for Cursor / Claude Desktop)",
    )
    p_mcp.add_argument(
        "--permission",
        choices=("read", "propose", "admin"),
        default=os.environ.get("MEMORYHUB_MCP_PERMISSION", "propose"),
        help="capability tier: read (3 tools) / propose (+memory_propose) / "
        "admin (+memory_status). Default: propose, or $MEMORYHUB_MCP_PERMISSION",
    )
    p_mcp.set_defaults(func=cmd_mcp)

    p_sync = sub.add_parser(
        "sync",
        parents=[vault_arg],
        help="project the vault into each tool's managed block (P5-06)",
    )
    p_sync.add_argument(
        "--to",
        default="all",
        help="all, or a comma-separated subset of claude,workbuddy,codex",
    )
    p_sync.add_argument(
        "--write", action="store_true", help="write for real (default is a plan only)"
    )
    p_sync.add_argument("--diff", action="store_true", help="print the unified diff")
    p_sync.add_argument("--list", action="store_true", help="only list the target paths")
    p_sync.set_defaults(func=cmd_sync)

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

    p_diff = sub.add_parser(
        "diff", parents=[vault_arg], help="compare a proposal with the entry it would replace"
    )
    p_diff.add_argument("id")
    p_diff.add_argument("--against", default=None, help="compare with this id instead")
    p_diff.set_defaults(func=cmd_diff)

    p_ok = sub.add_parser(
        "approve", parents=[vault_arg], help="approve one proposal, or a filtered batch"
    )
    p_ok.add_argument("id", nargs="?", default=None)
    p_ok.add_argument("--all", action="store_true", help="approve every matching pending entry")
    p_ok.add_argument("--category", choices=CATEGORIES, default=None)
    p_ok.add_argument("--tag", default=None, help="only entries carrying this tag")
    p_ok.add_argument("--dry-run", action="store_true", help="list what would be approved")
    p_ok.set_defaults(func=cmd_approve)

    p_review = sub.add_parser(
        "review",
        parents=[vault_arg],
        help="review pending proposals one screen at a time",
        description=(
            "Walk the pending queue one entry per screen. Keys: a approve, "
            "r reject, c change category, s skip (ask again next run), q quit. "
            "Progress is resumable: decided entries leave the queue by changing "
            "status, and skips are remembered in the config directory."
        ),
    )
    p_review.add_argument("--limit", type=int, default=None, help="stop after N entries")
    p_review.add_argument(
        "--reset", action="store_true", help="forget previously skipped entries"
    )
    p_review.set_defaults(func=cmd_review)

    p_no = sub.add_parser(
        "reject", parents=[vault_arg], help="reject a pending proposal"
    )
    p_no.add_argument("id")
    p_no.set_defaults(func=lambda a: cmd_set_status(a, "rejected"))

    p_stats = sub.add_parser(
        "stats", parents=[vault_arg], help="show vault statistics"
    )
    p_stats.set_defaults(func=cmd_stats)

    p_conf = sub.add_parser(
        "conflicts",
        parents=[vault_arg],
        help="list conflict proposals; --ai asks the model for new ones (P3-05)",
        description=(
            "Conflict proposals are suggestions, never writes. --ai splits every "
            "approved entry into claims, pairs the ones that share vocabulary, and "
            "asks your own model about each pair separately (one pair per call, so "
            "no unrelated entry can dilute it). Answers land in inbox/conflicts/. "
            "Nothing reaches the vault until you run "
            "`memoryhub supersede <old> --by <new>`. --show <id> prints the "
            "entries behind every proposal mentioning that id."
        ),
    )
    p_conf.add_argument(
        "--ai", action="store_true", help="run the model over the approved entries"
    )
    p_conf.add_argument("--show", metavar="ID", default=None, help="detail for one entry")
    p_conf.add_argument("--category", choices=CATEGORIES, default=None)
    p_conf.add_argument(
        "--max-pairs",
        type=int,
        default=DEFAULT_MAX_PAIRS,
        metavar="N",
        help=f"with --ai: how many claim pairs to send (default {DEFAULT_MAX_PAIRS})",
    )
    p_conf.add_argument(
        "--per-pair",
        type=int,
        default=PER_ENTRY_PAIR,
        metavar="N",
        help="with --ai: claim pairs per entry pair (default "
        f"{PER_ENTRY_PAIR}, keeps one verbose entry from flooding)",
    )
    p_conf.add_argument(
        "--yes", action="store_true", help="with --ai: skip the send confirmation"
    )
    p_conf.add_argument(
        "--dry-run", action="store_true", help="with --ai: call the model, write nothing"
    )
    p_conf.set_defaults(func=cmd_conflicts)

    p_sup = sub.add_parser(
        "supersede",
        parents=[vault_arg],
        help="archive one memory in favour of another (ADR-006)",
        description=(
            "Write the supersede chain: the old entry gets valid_until=now and "
            "status=archived, the new one gets it in `supersedes`. Queries and "
            "MCP stop returning the old one; --include-expired still shows it."
        ),
    )
    p_sup.add_argument("old_id", help="the entry that stops being true")
    p_sup.add_argument("--by", required=True, dest="by", help="the entry that replaces it")
    p_sup.add_argument("--note", default="", help="why, stored on the replacement")
    p_sup.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    p_sup.add_argument("--yes", action="store_true", help="skip the confirmation")
    p_sup.set_defaults(func=cmd_supersede)

    #: Six commands carry the product; the rest are reachable but off the first
    #: screen. 20 verbs in `--help` is a large part of why the tool reads as
    #: complicated -- see docs/04-classify-audit.md for what the code actually is.
    #: ``_choices_actions`` is the table argparse renders for subcommands; entries
    #: are removed in place (not help=SUPPRESS, which prints the literal word).
    mainline = {"init", "import", "scan", "approve", "review", "sync", "mcp", "doctor"}
    sub._choices_actions[:] = [
        action for action in sub._choices_actions if action.dest in mainline
    ]

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
