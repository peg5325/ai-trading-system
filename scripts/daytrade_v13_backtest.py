"""v1.3 historical run (PREREG_DAYTRADE_V1_3_SIZING §9; v1.1 PREREG §6.4-§6.5). Refuses without user approval.

Runs `v1.0+`, `v1.3-cap` and `v1.3` on the fixed v1.1 mask (two independent USD 2,000 segment accounts) at base
cost, under stress (a)+(b) (cost x1.5 and a 1-minute delay of open-filled orders) and, for reference only, cost x2.
Checks: §5 operational checks on every run of every arm, §9 test ① (v1.0+ vs v1.0 run_day) and ② (v1.3-cap with
the 1.5% refusal restored = v1.0+). Verdict: v1.3-cap vs v1.0+ only, v1.1 §6.4 table; any invalid run, failed
check or failed regression makes the whole run invalid (row 0). Failed runs are written, never deleted.

This computes P&L on historical prices. It refuses unless --i-have-user-approval is passed. No download, no orders.
Refuses on a dirty tracked worktree unless --allow-dirty. Output is write-once. If the run raises anything, a
write-once failure record (verdict row 0, exception, traceback summary, manifest) goes to --out and the exit is 3.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_v11.data import MaskRefused, load_external
from src.daytrade_v13.day import InvalidRun
from src.daytrade_v13.policy import (
    ARMS,
    BASE_COSTS,
    V10_PLUS,
    V13,
    V13_CAP,
    with_stop_cap,
)
from src.daytrade_v13.report import arm_summary, decompose_d, not_double
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
from src.daytrade_v13.runner import (
    MISMATCH,
    arm_violations,
    regression_one,
    regression_two,
    run_arm,
    segment_days,
)
from src.daytrade_v13.stats import (
    SEED,
    B,
    bootstrap_test,
    d_by_runs,
    daily_d,
    inference_mean,
    verdict,
)

VARIANTS = {  # name: (cost multiplier, delay in bars, used in the verdict)
    "base": (1.0, 0, True),
    "stress_ab": (1.5, 1, True),
    "reference_cost2x": (2.0, 0, False),
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--i-have-user-approval", action="store_true",
                   help="required: the user approved this historical P&L run after code review")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--vix-csv", type=Path, required=True)
    p.add_argument("--vix9d-csv", type=Path, required=True)
    p.add_argument("--dix-csv", type=Path, default=None, help="GEX (record only; must match the mask's snapshot)")
    p.add_argument("--events", type=Path, default=EVENTS_HISTORY)
    p.add_argument("--mask", type=Path, required=True, help="fixed v1.1 mask (reports/daytrade_v11/v11_mask.json)")
    p.add_argument("--out", type=Path, required=True, help="result JSON (write-once)")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--allow-missing-vix", action="store_true")
    return p.parse_args(argv)


def _compact(arm: dict) -> dict:
    """Per-day record kept in the output: return, trades (no bars), lock, halt."""
    out = {}
    for name, acc in arm["segments"].items():
        out[name] = [{"date": d["date"], "r": d["pnl"]["day_return"], "situations": d["situations"],
                      "lock": d["lock"], "account_halt": d["account_halt"], "blocked": d["blocked_entries"],
                      "trades": [{k: t[k] for k in ("kind", "symbol", "entry_time", "exit_time", "exit_reason",
                                                    "notional", "stop_frac", "pnl", "sizing")} for t in d["trades"]]}
                     for d in acc["days"]]
    return out


def run_all(seg_days: dict, mask: dict) -> dict:
    invalid: list[str] = []
    runs: dict[str, dict[str, dict]] = {}
    for vname, (mult, delay, _used) in VARIANTS.items():
        costs = BASE_COSTS.scaled(mult) if mult != 1.0 else BASE_COSTS
        for pol in ARMS:
            try:
                runs.setdefault(vname, {})[pol.name] = run_arm(seg_days, pol, costs, delay)
            except InvalidRun as exc:
                invalid.append(f"{pol.name} {vname}: invalid run: {exc}")
    operational = {v: {a: arm_violations(r) for a, r in arms.items()} for v, arms in runs.items()}
    for v, arms in operational.items():
        for viol in arms.values():
            invalid.extend(f"operational check failed ({v}): {x}" for x in viol)

    base = runs.get("base", {})
    regression: dict = {}
    if V10_PLUS.name in base:
        try:
            regression["one"] = regression_one(seg_days, base[V10_PLUS.name])
            if regression["one"]["counts"][MISMATCH]:
                invalid.append(f"§9 test ① mismatch on {regression['one']['counts'][MISMATCH]} days (implementation bug)")
            regression["two"] = regression_two(run_arm(seg_days, with_stop_cap(V13_CAP)), base[V10_PLUS.name])
            if regression["two"]["mismatch_days"]:
                invalid.append(f"§9 test ② mismatch on {len(regression['two']['mismatch_days'])} days")
        except InvalidRun as exc:
            invalid.append(f"regression run invalid: {exc}")

    stress = runs.get("stress_ab", {})
    have = all(p.name in base and p.name in stress for p in (V10_PLUS, V13_CAP))
    test, m_stress, reports = None, None, {}
    if have:
        d = daily_d(base[V13_CAP.name], base[V10_PLUS.name])
        test = bootstrap_test(d_by_runs(d, mask))
        m_stress = inference_mean(d_by_runs(daily_d(stress[V13_CAP.name], stress[V10_PLUS.name]), mask))
        reports["d_decomposition"] = decompose_d(base[V13_CAP.name], base[V10_PLUS.name], d, mask)
    else:
        invalid.append("comparison runs missing")
    if V13.name in base and V13_CAP.name in base:
        reports["v13_not_double"] = not_double(base[V13.name], base[V13_CAP.name])
    reports["arms"] = {v: {a: arm_summary(r) for a, r in arms.items()} for v, arms in runs.items()}
    if have:
        cap, plus = base[V13_CAP.name], base[V10_PLUS.name]
        res = verdict(invalid=invalid, g_arm=cap["G"], g_base=plus["G"], d_arm=cap["D"], d_base=plus["D"],
                      test=test, m_stress=m_stress)
    else:
        res = verdict(invalid=invalid, g_arm=0.0, g_base=0.0, d_arm=0.0, d_base=0.0,
                      test={"m": None, "p": None, "insufficient": "no runs"}, m_stress=None)
    return {"verdict": res, "invalid": invalid, "test": test, "stress_ab_m": m_stress, "regression": regression,
            "operational_violations": operational, "reports": reports,
            "days": {v: {a: _compact(r) for a, r in arms.items()} for v, arms in runs.items()}}


def main(argv=None, expected_mask_sha: str = V11_MASK_SHA256) -> int:
    args = parse_args(argv)
    git = git_state()  # before any read or write
    try:
        if not args.i_have_user_approval:
            raise Refused("this run computes historical P&L; pass --i-have-user-approval only after the user "
                          "approved it")
        refuse_dirty(git, args.allow_dirty)
        refuse_existing(args.out)
        ext = load_external(args.vix_csv, args.vix9d_csv, args.dix_csv)
        mask, datas, cal = load_fixed_mask(args.mask, expected_mask_sha, args.db, args.events, ext)
        refuse_missing_vix(mask, args.allow_missing_vix)
        seg_days = segment_days(mask, datas, cal, ext)
    except (Refused, MaskRefused, InvalidRun) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    mask_sha = hashlib.sha256(args.mask.read_bytes()).hexdigest()
    man = manifest(git, args, mask, mask_sha, ext, {
        "random_seed": SEED, "bootstrap_B": B, "variants": {k: {"cost_mult": m, "delay_bars": dl, "verdict": u}
                                                            for k, (m, dl, u) in VARIANTS.items()},
        "arms": [p.name for p in ARMS]})
    try:
        result = run_all(seg_days, mask)
    except Exception as exc:  # noqa: BLE001 - a failed run must be written, never lost
        return write_failure(args.out, exc, man)
    result["manifest"] = man
    result["schema"] = "daytrade_v13_backtest/v1"
    sha = write_json_once(args.out, result)
    v = result["verdict"]
    print(f"verdict v1.3-cap vs v1.0+: row {v['row']} {v['label']} ({'; '.join(map(str, v['why']))})")
    print(f"result {args.out} sha256 {sha}")
    return 0


def write_failure(out: Path, exc: BaseException, man: dict) -> int:
    """Write-once failure record (verdict row 0) with the exception and a traceback summary; exit 3."""
    tb = traceback.TracebackException.from_exception(exc)
    doc = {"schema": "daytrade_v13_backtest_failure/v1",
           "verdict": {"row": 0, "label": "무효", "why": [f"run failed: {type(exc).__name__}: {exc}"]},
           "error": {"type": type(exc).__name__, "message": str(exc),
                     "traceback": [f"{f.filename}:{f.lineno} in {f.name}" for f in tb.stack][-15:],
                     "formatted": "".join(tb.format())[-8000:]},
           "manifest": man}
    sha = write_json_once(out, doc)
    print(f"run failed ({type(exc).__name__}: {exc}); failure record {out} sha256 {sha}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
