#!/usr/bin/env python3
"""Move gitignored outputs from the pre-2026-10-14 layout into the layout of docs/OUTPUT_LAYOUT.md.

  python scripts/migrate_output_layout.py --root <checkout>            # dry run: plan, counts, leftovers
  python scripts/migrate_output_layout.py --root <checkout> --apply    # move, then append reports/MOVES.json

Only repo-root `logs[_<session>]/`, `status[_<session>].json` and top-level `reports/` entries are
considered; data/ and datasets/ are never touched. Rules come from src.output_layout.classify (single
source). A directory whose destination already exists is merged child by child. Any destination that
already exists as a file (or a plan with two sources for one destination) aborts before anything moves.
Moves use os.rename, so the checkout must be one filesystem. Re-running after --apply plans 0 moves.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.output_layout import LAYOUT_REPORT_ENTRIES, classify, moves_ledger, reports_dir

SKIP_NAMES = frozenset({".DS_Store"})


@dataclass
class Plan:
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    merged_dirs: list[Path] = field(default_factory=list)   # sources merged child by child
    conflicts: list[str] = field(default_factory=list)
    unclassified: list[Path] = field(default_factory=list)


def _add(plan: Plan, src: Path, dst: Path) -> None:
    if not os.path.lexists(dst):
        plan.moves.append((src, dst))
        return
    if src.is_dir() and not src.is_symlink() and dst.is_dir() and not dst.is_symlink():
        plan.merged_dirs.append(src)
        for child in sorted(src.iterdir()):
            if child.name not in SKIP_NAMES:
                _add(plan, child, dst / child.name)
        return
    plan.conflicts.append(f"destination exists: {dst} (from {src})")


def build_plan(root: Path) -> Plan:
    root = Path(root)
    plan = Plan()
    for entry in sorted(root.iterdir()):
        if entry.name in SKIP_NAMES:
            continue
        new = classify(entry.name, entry.is_dir())
        if new is not None:
            _add(plan, entry, root / new)
    reports = reports_dir(root)
    if reports.is_dir():
        for entry in sorted(reports.iterdir()):
            if entry.name in SKIP_NAMES or entry.name in LAYOUT_REPORT_ENTRIES:
                continue
            new = classify(f"reports/{entry.name}", entry.is_dir())
            if new is None:
                plan.unclassified.append(entry)
            else:
                _add(plan, entry, root / new)
    seen: dict[Path, Path] = {}
    for src, dst in plan.moves:
        if dst in seen:
            plan.conflicts.append(f"two sources for {dst}: {seen[dst]} and {src}")
        seen[dst] = src
    return plan


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_ledger(root: Path) -> list:
    path = moves_ledger(root)
    if not path.exists():
        return []
    prior = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(prior, list):
        raise TypeError(f"{path} must hold a JSON list")
    return prior


def _append_ledger(root: Path, prior: list, entries: list[dict]) -> None:
    """Append-only: earlier entries are kept unchanged and in order; the file is replaced atomically."""
    path = moves_ledger(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(prior + entries, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def apply_plan(root: Path, plan: Plan, now: datetime | None = None) -> list[dict]:
    if plan.conflicts:
        raise RuntimeError("refusing to move: " + "; ".join(plan.conflicts))
    prior = _read_ledger(root)   # an unreadable ledger stops the run before anything moves
    moved_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    done: list[dict] = []
    try:
        for src, dst in plan.moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(dst):   # appeared after planning
                raise FileExistsError(f"destination exists: {dst}")
            os.rename(src, dst)
            done.append({"old": _rel(root, src), "new": _rel(root, dst), "moved_at_utc": moved_at})
    finally:
        if done:
            _append_ledger(root, prior, done)
    for folder in sorted(plan.merged_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if [p.name for p in folder.iterdir()] == [".DS_Store"]:
                (folder / ".DS_Store").unlink()   # Finder metadata alone must not keep an old-name folder
            folder.rmdir()   # only when empty
        except OSError:
            pass
    return done


def _group(root: Path, dst: Path) -> str:
    return _rel(root, dst.parent) + "/"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="repository checkout to migrate")
    ap.add_argument("--apply", action="store_true", help="perform the moves (default: dry run)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    plan = build_plan(root)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"# {mode} root={root}")
    for src, dst in plan.moves:
        print(f"MOVE {_rel(root, src)} -> {_rel(root, dst)}")
    print(f"\n# planned moves: {len(plan.moves)}")
    for group, count in sorted(Counter(_group(root, dst) for _, dst in plan.moves).items()):
        print(f"{count:6d}  {group}")
    print(f"\n# unclassified (left in place): {len(plan.unclassified)}")
    for path in plan.unclassified:
        print(f"UNCLASSIFIED {_rel(root, path)}{'/' if path.is_dir() else ''}")
    if plan.conflicts:
        print(f"\n# conflicts: {len(plan.conflicts)} (nothing moved)")
        for conflict in plan.conflicts:
            print(f"CONFLICT {conflict}")
        return 1
    if args.apply:
        done = apply_plan(root, plan)
        print(f"\n# moved {len(done)}; appended to {_rel(root, moves_ledger(root))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
