"""§7 pre-run count: morning entry candidates and stand-alone size constraints only (PREREG_DAYTRADE_V1_3_SIZING §7).

Per scored day this runs v1.0's morning decision exactly as run_day does (preopen inputs, S0, S1/S2 at the decision
time, else S3) and the entry check of each arm (`day.open_trade`, start equity, nothing open) at the decision bar
open. It never walks exits, never reads a price path after the decision bar except the S5 condition (14:44 QQQ close
vs prior close, `classify_s5`, condition only), and outputs counts only: no price, stop distance, size or P&L.
Same-direction room, cash limit and locks depend on whether the morning position is alive and are not counted here.
"""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    data_quality,
    decision_time,
    preopen_inputs,
    window,
)
from src.daytrade_paper.situation import (
    classify_open,
    classify_s3,
    classify_s5,
    opening_signals,
)
from src.daytrade_v11.data import SegmentData
from src.daytrade_v13.day import open_trade, validate_prices
from src.daytrade_v13.policy import BASE_COSTS, V10_PLUS, V13, V13_CAP
from src.daytrade_v13.runner import segment_days
from src.daytrade_v13.stats import MIN_RUN, runs_from_mask

MORNING = ("S1", "S2", "S3")
DIRECTIONS = (R.LONG, R.SHORT)
RISK_MODES = ("NORMAL", "HIGH", "UNKNOWN_TREATED_AS_HIGH")


class CountInvariantError(RuntimeError):
    """A masked day behaved like an unmasked one, or the arms differ for a reason other than the stop cap."""


def morning_candidate(day: str, regular: dict[str, list[Bar]], qqq_pre: list[Bar], prior: list[PriorSession],
                      external: dict, events: dict, *, prev_times: dict[str, set[int]], expected_prev: str,
                      prev_early_close: bool = False) -> dict:
    """Admission of the v1.0 morning signal under each arm; booleans and labels only."""
    q = data_quality(regular, prior, bool(events.get("early_close")), decision_time(events), prev_times,
                     expected_prev, prev_early_close)
    if not q["ok"]:
        raise CountInvariantError(f"{day}: masked day fails data quality: {q['issues']}")
    validate_prices(regular, qqq_pre, prior)
    qqq = regular[R.SIGNAL]
    inputs = preopen_inputs(day, prior, qqq_pre, qqq, external)
    out = {"label": None, "symbol": None, "risk_mode": inputs["risk_mode"], "s0": False,
           "admit": {}, "cap70_binds": None, "s5": None}
    if events.get("fomc") or events.get("early_close"):
        out["s0"] = True
        return out
    decision_t = decision_time(events)
    op = classify_open(inputs, opening_signals(inputs, qqq, prior, decision_t))
    if op["label"]:
        label, t = op["label"], decision_t
        sym = R.LONG if label == "S1" else R.SHORT
        stop_raw = min(b.low for b in window(regular[sym], R.OPEN, t))
    else:
        t = R.DECISION_INDICATOR_DAY if events.get("indicator") else R.S3_DECISION
        if not classify_s3(inputs, qqq, t)["label"]:
            label = None
        else:
            label, sym = "S3", R.LONG
            stop_raw = min(b.low for b in window(regular[sym], R.OPEN, t)) * (1 - R.S3_STOP_BUFFER)
    s5 = classify_s5(inputs, qqq)
    out["s5"] = s5["symbol"]
    if label is None:
        return out
    out["label"], out["symbol"] = label, sym
    bar = next(b for b in regular[sym] if b.t == t)
    for pol in (V10_PLUS, V13_CAP, V13):
        trade, why = open_trade(label, sym, t, bar, stop_raw, None, R.START_EQUITY, inputs["high"], pol,
                                BASE_COSTS, [])
        out["admit"][pol.name] = trade is not None
        if pol is V13 and trade is not None:
            out["cap70_binds"] = trade["sizing"]["single_cap_binds"]
        if pol is V10_PLUS and trade is None:
            out["v10_refusal_is_stop_cap"] = "상한" in why
    if out["admit"][V13_CAP.name] != out["admit"][V13.name]:
        raise CountInvariantError(f"{day}: v1.3-cap and v1.3 admit differently with nothing open")
    if out["admit"][V13_CAP.name] and not out["admit"][V10_PLUS.name] and not out["v10_refusal_is_stop_cap"]:
        raise CountInvariantError(f"{day}: v1.0 refused for a reason other than the 1.5% cap but v1.3-cap admits")
    return out


