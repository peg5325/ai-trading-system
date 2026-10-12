"""Entry study v1 development test, run once (PREREG_DAYTRADE_ENTRY_STUDY_V1 §7).

Refuses unless --i-have-user-approval is given, the gate / mask / data manifest files hash to the given values,
the event calendar is the one in the mask, and the DB rows read hash to the manifest. Development bars only
(upper bound 2026-01-21 00:00 ET); reads via research_load(purpose="performance"). Writes dev_result.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_paper.external import load_events
from src.entry_study_v1 import devrun
from src.entry_study_v1 import mask as M
from src.entry_study_v1.cli import (
    add_fixed_args,
    cells,
    check_mask_rebuild,
    passing_pairs,
    reproduction_record,
    require_dev_inputs,
    write_json_once,
)
from src.entry_study_v1.config import EVENTS_HISTORY, sha256_file
from src.entry_study_v1.data import DEV_SEGMENTS
from src.entry_study_v1.pipeline import (
    load_dev_segments,
    pair_trades,
    verify_against_manifest,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_fixed_args(ap, approval=True)
    args = ap.parse_args(argv)
    gate, mask, manifest = require_dev_inputs(args)
    pairs = passing_pairs(gate, mask)
    symbols = sorted({s for t in pairs.values() for s in t})
    seg_data = load_dev_segments(args.db, symbols, purpose="performance")
    verify_against_manifest(seg_data, manifest)  # before any trade is built
    cal = load_events(EVENTS_HISTORY)
    check_mask_rebuild(mask, M.build_mask(pairs, seg_data, DEV_SEGMENTS, cal, sha256_file(EVENTS_HISTORY)))
    all_cells = cells()
    results = {p: pair_trades(p, all_cells, mask["pairs"][p], seg_data, DEV_SEGMENTS) for p in pairs}
    out = devrun.run_dev(all_cells, results)
    out["reproduction"] = {**reproduction_record(), "gate_sha256": args.gate_sha256,
                           "mask_sha256": args.mask_sha256, "data_manifest_sha256": args.data_sha256}
    print("dev_result.json sha256", write_json_once(args.out_dir / "dev_result.json", out))
    print("verdict", out["verdict"], "selected_for_oos", out["selected_for_oos"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
