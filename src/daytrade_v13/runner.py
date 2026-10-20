"""Segment accounts, operational checks and the §9 regression checks (PREREG_DAYTRADE_V1_3_SIZING §3, §5, §9).

Each segment (A, B) is an independent USD 2,000 account. Equity, the liquidation-value high-water mark and the
account-halt state carry from one scored day to the next inside a segment, across mask gaps; days after a halt are
zero-return observations. Day inputs come from `src.daytrade_v11.data` (prices via research_load only).
"""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.simulate import run_day
from src.daytrade_v11.data import SegmentData, day_context, day_inputs, external_for
from src.daytrade_v13.day import V10_TRADE_KEYS, AccountState, simulate_day
from src.daytrade_v13.policy import BASE_COSTS, Costs, Policy, cost_model_id

# Result kinds (cost contract v2). Only an account_sim re-runs the account (equity, sizing, cash limits, locks)
# under its cost model; a fixed_qty_diagnostic keeps another run's quantities and is never an account return.
ACCOUNT_SIM = "account_sim"
FIXED_QTY_DIAGNOSTIC = "fixed_qty_diagnostic"

INTENDED = "intended_difference"
MATCH = "match"
MISMATCH = "mismatch"
EXCLUDED = "excluded_account_halt"


def segment_days(mask: dict, datas: list[SegmentData], cal: dict, ext: dict) -> dict[str, list[dict]]:
    """Scored-day inputs per segment, in date order (the keyword set `simulate_day` / `run_day` take)."""
    out: dict[str, list[dict]] = {}
    for data in datas:
        pos = {d: i for i, d in enumerate(data.sessions)}
        days = []
        for day in mask["segments"][data.segment.name]["scored"]:
            ctx = day_context(cal, day)
            inp = day_inputs(data, pos[day])
            days.append({"day": day, "regular": inp["regular"], "qqq_pre": inp["qqq_pre"], "prior": inp["prior"],
                         "external": external_for(ext, day), "events": ctx["events"],
                         "prev_times": inp["prev_times"], "expected_prev": ctx["expected_prev"],
                         "prev_early_close": ctx["prev_early_close"]})
        out[data.segment.name] = days
    return out


def run_account(days: list[dict], policy: Policy, costs: Costs = BASE_COSTS, delay: int = 0) -> dict:
    """One segment account over its scored days. R = compounded return, D = max drawdown of bar-close
    liquidation values (v1.1 §6.4)."""
    acct = AccountState()
    results = [simulate_day(account=acct, policy=policy, costs=costs, delay=delay, **d) for d in days]
    return {"result_kind": ACCOUNT_SIM, "cost_model_id": cost_model_id(costs),
            "policy": policy.name, "delay": delay, "costs": vars(costs).copy(), "days": results,
            "R": acct.equity / R.START_EQUITY - 1, "D": acct.max_dd, "equity_end": acct.equity,
            "account_halt": acct.halt_event}


def run_arm(seg_days: dict[str, list[dict]], policy: Policy, costs: Costs = BASE_COSTS, delay: int = 0) -> dict:
    """Both segment accounts; G = (R_A + R_B) / 2, D = max(D_A, D_B)."""
    segs = {name: run_account(days, policy, costs, delay) for name, days in seg_days.items()}
    rs = [s["R"] for s in segs.values()]
    return {"result_kind": ACCOUNT_SIM, "cost_model_id": cost_model_id(costs),
            "policy": policy.name, "delay": delay, "costs": vars(costs).copy(), "segments": segs,
            "G": sum(rs) / len(rs) if rs else 0.0, "D": max((s["D"] for s in segs.values()), default=0.0)}


def fixed_qty_diagnostic(account: dict, costs: Costs) -> dict:
    """Re-price the trades of one `run_account` result under `costs`, keeping every quantity and raw price.

    Diagnostic only: equity, sizing, cash limits, locks and halts are NOT re-run, so the running balance is a
    cost sensitivity, not an account, and must not be reported as an account return or used for promotion. It may
    fall below zero; `min_running_balance` reports that instead of hiding it. The running balance adds trades in
    entry order (as recorded), not exit time, so on days with overlapping positions its minimum is approximate.
    """
    if account.get("result_kind") != ACCOUNT_SIM:
        raise ValueError("fixed_qty_diagnostic needs an account_sim result")
    balance = min_balance = R.START_EQUITY
    total, n = 0.0, 0
    for day in account["days"]:
        for tr in day["trades"]:
            q = tr["shares"]
            notional = q * tr["entry_raw"] * (1 + costs.slippage)
            proceeds = q * tr["exit_raw"] * (1 - costs.slippage)
            pnl = proceeds * (1 - costs.commission - costs.sec_fee) - notional * (1 + costs.commission)
            total += pnl
            balance += pnl
            min_balance = min(min_balance, balance)
            n += 1
    return {"result_kind": FIXED_QTY_DIAGNOSTIC, "not_an_account_return": True,
            "source_cost_model_id": account["cost_model_id"], "cost_model_id": cost_model_id(costs),
            "trades": n, "total_pnl": total, "end_balance": balance, "min_running_balance": min_balance,
            "went_below_zero": min_balance < 0}


