"""Entry study v1 scored-date mask and data manifest (PREREG_DAYTRADE_ENTRY_STUDY_V1 §5.1). No P&L.

Needs the fixed gate.json (--gate, --gate-sha256); builds the mask for the pairs that PASSED, reading development
data via research_load(purpose="performance") (price sanity and the 15% gap filter only). Writes mask.json and
data_manifest.json (write-once) and prints their SHA-256.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_paper.external import load_events
from src.entry_study_v1 import mask as M
from src.entry_study_v1.cli import (
    OUT_DIR,
    check_sha,
    pair_symbols,
    reproduction_record,
    write_json_once,
)
from src.entry_study_v1.config import EVENTS_HISTORY, sha256_file
from src.entry_study_v1.data import DEV_SEGMENTS
from src.entry_study_v1.pipeline import load_dev_segments


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--gate", type=Path, required=True)
    ap.add_argument("--gate-sha256", required=True)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)
    check_sha(args.gate, args.gate_sha256, "gate")
    gate = json.loads(args.gate.read_text(encoding="utf-8"))
    live = {p for p, g in gate["pairs"].items() if g["status"] == "PASS"}
    if not live:
        raise SystemExit("DATA_GATE_FAIL for every pair: the study stops (PREREG §5.2)")
    pairs = pair_symbols(live)
    symbols = sorted({s for t in pairs.values() for s in t})
    seg_data = load_dev_segments(args.db, symbols, purpose="performance")
    cal = load_events(EVENTS_HISTORY)
    mask = M.build_mask(pairs, seg_data, DEV_SEGMENTS, cal, sha256_file(EVENTS_HISTORY))
    mask["gate_sha256"] = args.gate_sha256
    mask["reproduction"] = reproduction_record()
    manifest = M.build_manifest(seg_data, DEV_SEGMENTS)
    print("mask.json sha256", write_json_once(args.out_dir / "mask.json", mask))
    print("data_manifest.json sha256", write_json_once(args.out_dir / "data_manifest.json", manifest))
    for p, m in mask["pairs"].items():
        for s, v in m["segments"].items():
            print(p, s, "scored", v["scored_count"], "excluded", v["excluded_count"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
