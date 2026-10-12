"""Entry study v1 data gate G1..G4 (PREREG_DAYTRADE_ENTRY_STUDY_V1 §5.2). No P&L.

G1-G3 read bars with research_load(purpose="coverage"); G4 (timing alignment, price correlation on development
data) with purpose="performance". Development upper bound 2026-01-21 00:00 ET is enforced. Writes gate.json
(write-once) and prints its SHA-256.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.entry_study_v1 import gate as G
from src.entry_study_v1.cli import (
    OUT_DIR,
    pair_symbols,
    reproduction_record,
    write_json_once,
)
from src.entry_study_v1.data import DEV_SEGMENTS
from src.entry_study_v1.pipeline import load_dev_segments


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    pairs = pair_symbols()
    symbols = sorted({s for t in pairs.values() for s in t})
    cov = load_dev_segments(args.db, symbols, purpose="coverage")
    perf = load_dev_segments(args.db, symbols, purpose="performance")
    for seg in DEV_SEGMENTS:
        for s in symbols:
            if cov[seg.name][s].row_sha256 != perf[seg.name][s].row_sha256:
                raise SystemExit(f"{s} {seg.name}: coverage and performance reads differ")
    out = {"schema": G.GATE_SCHEMA, "pairs": {
        p: G.pair_gate(p, ref, lo, sh, DEV_SEGMENTS, cov, perf) for p, (ref, lo, sh) in pairs.items()},
        "g2_boundary_dates": G.boundary_dates(args.db, tuple(symbols), DEV_SEGMENTS),
        "reproduction": reproduction_record()}
    sha = write_json_once(args.out_dir / "gate.json", out)
    print(f"gate.json sha256 {sha}")
    for p, g in out["pairs"].items():
        print(p, g["status"], {s: round(v["ratio"], 4) for s, v in g["g1"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
