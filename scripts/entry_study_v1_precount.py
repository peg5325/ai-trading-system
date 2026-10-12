"""Entry study v1 pre-run count and power check (PREREG_DAYTRADE_ENTRY_STUDY_V1 §6). Signal counts only.

Reads the reference symbols (SMH/QQQ/SPY) only, via research_load(purpose="performance"), after checking that the
gate, mask and data manifest are the fixed ones. No ETF price is read and no return is computed. The count is
the user's "run it / stop" input (PREREG §12 step 4).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.entry_study_v1 import precount as PC
from src.entry_study_v1.cli import (
    add_fixed_args,
    cells,
    passing_pairs,
    reproduction_record,
    require_dev_inputs,
    write_json_once,
)
from src.entry_study_v1.data import DEV_SEGMENTS
from src.entry_study_v1.pipeline import load_dev_segments, verify_against_manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_fixed_args(ap, approval=False)
    args = ap.parse_args(argv)
    gate, mask, manifest = require_dev_inputs(args)
    pairs = passing_pairs(gate, mask)
    refs = sorted({ref for ref, _, _ in pairs.values()})
    seg_data = load_dev_segments(args.db, refs, purpose="performance")
    verify_against_manifest(seg_data, manifest)
    k_total = 10 * len(pairs)
    out = {"k_total": k_total, "pairs": {}, "reproduction": reproduction_record()}
    for pair in pairs:
        counts = PC.count_pair(pair, cells(), mask["pairs"][pair],
                               {s.name: {mask["pairs"][pair]["ref"]: seg_data[s.name][mask["pairs"][pair]["ref"]]}
                                for s in DEV_SEGMENTS}, DEV_SEGMENTS)
        out["pairs"][pair] = {"counts": counts, "power": PC.power_table(pair, counts, k_total)}
    print("precount.json sha256", write_json_once(args.out_dir / "precount.json", out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
