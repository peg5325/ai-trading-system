"""Compute step of `eval_repro_v13.py`: one v1.3-route strategy run (segments A and B) with the code under --code-root.

Started by the driver as a separate process with PYTHONPATH cleared. `src` is imported from --code-root only, so the
same file runs the pre-patch and the post-patch code. It gets the research copy, the fixed mask, the event file and
the external CSV snapshots; never the source market DB. It first rebuilds the mask from the copy and stops (exit 4,
status MASK_MISMATCH) unless the scored and excluded days equal the fixed mask's.

Budget (closeout 2026-10-19): each segment account is one `run_account` call and one market-data unit. The ledger
module of THIS repository is loaded by path (it is standard-library only); `AccountCallGuard` reserves each planned
unit right before its call and refuses an unplanned call before it runs. G and D are computed as `run_arm` does.

Outputs: `--out` is the regression projection (v1.0 trade keys only, comparable with pre-patch code). `--full-out`
(post-patch code only) is the full output contract (`src.daytrade_v13.output_contract`).
Uses only APIs present at bab3c0d for the projection.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

LEDGER_FILE = Path(__file__).resolve().parents[1] / "src" / "evalpath" / "ledger.py"


def load_ledger_module():
    spec = importlib.util.spec_from_file_location("_evalpath_ledger_by_path", LEDGER_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUTHORIZATIONS = Path(__file__).resolve().parents[1] / "config" / "eval_authorizations.json"


class AuthorizationRefused(Exception):
    pass


def verify_authorization(path: Path, authorization_id: str, budget: int, market_unit: str) -> None:
    """Re-check the authorization file here too, so running this step directly cannot use a closed authorization,
    another unit or a made-up budget (the arguments are not trusted on their own)."""
    auths = json.loads(Path(path).read_text(encoding="utf-8"))
    if (auths.get("global") or {}).get("halt"):
        raise AuthorizationRefused("all runs halted")
    auth = (auths.get("authorizations") or {}).get(authorization_id)
    if auth is None or auth.get("status") != "active":
        raise AuthorizationRefused(f"authorization {authorization_id!r} is missing or not active")
    if auth.get("budget_unit") != market_unit:
        raise AuthorizationRefused(f"authorization unit {auth.get('budget_unit')!r} is not {market_unit!r}")
    if int(auth.get("budget", -1)) != budget:
        raise AuthorizationRefused(f"budget argument {budget} != authorized {auth.get('budget')}")


def parse(argv=None):
    p = argparse.ArgumentParser()
    for a in ("--code-root", "--copy", "--mask", "--events", "--vix-csv", "--vix9d-csv", "--dix-csv", "--out",
              "--ledger"):
        p.add_argument(a, type=Path, required=True)
    p.add_argument("--authorizations", type=Path, default=AUTHORIZATIONS)
    p.add_argument("--code-label", default=None, help="commit the code root was exported from (run_record.commit)")
    p.add_argument("--full-out", type=Path, default=None)
    p.add_argument("--run-key", required=True)
    p.add_argument("--authorization-id", required=True)
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--unit-keys", required=True, help="JSON list of the planned unit keys of this run")
    p.add_argument("--policy", required=True, choices=["v1.0+", "v1.3"])
    p.add_argument("--costs", required=True, help='JSON {"commission":..,"sec_fee":..,"slippage":..}')
    p.add_argument("--delay", type=int, default=0)
    return p.parse_args(argv)


def mask_days(mask: dict) -> dict:
    return {n: {"scored": s["scored"], "excluded": [(e["date"], e["kinds"]) for e in s["excluded"]]}
            for n, s in mask["segments"].items()}


def run_guarded(seg_days: dict, policy, costs, delay: int, run_account, guard, run_key: str) -> dict:
    """run_arm, one guarded run_account call per segment: {"segments", "G", "D", "run_account_calls"}."""
    segs = {name: guard.call(f"{run_key}/{name}", run_account, days, policy, costs, delay)
            for name, days in seg_days.items()}
    rs = [s["R"] for s in segs.values()]
    return {"segments": segs, "G": sum(rs) / len(rs) if rs else 0.0,
            "D": max((s["D"] for s in segs.values()), default=0.0), "run_account_calls": len(segs)}


def projection(arm: dict, v10_keys) -> dict:
    """Regression projection: economic fields only (drops timing, cost_breakdown, result_kind, ... on purpose)."""
    return {name: {"R": acc["R"], "D": acc["D"], "equity_end": acc["equity_end"], "account_halt": acc["account_halt"],
                   "days": [{"date": d["date"], "situations": d["situations"], "day_pnl": d["pnl"]["day_pnl"],
                             "equity_end": d["pnl"]["equity_end"], "blocked": d["blocked_entries"],
                             "lock": d["lock"], "trades": [{k: t[k] for k in v10_keys} for t in d["trades"]]}
                            for d in acc["days"]]}
            for name, acc in arm["segments"].items()}


def main(argv=None) -> int:
    args = parse(argv)
    ledger = load_ledger_module()
    try:
        verify_authorization(args.authorizations, args.authorization_id, args.budget, ledger.MARKET_UNIT)
    except AuthorizationRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
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
    from src.daytrade_v13.runner import run_account, segment_days

    unit_keys = json.loads(args.unit_keys)
    guard = ledger.AccountCallGuard(args.ledger, args.authorization_id, args.budget, unit_keys, ledger.MARKET_UNIT)
    out: dict = {"schema": "eval_repro_v13_compute/v2", "output_kind": "regression_projection",
                 "code_root": str(root), "policy": args.policy, "costs": json.loads(args.costs), "delay": args.delay,
                 "inputs_sha256": {k: hashlib.sha256(Path(getattr(args, k)).read_bytes()).hexdigest()
                                   for k in ("mask", "events", "vix_csv", "vix9d_csv", "dix_csv")},
                 "external_alias": "load_external stores the --dix-csv snapshot under the key 'gex'"}
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
    costs = Costs(**json.loads(args.costs))
    arm = run_guarded(seg_days, pol, costs, args.delay, run_account, guard, args.run_key)
    out.update({"status": "OK", "run_account_calls": arm["run_account_calls"], "G": arm["G"], "D": arm["D"],
                "segments": projection(arm, V10_TRADE_KEYS)})
    _write(args.out, out)
    if args.full_out is not None:
        _write_full(args, arm, pol, costs, fixed)
    return 0


def _write_full(args, arm: dict, pol, costs, fixed: dict) -> None:
    try:
        from src.daytrade_v13 import output_contract as oc
        from src.daytrade_v13.policy import cost_model_id
        from src.daytrade_v13.runio import config_hash, run_record
    except ImportError:
        _write(args.full_out, {"output_kind": "full_output", "status": "NOT_AVAILABLE_IN_THIS_CODE_VERSION"})
        return
    full_arm = {"result_kind": "account_sim", "cost_model_id": cost_model_id(costs), "policy": pol.name,
                "delay": args.delay, "segments": arm["segments"], "G": arm["G"], "D": arm["D"]}
    commit = args.code_label or {"value": None, "unknown_reason": "no --code-label given"}
    rec = run_record(run_id=args.run_key, run_type="ENGINE_REPRO", commit=commit,
                     config_hash=config_hash(pol, costs, args.delay),
                     cost_model_ids=[cost_model_id(costs)], mask_sha256=hashlib.sha256(args.mask.read_bytes()).hexdigest())
    doc = oc.build_full_output(full_arm, label="ENGINE_REPRO", run_record=rec,
                               mask={"sha256": rec["mask_sha256"], "schema": fixed.get("schema"),
                                     "quality_conditional": True})
    path = Path(args.full_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(oc.dumps(doc))


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    sys.exit(main())
