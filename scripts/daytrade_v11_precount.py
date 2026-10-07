"""v1.1 pre-run count (PREREG_DAYTRADE_V1_1_INTRADAY_REVERSAL §6.1 mask + §6.2 count). Counts only.

Two separate stages, so counts can never exist before the mask has been shown:

  --mask-only --mask-out M   check segment boundaries (dates only), load segments via research_load, build the
                             scored-date mask, write it (write-once), print its SHA-256 and the scored/excluded
                             summary by reason, VIX/VIX9D availability on scored days, then exit.
  --mask M --mask-sha256 H --out C
                             refuse unless M hashes to H and rebuilding the mask from the same DB, events and
                             CSV snapshots reproduces M byte for byte; refuse if a scored day lacks VIX/VIX9D
                             (unless --allow-missing-vix); count v1.0 morning entries and whether they are still
                             open after the 10:30 open stage; write counts + manifest (write-once).

No download, no orders, no P&L. Refuses on a dirty tracked worktree unless --allow-dirty.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_paper.external import load_events
from src.daytrade_v11.data import (
    PREREG_PATH,
    ROW_HASH_FIELDS,
    SEGMENTS,
    MaskRefused,
    boundary_check,
    build_mask,
    check_segment_facts,
    load_external,
    load_segment,
    mask_bytes,
    sha256_file,
    write_mask,
)
from src.daytrade_v11.precount import run_count
from src.history.exchange_calendar import artifact_summary

EVENTS_HISTORY = ROOT / "config" / "daytrade_events_history_v1.yaml"
EVENTS_LIVE = ROOT / "config" / "daytrade_events_v1.yaml"
# PREREG §2 (recorded there; this script does not download the paper)
PAPER = {"sha256": "41d635d162551c4add329d2a11cd9571aea391befd00e472ed81c0070bb207ad",
         "edition": "SFI Research Paper N°24-97, This Version: February 3, 2025", "pages": "6-7 (§3 Noise Area)"}


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


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--vix-csv", type=Path, required=True)
    p.add_argument("--vix9d-csv", type=Path, required=True)
    p.add_argument("--dix-csv", type=Path, default=None, help="GEX (record only)")
    p.add_argument("--events", type=Path, default=EVENTS_HISTORY)
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--mask-only", action="store_true")
    p.add_argument("--mask-out", type=Path, help="mask-only: mask JSON to write (write-once)")
    p.add_argument("--mask", type=Path, help="count: fixed mask JSON")
    p.add_argument("--mask-sha256", help="count: expected SHA-256 of --mask")
    p.add_argument("--out", type=Path, help="count: counts JSON (write-once)")
    p.add_argument("--allow-missing-vix", action="store_true")
    a = p.parse_args(argv)
    if a.mask_only:
        if a.mask_out is None or a.mask or a.mask_sha256 or a.out:
            p.error("--mask-only takes --mask-out only (no --mask/--mask-sha256/--out)")
    elif not (a.mask and a.mask_sha256 and a.out) or a.mask_out:
        p.error("count mode needs --mask, --mask-sha256 and --out (run --mask-only first)")
    return a


def _make_mask(args, ext: dict) -> tuple[dict, list, dict]:
    cal = load_events(args.events)
    events_file = args.events.relative_to(ROOT) if args.events.is_relative_to(ROOT) else args.events
    sources = {"events_file": str(events_file), "events_sha256": sha256_file(args.events),
               "exchange_calendar_sha256": hashlib.sha256(
                   json.dumps(artifact_summary(), sort_keys=True).encode()).hexdigest(),
               "boundary_check": boundary_check(args.db)}
    datas = [load_segment(args.db, seg) for seg in SEGMENTS]
    mask = build_mask(datas, cal, sources, ext)
    check_segment_facts(mask)
    return mask, datas, cal


def _print_mask_summary(mask: dict, sha: str) -> None:
    print(f"mask sha256 {sha}")
    for name, seg in mask["segments"].items():
        print(f"segment {name} {seg['start']}..{seg['end']}: {seg['calendar_sessions']} sessions, "
              f"scored {seg['scored_count']}, excluded {seg['excluded_count']}")
        for kind, n in seg["excluded_by_kind"].items():
            print(f"  excluded {kind}: {n}")
        diag = seg["sigma_undefined_minutes_diagnostic"]
        print(f"  scored days with some sigma(t) undefined (diagnostic): {sum(1 for v in diag.values() if v)}")
        for key, av in (seg["external_availability"] or {}).items():
            print(f"  scored days with {key} not ok: {av['non_ok_count']}")


def run_mask_only(args, ext: dict) -> int:
    if args.mask_out.exists():
        raise Refused(f"{args.mask_out} already exists; the mask is write-once")
    mask, _datas, _cal = _make_mask(args, ext)
    sha = write_mask(args.mask_out, mask)
    print(f"mask {args.mask_out}")
    _print_mask_summary(mask, sha)
    return 0


def run_count_mode(args, ext: dict, git: dict) -> int:
    import numpy

    if args.out.exists():
        raise Refused(f"{args.out} already exists; outputs are write-once")
    raw = args.mask.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != args.mask_sha256:
        raise Refused(f"mask SHA-256 {sha} != expected {args.mask_sha256}")
    mask, datas, cal = _make_mask(args, ext)
    if mask_bytes(mask) != raw:
        raise Refused("rebuilding the mask from the current DB, events, CSV snapshots and code does not "
                      "reproduce the fixed mask file")
    missing = {name: {k: av["non_ok_count"] for k, av in seg["external_availability"].items()}
               for name, seg in mask["segments"].items()}
    if any(n for seg in missing.values() for n in seg.values()) and not args.allow_missing_vix:
        raise Refused(f"scored days without an ok VIX/VIX9D prior value {missing}; pass --allow-missing-vix "
                      "to count them as UNKNOWN_TREATED_AS_HIGH")
    counts = run_count(json.loads(raw), datas, cal, ext)
    manifest = {
        "prereg": PREREG_PATH,
        "git_commit": git["commit"], "git_dirty_tracked": git["dirty_tracked"], "git_dirty_files": git["dirty_files"],
        "allow_dirty": args.allow_dirty, "allow_missing_vix": args.allow_missing_vix,
        "mask_file": str(args.mask), "mask_sha256": sha,
        "events_sha256": {"history": sha256_file(args.events),
                          "live": sha256_file(EVENTS_LIVE) if EVENTS_LIVE.exists() else None},
        "external_csv_sha256": ext["sha256"], "external_csv_files": ext["files"],
        "db_rows_sha256": {n: s["db_rows_sha256"] for n, s in mask["segments"].items()},
        "db_row_hash_fields": list(ROW_HASH_FIELDS),
        "db_row_hash_format": "sha256 over one line per row in ts_utc order: fields joined by '|' "
                              "(ts isoformat, session, repr of floats, market, int(adjusted)), per segment "
                              "and symbol, month chunks",
        "paper_pdf": PAPER,
        "python": platform.python_version(), "numpy": numpy.__version__,
        "random_seed": "not used by the §6.2 count (§6.4 bootstrap seed 20261007)",
    }
    doc = {"schema": "daytrade_v11_precount/v1", "counts": counts, "manifest": manifest}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "x", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    for name, acc in [*counts["segments"].items(), ("total", counts["total"])]:
        print(f"{name}: days {acc['days']}, entry days {acc['entry_days']} {acc['entries']} "
              f"{acc['entries_by_direction']}, open after 10:30 open stage {acc['open_after_1030_days']} "
              f"{acc['open_after_1030']}, risk mode {acc['risk_mode']}, VIX not ok {acc['external_non_ok']}")
    print(f"counts {args.out}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    git = git_state()  # before any read or write
    try:
        if git["dirty_tracked"] is not False and not args.allow_dirty:
            raise Refused(f"tracked files are dirty or git state unknown ({git['dirty_files']}); "
                          "commit first or pass --allow-dirty")
        ext = load_external(args.vix_csv, args.vix9d_csv, args.dix_csv)
        return run_mask_only(args, ext) if args.mask_only else run_count_mode(args, ext, git)
    except (Refused, MaskRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
