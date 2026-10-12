"""Reversal study v1 pre-run count and power check (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §6). Signal counts only.

Runs the full §5.1 verification first (fixed gate / mask / manifest hashes from config/reversal_study_v1.json,
events calendar, DB rows of SMH SOXL SOXS QQQ TQQQ SQQQ against the manifest, P1/P2 mask rebuilt from the verified
rows). Rows are read with research_load(purpose="coverage") for the hash / mask check; signals use the reference
symbols' bars only. No ETF price is used and no return is computed. The count is the user's "run it / stop" input.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.entry_study_v1.data import DEV_SEGMENTS
from src.reversal_study_v1 import cli as C
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import precount as PC


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    C.add_fixed_args(ap, approval=False)
    args = ap.parse_args(argv)
    _gate, mask, _manifest, pairs, seg_data = C.prepare_dev(args, purpose="coverage")
    cells = RC.load_cells()
    out = {"k_total": RC.K_TOTAL, "pairs": {}, "reproduction": C.reproduction_record()}
    for pair in pairs:
        ref = mask["pairs"][pair]["ref"]
        counts = PC.count_pair(pair, cells, mask["pairs"][pair],
                               {s.name: {ref: seg_data[s.name][ref]} for s in DEV_SEGMENTS}, DEV_SEGMENTS)
        out["pairs"][pair] = {"counts": counts, "power": PC.power_table(pair, counts)}
    print("precount.json sha256", C.write_json_once(args.out_dir / "precount.json", out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