def _empty() -> dict:
    return {"days": 0, "s0_days": 0, "risk_mode": dict.fromkeys(RISK_MODES, 0),
            "morning_signal": dict.fromkeys(MORNING, 0),
            "v10_entry": dict.fromkeys(MORNING, 0), "v13cap_entry": dict.fromkeys(MORNING, 0),
            "v13cap_refused": dict.fromkeys(MORNING, 0),
            "new": {"days": 0, "by_signal": dict.fromkeys(MORNING, 0), "by_direction": dict.fromkeys(DIRECTIONS, 0),
                    "by_risk_mode": dict.fromkeys(RISK_MODES, 0), "inference": 0, "not_inference": 0,
                    "combo": {}},
            "cap70_binds_new": 0, "cap70_binds_all_v13_morning": 0,
            "s5_condition_days": 0, "s5_overlap_new": {"days": 0, "same_direction": 0, "opposite_direction": 0}}


def _add(acc: dict, r: dict, in_inference: bool) -> None:
    acc["days"] += 1
    acc["risk_mode"][r["risk_mode"]] += 1
    if r["s0"]:
        acc["s0_days"] += 1
        return
    acc["s5_condition_days"] += r["s5"] is not None
    label = r["label"]
    if label is None:
        return
    acc["morning_signal"][label] += 1
    v10, cap = r["admit"][V10_PLUS.name], r["admit"][V13_CAP.name]
    acc["v10_entry"][label] += v10
    acc["v13cap_entry"][label] += cap
    acc["v13cap_refused"][label] += not cap
    acc["cap70_binds_all_v13_morning"] += bool(r["cap70_binds"])
    if cap and not v10:
        new = acc["new"]
        new["days"] += 1
        new["by_signal"][label] += 1
        new["by_direction"][r["symbol"]] += 1
        new["by_risk_mode"][r["risk_mode"]] += 1
        new["inference" if in_inference else "not_inference"] += 1
        key = f"{label}/{r['symbol']}/{r['risk_mode']}/{'inference' if in_inference else 'not_inference'}"
        new["combo"][key] = new["combo"].get(key, 0) + 1
        acc["cap70_binds_new"] += bool(r["cap70_binds"])
        if r["s5"] is not None:
            acc["s5_overlap_new"]["days"] += 1
            acc["s5_overlap_new"]["same_direction" if r["s5"] == r["symbol"] else "opposite_direction"] += 1


def _merge(tot: dict, acc: dict) -> None:
    for k, v in acc.items():
        if isinstance(v, dict):
            _merge(tot.setdefault(k, {}), v)
        else:
            tot[k] = tot.get(k, 0) + v


def run_precount(mask: dict, datas: list[SegmentData], cal: dict, ext: dict) -> dict:
    out: dict = {"segments": {}, "total": _empty(), "equity_start": R.START_EQUITY,
                 "inference_rule": f"days in runs of consecutive scored sessions with n > {MIN_RUN}"}
    seg_days = segment_days(mask, datas, cal, ext)
    for name, days in seg_days.items():
        member = {day for run in runs_from_mask(mask["segments"][name]) if len(run) > MIN_RUN for day in run}
        acc = _empty()
        for d in days:
            r = morning_candidate(d["day"], d["regular"], d["qqq_pre"], d["prior"], d["external"], d["events"],
                                  prev_times=d["prev_times"], expected_prev=d["expected_prev"],
                                  prev_early_close=d["prev_early_close"])
            _add(acc, r, d["day"] in member)
        out["segments"][name] = acc
        _merge(out["total"], acc)
    return out
