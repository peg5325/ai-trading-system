"""Compute step of `eval_repro_v13.py`: one v1.3-route account run (both segments) with the code under --code-root.

Started by the driver as a separate process with PYTHONPATH cleared. `src` is imported from --code-root only, so the
same file runs the pre-patch and the post-patch code. It gets the research copy, the fixed mask, the event file and
the external CSV snapshots; never the source market DB. It first rebuilds the mask from the copy and stops (exit 4,
status MASK_MISMATCH) unless the scored and excluded days equal the fixed mask's, so a different warm-up or data set
cannot silently change the evaluated days. Uses only APIs present at bab3c0d.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def parse(argv=None):
    p = argparse.ArgumentParser()
    for a in ("--code-root", "--copy", "--mask", "--events", "--vix-csv", "--vix9d-csv", "--dix-csv", "--out"):
        p.add_argument(a, type=Path, required=True)
    p.add_argument("--policy", required=True, choices=["v1.0+", "v1.3"])
    p.add_argument("--costs", required=True, help='JSON {"commission":..,"sec_fee":..,"slippage":..}')
    p.add_argument("--delay", type=int, default=0)
    return p.parse_args(argv)


def mask_days(mask: dict) -> dict:
    return {n: {"scored": s["scored"], "excluded": [(e["date"], e["kinds"]) for e in s["excluded"]]}
            for n, s in mask["segments"].items()}


def main(argv=None) -> int:
    args = parse(argv)
    root = args.code_root.resolve()
    sys.path.insert(0, str(root))
    import src

    if not Path(src.__file__).resolve().is_relative_to(root):
        raise SystemExit(f"src imported from {src.__file__}, not from {root}")
    from src.daytrade_paper.external import load_events
    from src.daytrade_v11.data import (
        SEGMENTS,
        boundary_check,
        build_mask,
        load_external,
        load_segment,
    )
    from src.daytrade_v13.day import V10_TRADE_KEYS
    from src.daytrade_v13.policy import V10_PLUS, V13, Costs
    from src.daytrade_v13.runner import run_arm, segment_days

    out: dict = {"schema": "eval_repro_v13_compute/v1", "code_root": str(root), "policy": args.policy,
                 "costs": json.loads(args.costs), "delay": args.delay,
                 "inputs_sha256": {k: hashlib.sha256(Path(getattr(args, k)).read_bytes()).hexdigest()
                                   for k in ("mask", "events", "vix_csv", "vix9d_csv", "dix_csv")}}
    ext = load_external(args.vix_csv, args.vix9d_csv, args.dix_csv)
    cal = load_events(args.events)
    out["boundary_check"] = boundary_check(args.copy)
    datas = [load_segment(args.copy, seg) for seg in SEGMENTS]
    out["copy_rows_sha256"] = {d.segment.name: d.row_sha256 for d in datas}
    out["copy_row_counts"] = {d.segment.name: d.row_counts for d in datas}
    fixed = json.loads(args.mask.read_text(encoding="utf-8"))
    rebuilt = build_mask(datas, cal, {}, ext)
    if mask_days(rebuilt) != mask_days(fixed):
        diff = {n: {k: sorted(set(map(str, mask_days(rebuilt)[n][k])) ^ set(map(str, mask_days(fixed)[n][k])))
                    for k in ("scored", "excluded")} for n in fixed["segments"]}
        out.update({"status": "MASK_MISMATCH", "diff": diff})
        _write(args.out, out)
        return 4
    seg_days = segment_days(fixed, datas, cal, ext)
    pol = {"v1.0+": V10_PLUS, "v1.3": V13}[args.policy]
    arm = run_arm(seg_days, pol, Costs(**json.loads(args.costs)), args.delay)
    out["status"] = "OK"
    out["run_account_calls"] = len(arm["segments"])
    out["G"], out["D"] = arm["G"], arm["D"]
    out["segments"] = {
        name: {"R": acc["R"], "D": acc["D"], "equity_end": acc["equity_end"], "account_halt": acc["account_halt"],
               "days": [{"date": d["date"], "situations": d["situations"], "day_pnl": d["pnl"]["day_pnl"],
                         "equity_end": d["pnl"]["equity_end"], "blocked": d["blocked_entries"],
                         "lock": d["lock"], "trades": [{k: t[k] for k in V10_TRADE_KEYS} for t in d["trades"]]}
                        for d in acc["days"]]}
        for name, acc in arm["segments"].items()}
    _write(args.out, out)
    return 0


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    sys.exit(main())
