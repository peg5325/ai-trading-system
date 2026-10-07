"""v1.1 pre-run count (PREREG_DAYTRADE_V1_1_INTRADAY_REVERSAL §6.1 mask + §6.2 count). Counts only.

Steps: hash the snapshots -> check segment boundaries (dates only) -> load segments via research_load ->
build the scored-date mask -> write it (write-once) and hash it -> re-read the mask file -> count v1.0 morning
entries and whether they are still open after the 10:30 open stage -> write counts + manifest (write-once).
No download, no orders, no P&L.
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
    SEGMENTS,
    boundary_check,
    build_mask,
    load_external,
    load_segment,
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


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--vix-csv", type=Path, required=True)
    p.add_argument("--vix9d-csv", type=Path, required=True)
    p.add_argument("--dix-csv", type=Path, default=None, help="GEX (record only)")
    p.add_argument("--events", type=Path, default=EVENTS_HISTORY)
    p.add_argument("--out", type=Path, required=True, help="counts JSON (write-once)")
    p.add_argument("--mask-out", type=Path, default=None, help="mask JSON (default: <out stem>_mask.json)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    mask_path = args.mask_out or args.out.with_name(args.out.stem + "_mask.json")
    for p in (args.out, mask_path):
        if p.exists():
            print(f"{p} already exists; outputs are write-once", file=sys.stderr)
            return 2

    import numpy

    events_sha = sha256_file(args.events)
    ext = load_external(args.vix_csv, args.vix9d_csv, args.dix_csv)
    cal = load_events(args.events)
    sources = {"events_file": str(args.events.relative_to(ROOT) if args.events.is_relative_to(ROOT) else args.events),
               "events_sha256": events_sha,
               "exchange_calendar_sha256": hashlib.sha256(
                   json.dumps(artifact_summary(), sort_keys=True).encode()).hexdigest(),
               "boundary_check": boundary_check(args.db)}
    datas = [load_segment(args.db, seg) for seg in SEGMENTS]
    mask = build_mask(datas, cal, sources)
    mask_sha = write_mask(mask_path, mask)

    raw = mask_path.read_bytes()  # count from the fixed file, not the in-memory object
    if hashlib.sha256(raw).hexdigest() != mask_sha:
        raise RuntimeError("mask file changed after writing")
    counts = run_count(json.loads(raw), datas, cal, ext)

    manifest = {
        "prereg": PREREG_PATH,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "mask_file": str(mask_path), "mask_sha256": mask_sha,
        "events_sha256": {"history": events_sha,
                          "live": sha256_file(EVENTS_LIVE) if EVENTS_LIVE.exists() else None},
        "external_csv_sha256": ext["sha256"], "external_csv_files": ext["files"],
        "db_rows_sha256": {d.segment.name: d.row_sha256 for d in datas},
        "paper_pdf": PAPER,
        "python": platform.python_version(), "numpy": numpy.__version__,
        "random_seed": "not used by the §6.2 count (§6.4 bootstrap seed 20261007)",
    }
    doc = {"schema": "daytrade_v11_precount/v1", "counts": counts, "manifest": manifest}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "x", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

    print(f"mask {mask_path} sha256 {mask_sha}")
    for name, seg in mask["segments"].items():
        print(f"segment {name}: {seg['calendar_sessions']} sessions, scored {seg['scored_count']}, "
              f"excluded {seg['excluded_count']}")
    for name, acc in [*counts["segments"].items(), ("total", counts["total"])]:
        print(f"{name}: days {acc['days']}, entry days {acc['entry_days']} {acc['entries']}, "
              f"open after 10:30 open stage {acc['open_after_1030_days']} {acc['open_after_1030']}")
    print(f"counts {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
