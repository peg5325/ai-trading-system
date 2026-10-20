"""Full output contract of a v1.3-route run (closeout 2026-10-19). Report only; no calculation changes.

Two outputs are kept apart:
- regression projection (scripts/eval_repro_v13_compute.py `--out`): only the v1.0 trade keys, so pre-patch and
  post-patch code can be compared field by field. It deliberately drops the reporting fields.
- full output (this module): every trade with `timing` and `cost_breakdown`, every account with `result_kind` and
  `cost_model_id`, the run's `verdict_axes` and `run_record`, and what G and D mean.

`build_full_output` -> `dumps` -> `loads` -> `missing_fields` is the serialization path checked by tests.
"""
from __future__ import annotations

import json

from src.daytrade_paper import rules as R
from src.daytrade_v13.report import FAIL, NOT_EVALUATED, PASS, verdict_axes

OUTPUT_CONTRACT_VERSION = "v13_full_output/v1"
SYNTHETIC_LABEL = "SYNTHETIC_OUTPUT_CONTRACT_EXAMPLE"
AGGREGATION = {
    "segment_R": "compounded return of one segment account over its scored days (equity_end / start_equity - 1)",
    "G": "arithmetic mean of the segment R values. Segments A and B are independent accounts, each starting at "
         "start_equity (reset per segment). G is not the return of one continuous account and not a CAGR.",
    "D": "maximum over segments of each account's maximum drawdown of bar-close liquidation values",
    "days": "the scored days of the fixed v1.1 quality mask, built from whole-day bar completeness: results are "
            "conditional on that mask and are not a real-time executability result",
}
TRADE_FIELDS = ("timing", "cost_breakdown")
ACCOUNT_FIELDS = ("result_kind", "cost_model_id", "start_equity", "R", "D", "equity_end", "days")
TOP_FIELDS = ("output_contract", "label", "result_kind", "cost_model_id", "aggregation", "G", "D", "accounts",
              "verdict_axes", "run_record", "mask")
IDENTITY_TOL = 1e-9


def _accounting_axis(accounts: dict) -> dict:
    worst, n = 0.0, 0
    for acc in accounts.values():
        for d in acc["days"]:
            for t in d["trades"]:
                worst = max(worst, abs(t["cost_breakdown"]["identity_residual"]) / max(1.0, t["notional"]))
                n += 1
    if n == 0:
        return {"status": NOT_EVALUATED, "reason": "no trades to check"}
    ok = worst <= IDENTITY_TOL
    return {"status": PASS if ok else FAIL, "reason": f"cost identity on {n} trades, max residual/notional {worst:.3g}",
            "trades_checked": n, "max_residual_over_notional": worst}


def build_full_output(arm: dict, *, label: str, run_record: dict, mask: dict) -> dict:
    """`arm` = a run_arm-shaped dict from the post-patch runner (segments of account_sim results)."""
    accounts = {}
    for name, acc in arm["segments"].items():
        accounts[name] = {
            "result_kind": acc["result_kind"], "cost_model_id": acc["cost_model_id"],
            "start_equity": R.START_EQUITY, "R": acc["R"], "D": acc["D"], "equity_end": acc["equity_end"],
            "account_halt": acc["account_halt"],
            "days": [{k: d[k] for k in ("date", "situations", "cost_model_id", "equity_start", "pnl", "lock",
                                        "blocked_entries", "account_halt") if k in d}
                     | {"account_trace": d.get("account_trace"), "trades": d["trades"]} for d in acc["days"]]}
    axes = verdict_axes("ENGINE_VALIDATION", accounting=_accounting_axis(accounts))
    return {"output_contract": OUTPUT_CONTRACT_VERSION, "label": label, "result_kind": arm["result_kind"],
            "cost_model_id": arm["cost_model_id"], "policy": arm["policy"], "delay": arm["delay"],
            "aggregation": AGGREGATION, "G": arm["G"], "D": arm["D"], "accounts": accounts,
            "verdict_axes": axes, "run_record": run_record, "mask": mask}


def dumps(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n"


def loads(text: str) -> dict:
    return json.loads(text)


def missing_fields(doc: dict) -> list[str]:
    """Contract fields absent after a round trip (empty list = complete)."""
    miss = [f"top.{k}" for k in TOP_FIELDS if k not in doc]
    for name, acc in doc.get("accounts", {}).items():
        miss += [f"{name}.{k}" for k in ACCOUNT_FIELDS if k not in acc]
        for d in acc.get("days", []):
            for i, t in enumerate(d.get("trades", [])):
                miss += [f"{name}.{d['date']}.trade{i}.{k}" for k in TRADE_FIELDS if k not in t]
    return miss
