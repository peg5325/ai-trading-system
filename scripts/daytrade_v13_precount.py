"""v1.3 pre-run count (PREREG_DAYTRADE_V1_3_SIZING §7). Counts only: no P&L, no exits, no prices in the output.

On the fixed v1.1 mask (SHA-256 checked against the PREREG value and rebuilt byte for byte from the same DB, events
and CSV snapshots) count the morning entry candidates v1.3-cap admits that v1.0 refused (by signal, direction,
segment, risk mode, inference-population membership), the days where v1.3's 70% single cap binds on those morning
entries, and the S5-condition days overlapping new morning candidates (condition only).

No download, no orders. Refuses on a dirty tracked worktree unless --allow-dirty. Output is write-once.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_v11.data import MaskRefused, load_external
from src.daytrade_v13.day import InvalidRun
from src.daytrade_v13.precount import CountInvariantError, run_precount
from src.daytrade_v13.runio import (
    EVENTS_HISTORY,
    V11_MASK_SHA256,
    Refused,
    git_state,
    load_fixed_mask,
    manifest,
    refuse_dirty,
    refuse_existing,
    refuse_missing_vix,
    write_json_once,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--vix-csv", type=Path, required=True)
    p.add_argument("--vix9d-csv", type=Path, required=True)
    p.add_argument("--dix-csv", type=Path, default=None, help="GEX (record only; must match the mask's snapshot)")
    p.add_argument("--events", type=Path, default=EVENTS_HISTORY)
    p.add_argument("--mask", type=Path, required=True, help="fixed v1.1 mask (reports/daytrade/v11/v11_mask.json)")
    p.add_argument("--out", type=Path, required=True, help="counts JSON (write-once)")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-missing-vix", action="store_true")
    return p.parse_args(argv)


def _print(counts: dict) -> None:
    for name, acc in [*counts["segments"].items(), ("total", counts["total"])]:
        new = acc["new"]
        print(f"{name}: days {acc['days']}, S0 {acc['s0_days']}, morning signal {acc['morning_signal']}, "
              f"v1.0 entry {acc['v10_entry']}, v1.3-cap entry {acc['v13cap_entry']}")
        print(f"  new candidates {new['days']} {new['by_signal']} {new['by_direction']} risk {new['by_risk_mode']} "
              f"inference {new['inference']} / not {new['not_inference']}")
        print(f"  70% cap binds on new {acc['cap70_binds_new']} (all v1.3 morning {acc['cap70_binds_all_v13_morning']}),"
              f" S5 condition days {acc['s5_condition_days']}, overlapping new {acc['s5_overlap_new']}")


def main(argv=None, expected_mask_sha: str = V11_MASK_SHA256) -> int:
    args = parse_args(argv)
    git = git_state()  # before any read or write
    try:
        refuse_dirty(git, args.allow_dirty)
        refuse_existing(args.out)
        ext = load_external(args.vix_csv, args.vix9d_csv, args.dix_csv)
        mask, datas, cal = load_fixed_mask(args.mask, expected_mask_sha, args.db, args.events, ext)
        refuse_missing_vix(mask, args.allow_missing_vix)
        counts = run_precount(mask, datas, cal, ext)
        mask_sha = hashlib.sha256(args.mask.read_bytes()).hexdigest()
        man = manifest(git, args, mask, mask_sha, ext,
                       {"stage": "§7 pre-run count (no P&L, no exits)", "random_seed": "not used by the §7 count"})
        write_json_once(args.out, {"schema": "daytrade_v13_precount/v1", "counts": counts, "manifest": man})
    except (Refused, MaskRefused, CountInvariantError, InvalidRun) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    _print(counts)
    print(f"counts {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