# ---------------------------------------------------------------- §5 operational checks


def operational_violations(account: dict) -> list[str]:
    """Independent of the simulator's own enforcement: no new entry at/after a lock's effective minute that day,
    and none at/after an account halt's effective minute for the rest of the segment."""
    out: list[str] = []
    halt = account.get("account_halt")
    for d in account["days"]:
        lock = d.get("lock")
        for tr in d["trades"]:
            if lock is not None and tr["entry_t"] >= lock["effective_t"]:
                out.append(f"{d['date']} {tr['kind']} entered {tr['entry_time']} after daily lock {lock['time']}")
            if halt is not None and (d["date"] > halt["date"] or
                                     (d["date"] == halt["date"] and tr["entry_t"] >= halt["effective_t"])):
                out.append(f"{d['date']} {tr['kind']} entered {tr['entry_time']} after account halt "
                           f"{halt['date']} {halt['time']}")
    return out


def arm_violations(arm: dict) -> list[str]:
    return [f"{arm['policy']} {name}: {v}" for name, acc in arm["segments"].items()
            for v in operational_violations(acc)]


# ---------------------------------------------------------------- §9 regression checks


def _v10_view(trades: list[dict]) -> list[dict]:
    return [{k: tr[k] for k in V10_TRADE_KEYS} for tr in trades]


def classify_regression(v13_day: dict, v10_day: dict) -> str:
    """§9 test ①: a `v1.0+` day vs v1.0 run_day from the same start equity.

    match: same trades (every v1.0 field) and day P&L. intended_difference: the v1.0+ day was locked by the close
    check (§3 step 7) before 14:45, v1.0's own 14:45 check did not trigger, and the only difference is the S5 trade
    v1.0 took. Anything else is a mismatch. Account-halt days are excluded by the caller.
    """
    ours, theirs = _v10_view(v13_day["trades"]), _v10_view(v10_day["trades"])
    if ours == theirs and v13_day["pnl"]["day_pnl"] == v10_day["pnl"]["day_pnl"]:
        return MATCH
    lock = v13_day.get("lock")
    if (lock is not None and lock["source"] == "close" and lock["effective_t"] <= R.S5_DECISION
            and not v10_day["halt"]["triggered"]
            and theirs[-1:] and theirs[-1]["kind"] == "S5" and ours == theirs[:-1]):
        return INTENDED
    return MISMATCH


def regression_one(seg_days: dict[str, list[dict]], v10plus: dict) -> dict:
    """Run v1.0 run_day on every scored day from the v1.0+ start equity and classify (§9 ①)."""
    counts = {MATCH: 0, INTENDED: 0, MISMATCH: 0, EXCLUDED: 0}
    details: list[dict] = []
    for name, days in seg_days.items():
        for d, ours in zip(days, v10plus["segments"][name]["days"], strict=True):
            if ours["halted_at_start"] or ours["account_halt"] is not None:
                counts[EXCLUDED] += 1
                continue
            theirs = run_day(d["day"], d["regular"], d["qqq_pre"], d["prior"], d["external"], d["events"],
                             ours["equity_start"], prev_times=d["prev_times"], expected_prev=d["expected_prev"],
                             prev_early_close=d["prev_early_close"])
            c = classify_regression(ours, theirs)
            counts[c] += 1
            if c != MATCH:
                details.append({"segment": name, "date": d["day"], "class": c,
                                "lock": ours["lock"] and {k: ours["lock"][k] for k in ("source", "time")}})
    return {"counts": counts, "non_match": details}


def regression_two(capped: dict, v10plus: dict) -> dict:
    """§9 test ②: v1.3-cap with the 1.5% refusal restored equals v1.0+ on every scored day (trades and returns)."""
    bad: list[str] = []
    n = 0
    for name, acc in v10plus["segments"].items():
        for a, b in zip(capped["segments"][name]["days"], acc["days"], strict=True):
            n += 1
            if _v10_view(a["trades"]) != _v10_view(b["trades"]) or a["pnl"] != b["pnl"]:
                bad.append(f"{name} {a['date']}")
    return {"days": n, "mismatch_days": bad}
