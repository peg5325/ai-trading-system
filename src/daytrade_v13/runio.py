"""Safety steps shared by the v1.3 scripts (same pattern as scripts/daytrade_v11_precount.py).

git state is read before anything else; a dirty tracked tree is refused unless allowed; the fixed v1.1 mask must hash
to V11_MASK_SHA256 and be reproduced byte for byte from the same DB, events and CSV snapshots; outputs are
write-once; the manifest carries every input hash (v1.1 PREREG §6.5).
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path

from src.daytrade_paper.external import load_events
from src.daytrade_v11.data import (
    ROW_HASH_FIELDS,
    SEGMENTS,
    boundary_check,
    build_mask,
    check_segment_facts,
    load_segment,
    mask_bytes,
    sha256_file,
)
from src.history.exchange_calendar import artifact_summary

ROOT = Path(__file__).resolve().parents[2]
V11_MASK_SHA256 = "84d22f461abff1a7e9ef3e4c80d337f5ef12667ea2bef66eb36abcd18404fac2"  # PREREG v1.3 §9
EVENTS_HISTORY = ROOT / "config" / "daytrade_events_history_v1.yaml"
EVENTS_LIVE = ROOT / "config" / "daytrade_events_v1.yaml"


class Refused(Exception):
    pass


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_state() -> dict:
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"commit": _git("rev-parse", "HEAD"), "dirty_tracked": None if status is None else bool(status),
            "dirty_files": status.splitlines() if status else []}


def refuse_dirty(git: dict, allow_dirty: bool) -> None:
    if git["dirty_tracked"] is not False and not allow_dirty:
        raise Refused(f"tracked files are dirty or git state unknown ({git['dirty_files']}); "
                      "commit first or pass --allow-dirty")


def refuse_existing(*paths: Path) -> None:
    for p in paths:
        if p.exists():
            raise Refused(f"{p} already exists; outputs are write-once")


def load_fixed_mask(mask_path: Path, expected_sha: str, db: Path, events: Path, ext: dict) -> tuple[dict, list, dict]:
    """Refuse unless the mask file hashes to `expected_sha` and rebuilding it reproduces the same bytes."""
    raw = Path(mask_path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != expected_sha:
        raise Refused(f"mask SHA-256 {sha} != expected {expected_sha}")
    cal = load_events(events)
    events_file = events.relative_to(ROOT) if events.is_relative_to(ROOT) else events
    sources = {"events_file": str(events_file), "events_sha256": sha256_file(events),
               "exchange_calendar_sha256": hashlib.sha256(
                   json.dumps(artifact_summary(), sort_keys=True).encode()).hexdigest(),
               "boundary_check": boundary_check(db)}
    datas = [load_segment(db, seg) for seg in SEGMENTS]
    mask = build_mask(datas, cal, sources, ext)
    check_segment_facts(mask)
    if mask_bytes(mask) != raw:
        raise Refused("rebuilding the mask from the current DB, events, CSV snapshots and code does not "
                      "reproduce the fixed mask file")
    return json.loads(raw), datas, cal


def refuse_missing_vix(mask: dict, allow: bool) -> None:
    missing = {name: {k: av["non_ok_count"] for k, av in seg["external_availability"].items()}
               for name, seg in mask["segments"].items()}
    if any(n for seg in missing.values() for n in seg.values()) and not allow:
        raise Refused(f"scored days without an ok VIX/VIX9D prior value {missing}; pass --allow-missing-vix "
                      "to run them as UNKNOWN_TREATED_AS_HIGH")


def manifest(git: dict, args, mask: dict, mask_sha: str, ext: dict, extra: dict) -> dict:
    import numpy

    return {
        "prereg": "docs/research/PREREG_DAYTRADE_V1_3_SIZING.md",
        "borrowed_framework": "docs/research/PREREG_DAYTRADE_V1_1_INTRADAY_REVERSAL.md §6",
        "git_commit": git["commit"], "git_dirty_tracked": git["dirty_tracked"], "git_dirty_files": git["dirty_files"],
        "allow_dirty": args.allow_dirty, "allow_missing_vix": args.allow_missing_vix,
        "mask_file": str(args.mask), "mask_sha256": mask_sha,
        "events_sha256": {"history": sha256_file(args.events),
                          "live": sha256_file(EVENTS_LIVE) if EVENTS_LIVE.exists() else None},
        "external_csv_sha256": ext["sha256"], "external_csv_files": ext["files"],
        "db_rows_sha256": {n: s["db_rows_sha256"] for n, s in mask["segments"].items()},
        "db_row_hash_fields": list(ROW_HASH_FIELDS),
        "python": platform.python_version(), "numpy": numpy.__version__,
        **extra,
    }


def write_json_once(path: Path, doc: dict) -> str:
    raw = (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(raw)
    return hashlib.sha256(raw).hexdigest()
