#!/usr/bin/env python3
"""Measure the rule-based classifier against a human-labelled sample.

Why this exists: ``classify.py`` claims to be "the floor" under a future LLM
layer, and ``docs/01-decisions.md`` sets a review trigger at 20% error. Neither
claim was ever measured, so both were opinion. This script is the measurement.

Design notes
------------
* The label file lives in the *public* repo and therefore holds only
  ``id / human_label / error_kind``. Bodies are read from the private vault at
  run time, so running this never copies memory content into git.
* ``fallback`` is detectable without trusting the human's annotation (the reason
  string says "no rule matched"), so the script recomputes it and reports any
  disagreement. A label you cannot check by machine is just a second opinion.
* Exit code 1 when accuracy is below the threshold in ``--min-accuracy``, so
  this can be wired into CI as a regression gate.

Usage::

    python tools/audit_classify.py --vault D:/my-memory
    python tools/audit_classify.py --vault D:/my-memory --min-accuracy 0.85
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from memoryhub import classify as clf  # noqa: E402
from memoryhub.models import CATEGORIES, MemoryEntry  # noqa: E402

FALLBACK_REASONS = ("no rule matched", "body too short for rules")
DEFAULT_LABELS = REPO / "docs" / "audits" / "classify-labels-2026-11-25.tsv"

#: Mirrors ``classify()``'s signature default. Production's ``recategorize``
#: deliberately passes ``entry.category`` instead (a re-run should not move an
#: entry the rules have no opinion about), but an audit cannot do that: once the
#: vault has been reviewed, ``entry.category`` *is* the human label, so the
#: fallback would be graded against its own answer. Fixed constant or nothing.
DEFAULT_CATEGORY = "profile"


def load_labels(path: Path) -> Dict[str, Tuple[str, str]]:
    labels: Dict[str, Tuple[str, str]] = {}
    # utf-8-sig: a spreadsheet export or an Obsidian-adjacent editor may prefix a
    # BOM, and a BOM would silently turn "id" into "\ufeffid" and fail the header check.
    with path.open(encoding="utf-8-sig") as handle:
        header = handle.readline().rstrip("\n").split("\t")
        if header[:3] != ["id", "human_label", "error_kind"]:
            raise SystemExit(f"unexpected header in {path}: {header[:3]}")
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            ident, human, kind = line.split("\t")
            if human not in CATEGORIES:
                raise SystemExit(f"unknown human_label {human!r} for {ident}")
            labels[ident] = (human, kind)
    return labels


def load_entries(vault: Path) -> Dict[str, MemoryEntry]:
    found: Dict[str, MemoryEntry] = {}
    for path in sorted(vault.rglob("*.md")):
        if "/.git/" in path.as_posix():
            continue
        try:
            entry = MemoryEntry.from_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            continue  # not a memory file; keep scanning
        found[entry.id] = entry
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--labels", default=DEFAULT_LABELS, type=Path)
    parser.add_argument("--min-accuracy", type=float, default=0.0)
    parser.add_argument(
        "--show-misses",
        action="store_true",
        help="print one line per disagreement (id + categories + reason, never the body)",
    )
    args = parser.parse_args()

    labels = load_labels(args.labels)
    entries = load_entries(args.vault)

    missing = [ident for ident in labels if ident not in entries]
    if missing:
        print(f"skipped {len(missing)} labelled id(s) not found in vault", file=sys.stderr)

    confusion: Dict[str, Counter] = defaultdict(Counter)
    kinds: Counter = Counter()
    detected_fallback = 0
    fallback_mismatch: List[str] = []
    misses: List[Tuple[str, str, str, str]] = []
    checked = 0

    for ident, (human, kind) in labels.items():
        entry = entries.get(ident)
        if entry is None:
            continue
        # `default` must be the same constant production uses, NOT
        # entry.category. The stored category is this very classifier's earlier
        # output (or, after a review, the human label itself), so feeding it
        # back makes the fallback "succeed" by construction -- the measurement
        # would silently change every time the vault is reviewed.
        rule, reason = clf.classify(entry.body, default=DEFAULT_CATEGORY)
        checked += 1
        confusion[human][rule] += 1

        is_fallback = reason in FALLBACK_REASONS
        detected_fallback += is_fallback
        if is_fallback != kind.startswith("fallback"):
            fallback_mismatch.append(ident)

        if rule == human:
            # A fallback that lands on the right answer is not evidence the rules
            # work, so it does not get counted as a hit.
            kinds["fallback_hit" if is_fallback else "ok"] += 1
        else:
            kinds[kind] += 1
            misses.append((ident, human, rule, reason))

    if not checked:
        print("nothing to check", file=sys.stderr)
        return 2

    correct = kinds["ok"] + kinds["fallback_hit"]
    accuracy = correct / checked
    print(f"sample            {checked}")
    print(f"rule == human      {correct}/{checked}  ({accuracy:.1%})")
    print(f"fallback fired     {detected_fallback}  ({detected_fallback / checked:.1%} of sample)")

    # The headline accuracy is misleading on its own: a fallback fires no rule and
    # returns the caller's default, so it "wins" whenever the default happens to be
    # right. Splitting the two makes the classifier accountable for the part it
    # actually decided, and exposes whether ``default`` is a strategy or a coin flip.
    scored = [
        (ident, human, clf.classify(entries[ident].body, default=DEFAULT_CATEGORY), kind)
              for ident, (human, kind) in labels.items() if ident in entries]
    signalled = [row for row in scored if row[2][1] not in FALLBACK_REASONS]
    silent = [row for row in scored if row[2][1] in FALLBACK_REASONS]
    if signalled:
        hits = sum(1 for row in signalled if row[2][0] == row[1])
        print(f"  with a signal    {hits}/{len(signalled)}  ({hits / len(signalled):.1%})")
    if silent:
        hits = sum(1 for row in silent if row[2][0] == row[1])
        print(f"  no signal        {hits}/{len(silent)}  ({hits / len(silent):.1%})  <- default={silent[0][2][0]}")
    print()
    print("accuracy per human label")
    for human in CATEGORIES:
        rows = [row for row in scored if row[1] == human]
        if not rows:
            continue
        hits = sum(1 for row in rows if row[2][0] == human)
        print(f"  {human:<10} {hits:>2}/{len(rows):<2}  {hits / len(rows):>5.1%}")
    print()
    print("error composition")
    for kind, count in kinds.most_common():
        share = count / checked
        print(f"  {kind:<10} {count:>3}  {share:>5.1%}")

    print()
    print("confusion (rows = human label, cols = rule's vote)")
    width = max(len(name) for name in CATEGORIES) + 2
    print(" " * width + "".join(f"{name[:9]:>10}" for name in CATEGORIES))
    for human in CATEGORIES:
        row = confusion.get(human, Counter())
        if not row:
            continue
        cells = "".join(f"{row.get(name, 0) or '':>10}" for name in CATEGORIES)
        print(f"{human:<{width}}" + cells)

    if fallback_mismatch:
        print()
        print(f"note: {len(fallback_mismatch)} label(s) marked fallback disagree with the reason string")
        for ident in fallback_mismatch:
            print(f"  {ident}")

    if args.show_misses:
        print()
        print("misses (human -> rule | reason)")
        for ident, human, rule, reason in misses:
            print(f"  {ident}  {human} <- {rule}  |  {reason}")

    if accuracy < args.min_accuracy:
        print()
        print(f"FAIL: {accuracy:.1%} < required {args.min_accuracy:.1%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
