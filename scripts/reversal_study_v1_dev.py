"""Reversal study v1 development test, run once (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §7, §7.5).

Refuses unless --i-have-user-approval is given and the full §5.1 verification passes (fixed gate / mask / manifest
hashes from config/reversal_study_v1.json, events calendar, DB rows of the six symbols against the manifest, P1/P2
mask rebuilt from the verified rows, clean work tree). Development bars only (upper bound 2026-01-21 00:00 ET),
read via research_load(purpose="performance"). With --entry-dev-result the §7.5 exit overlay is added when the
entry study's dev_result.json (fixed SHA-256 in the config) is reproduced exactly. Writes dev_result.json once.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.entry_study_v1 import config as EC
from src.entry_study_v1.config import sha256_file
from src.entry_study_v1.data import DEV_SEGMENTS
from src.entry_study_v1.pipeline import pair_trades
from src.reversal_study_v1 import cli as C
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import devrun
from src.reversal_study_v1 import rules as RL
from src.reversal_study_v1.pipeline import collect_trades, overlay_report


def overlay(args, mask, pairs, seg_data) -> dict:
    if args.entry_dev_result is None:
        return {"reported": False, "why": "--entry-dev-result not given"}
    want = RC.frozen_inputs()["entry_dev_result_sha256"]
    got = sha256_file(args.entry_dev_result)
    if got != want:
        return {"reported": False, "why": f"entry dev_result.json hashes to {got}, expected {want}"}
    entry_dev = json.loads(args.entry_dev_result.read_text(encoding="utf-8"))
    entry_cells = EC.load_cells()
    entry_trades = {p: pair_trades(p, entry_cells, mask["pairs"][p], seg_data, DEV_SEGMENTS) for p in pairs}
    params = {c.rule: c.params for c in RC.load_cells()}
    return overlay_report(entry_cells, entry_trades, entry_dev, seg_data, DEV_SEGMENTS, params["T3"], params["T4"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    C.add_fixed_args(ap, approval=True)
    ap.add_argument("--entry-dev-result", type=Path, default=None,
                    help="entry study dev_result.json (enables the §7.5 overlay report)")
    args = ap.parse_args(argv)
    _gate, mask, _manifest, pairs, seg_data = C.prepare_dev(args, purpose="performance")
    cells = RC.load_cells()
    trades: dict = {}
    for pair in pairs:
        trades.update(collect_trades([c for c in cells if c.pair == pair], RL.evaluate, mask["pairs"][pair],
                                     seg_data, DEV_SEGMENTS))
    out = devrun.run_dev(cells, trades)
    out["overlay"] = overlay(args, mask, pairs, seg_data)
    out["reproduction"] = C.reproduction_record()
    print("dev_result.json sha256", C.write_json_once(args.out_dir / "dev_result.json", out))
    print("verdict", out["verdict"], "selected_for_oos", out["selected_for_oos"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
