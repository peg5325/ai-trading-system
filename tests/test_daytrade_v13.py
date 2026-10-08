"""v1.3 sizing and risk controls (PREREG_DAYTRADE_V1_3_SIZING). Synthetic bars and tmp databases only."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
from itertools import pairwise

import numpy as np
import pytest
from test_daytrade_paper import (
    EXT_OK,
    NO_EVENTS,
    PREV,
    PRIOR,
    QC,
    T,
    s1_day,
    tqqq_default,
)
from test_daytrade_shadow import SCENARIOS, s2_day, s3_day, vwap_exit_day
from test_daytrade_v11_precount import CAL, ROOT, _csvs, _facts_db

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar
from src.daytrade_paper.simulate import run_day
from src.daytrade_v11 import data as V
from src.daytrade_v13 import precount as P
from src.daytrade_v13.day import (
    V10_TRADE_KEYS,
    AccountState,
    InvalidRun,
    open_trade,
    simulate_day,
)
from src.daytrade_v13.policy import (
    BASE_COSTS,
    V10_EXACT,
    V10_PLUS,
    V13,
    V13_CAP,
    size,
    with_stop_cap,
)
from src.daytrade_v13.report import arm_summary, decompose_d, not_double
from src.daytrade_v13.runner import (
    INTENDED,
    MATCH,
    MISMATCH,
    classify_regression,
    operational_violations,
    regression_one,
    regression_two,
    run_account,
    run_arm,
)
from src.daytrade_v13.stats import (
    bootstrap_indices,
    bootstrap_test,
    d_by_runs,
    daily_d,
    inference_mean,
    runs_from_mask,
    verdict,
)

EXT_HIGH = {"vix": {"value": 35.0}, "vix9d": {"value": 30.0}, "gex": {"value": 1.0e9}}
DAY = "2026-10-05"


def sim(build_or_reg, pol, ext=EXT_OK, ev=NO_EVENTS, acct=None, costs=BASE_COSTS, delay=0):
    reg, pre = build_or_reg() if callable(build_or_reg) else build_or_reg
    acct = acct if acct is not None else AccountState()
    return simulate_day(DAY, reg, pre, PRIOR, ext, ev, acct, pol, costs, delay, **QC)


def v10(trades):
    return [{k: t[k] for k in V10_TRADE_KEYS} for t in trades]


def day_input(build, ev=NO_EVENTS, ext=EXT_OK):
    reg, pre = build()
    return {"day": DAY, "regular": reg, "qqq_pre": pre, "prior": PRIOR, "external": ext, "events": ev,
            "prev_times": QC["prev_times"], "expected_prev": QC["expected_prev"], "prev_early_close": False}


def with_bar(reg, sym, t, o, h, lo, c):
    reg[sym] = [b if b.t != t else Bar(t, o, h, lo, c, b.volume) for b in reg[sym]]
    return reg


def wide_stop_day(**kw):
    """Opening low 45.25 vs entry 50.3 -> stop distance about 10% (v1.0 refuses, v1.3 admits)."""
    return s1_day(tqqq_fn=lambda t: 45.3 if t == T(9, 31) else tqqq_default(t), **kw)


def by_kind(out):
    return {t["kind"]: t for t in out["trades"]}


# ---------------------------------------------------------------- §9 test ①: v1.0+ / v1.0 = run_day


EXTRA = {
    "S1_high_plus_S5_same": (lambda: s1_day(qqq_after=lambda t: PREV * 1.03), NO_EVENTS, EXT_HIGH),
    "S1_plus_S5_opposite": (lambda: s1_day(qqq_after=lambda t: PREV * 0.97), NO_EVENTS, EXT_OK),
    "S1_high_plus_S5_opposite": (lambda: s1_day(qqq_after=lambda t: PREV * 0.97), NO_EVENTS, EXT_HIGH),
    "S1_wide_stop_plus_S5": (lambda: wide_stop_day(qqq_after=lambda t: PREV * 1.03), NO_EVENTS, EXT_OK),
    "S2_indicator": (s2_day, {**NO_EVENTS, "indicator": True, "types": ["CPI"]}, EXT_OK),
}
ALL_DAYS = {**SCENARIOS, **EXTRA}


@pytest.mark.parametrize("name", sorted(ALL_DAYS))
@pytest.mark.parametrize("pol", [V10_EXACT, V10_PLUS], ids=lambda p: p.name)
def test_v10_arms_reproduce_run_day(name, pol):
    build, ev, ext = ALL_DAYS[name]
    reg, pre = build()
    ref = run_day(DAY, reg, pre, PRIOR, ext, ev, 2000.0, **QC)
    out = sim((reg, pre), pol, ext, ev)
    assert v10(out["trades"]) == v10(ref["trades"])  # every v1.0 field, exact floats
    assert out["pnl"]["day_pnl"] == ref["pnl"]["day_pnl"]
    assert out["situations"] == ref["situations"]
    if ref["halt"].get("limit") is not None:
        h = out["check_1445"]
        assert (h["triggered"], h["day_pnl_at_check"], h["limit"]) == (
            ref["halt"]["triggered"], ref["halt"]["day_pnl_at_check"], ref["halt"]["limit"])
    assert classify_regression(out, ref) == MATCH


def test_v10_arms_reproduce_run_day_from_another_start_equity():
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03)
    ref = run_day(DAY, reg, pre, PRIOR, EXT_OK, NO_EVENTS, 1733.5, **QC)
    out = sim((reg, pre), V10_PLUS, acct=AccountState(equity=1733.5, hwm=2000.0))
    assert v10(out["trades"]) == v10(ref["trades"]) and out["pnl"]["day_pnl"] == ref["pnl"]["day_pnl"]


def test_close_lock_on_a_realized_gap_loss_still_matches_v10():
    """halt_day: the 10:00 gap stop is realized, so the close check (10:00) and v1.0's 14:45 check agree."""
    build, ev, ext = SCENARIOS["S1_halt_blocks_S5"]
    reg, pre = build()
    out = sim((reg, pre), V10_PLUS)
    ref = run_day(DAY, reg, pre, PRIOR, ext, ev, 2000.0, **QC)
    assert (out["lock"]["source"], out["lock"]["time"], out["lock"]["effective_t"]) == ("close", "10:00", T(10, 1))
    assert ref["halt"]["triggered"] and classify_regression(out, ref) == MATCH


def test_classify_regression_intended_difference_and_mismatch():
    """With v1.0 sizing a pre-14:45 close lock comes from a realized gap loss that v1.0 also sees, so the intended
    difference is exercised on constructed records: the lock removed exactly the S5 trade v1.0 took."""
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03)
    ref = run_day(DAY, reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    ours = sim((reg, pre), V10_PLUS)
    assert [t["kind"] for t in ref["trades"]] == ["S1", "S5"]
    locked = copy.deepcopy(ours)
    locked["trades"] = locked["trades"][:1]
    locked["pnl"]["day_pnl"] = locked["trades"][0]["pnl"]
    locked["lock"] = {"source": "close", "t": T(11, 0), "time": "11:00", "effective_t": T(11, 1), "day_pnl": -61.0}
    assert classify_regression(locked, ref) == INTENDED
    pre_entry = copy.deepcopy(locked)
    pre_entry["lock"]["source"] = "pre_entry"
    assert classify_regression(pre_entry, ref) == MISMATCH
    late = copy.deepcopy(locked)
    late["lock"]["effective_t"] = T(14, 46)
    assert classify_regression(late, ref) == MISMATCH
    v10_halted = copy.deepcopy(ref)
    v10_halted["halt"]["triggered"] = True
    assert classify_regression(locked, v10_halted) == MISMATCH
    other = copy.deepcopy(locked)
    other["trades"][0]["notional"] += 1.0
    assert classify_regression(other, ref) == MISMATCH


def test_regression_one_over_a_segment_account():
    names = sorted(ALL_DAYS)
    seg = {"A": [day_input(*ALL_DAYS[n]) for n in names]}
    plus = run_arm(seg, V10_PLUS)
    out = regression_one(seg, plus)
    assert out["counts"][MATCH] == len(names) and out["counts"][MISMATCH] == 0 and out["non_match"] == []
    # equity chains: each day starts where the previous ended
    days = plus["segments"]["A"]["days"]
    assert all(b["equity_start"] == a["pnl"]["equity_end"] for a, b in pairwise(days))


# ---------------------------------------------------------------- §9 test ②: v1.3-cap with the 1.5% cap = v1.0+


def test_v13cap_with_stop_cap_restored_equals_v10plus_on_every_day():
    names = sorted(ALL_DAYS)
    seg = {"A": [day_input(*ALL_DAYS[n]) for n in names], "B": [day_input(*ALL_DAYS[n]) for n in reversed(names)]}
    plus = run_arm(seg, V10_PLUS)
    capped = run_arm(seg, with_stop_cap(V13_CAP))
    out = regression_two(capped, plus)
    assert out == {"days": 2 * len(names), "mismatch_days": []}
    assert capped["G"] == plus["G"] and capped["D"] == plus["D"]
    # without the cap v1.3-cap differs exactly on the wide-stop days
    uncapped = regression_two(run_arm(seg, V13_CAP), plus)
    assert {d.split()[1] for d in uncapped["mismatch_days"]} == {DAY}
    assert len(uncapped["mismatch_days"]) >= 2


# ---------------------------------------------------------------- §2 sizing: worked examples


def _open(sym, notional, comm=R.COMMISSION):
    return {"symbol": sym, "notional": notional, "buy_commission": notional * comm}


def test_prereg_worked_examples_normal_mode():
    s1 = size(V13, BASE_COSTS, 2000.0, 0.015, False, R.LONG, [])
    assert s1["notional"] == pytest.approx(1333.33, abs=0.01) and s1["binding"] == ["base"]
    s5 = size(V13, BASE_COSTS, 2000.0, 0.015, False, R.SHORT, [_open(R.LONG, s1["notional"])])
    assert s5["limits"]["base"] == pytest.approx(1333.33, abs=0.01)
    assert s5["notional"] == pytest.approx(664.7, abs=0.05) and s5["binding"] == ["cash"]
    assert s5["notional"] == pytest.approx((2000 - 2000 / 1.5 - 2000 / 1.5 * 0.001) / 1.001)


def test_prereg_worked_examples_high_mode():
    s1 = size(V13, BASE_COSTS, 2000.0, 0.015, True, R.LONG, [])
    assert s1["notional"] == pytest.approx(666.67, abs=0.01)
    held = [_open(R.LONG, s1["notional"])]
    opp = size(V13, BASE_COSTS, 2000.0, 0.015, True, R.SHORT, held)
    assert opp["notional"] == pytest.approx(332.3, abs=0.05) and opp["binding"] == ["high_entry_budget"]
    same = size(V13, BASE_COSTS, 2000.0, 0.015, True, R.LONG, held)
    assert same["limits"]["same_dir_room"] == pytest.approx(333.33, abs=0.01)
    assert same["notional"] == pytest.approx(332.3, abs=0.05) and same["binding"] == ["high_entry_budget"]


def test_cash_limits_never_bind_at_half_percent():
    """§4: at 0.5% steps 5-6 cannot bind (that is why test ② holds)."""
    for high in (False, True):
        s1 = size(V13_CAP, BASE_COSTS, 2000.0, 0.0001, high, R.LONG, [])  # single cap binds
        held = [_open(R.LONG, s1["notional"])]
        for sym in (R.LONG, R.SHORT):
            s5 = size(V13_CAP, BASE_COSTS, 2000.0, 0.015, high, sym, held)
            assert not set(s5["binding"]) & {"cash", "high_entry_budget"}


# ---------------------------------------------------------------- §9 test ③: synthetic cases


def test_ten_percent_stop():
    plus, cap, full = (sim(lambda: wide_stop_day(), p) for p in (V10_PLUS, V13_CAP, V13))
    assert plus["trades"] == [] and any("1.5% 상한" in x for x in plus["no_trade_reasons"])
    tc, tf = cap["trades"][0], full["trades"][0]
    assert tc["stop_frac"] == pytest.approx(0.10, abs=0.002) and tc["stop"] == pytest.approx(45.25)
    assert tc["notional"] == pytest.approx(2000 * 0.005 / tc["stop_frac"])
    assert tf["notional"] == pytest.approx(2000 * 0.01 / tf["stop_frac"]) == pytest.approx(2 * tc["notional"])
    assert tc["sizing"]["binding"] == ["base"] and not tf["sizing"]["single_cap_binds"]


def test_cash_limit_with_commissions_opposite_s5():
    out = sim(lambda: s1_day(qqq_after=lambda t: PREV * 0.97), V13)
    s1, s5 = by_kind(out)["S1"], by_kind(out)["S5"]
    assert s1["notional"] == pytest.approx(1400.0) and s5["symbol"] == R.SHORT
    assert s5["notional"] == pytest.approx((2000 - 1400 - 1.4) / 1.001)
    assert s5["sizing"]["binding"] == ["cash"]
    cash_after = 2000 - s1["notional"] - s1["buy_commission"] - s5["notional"] - s5["buy_commission"]
    assert cash_after == pytest.approx(0.0, abs=1e-9)


def test_high_opposite_s5_clipped_by_entry_budget():
    out = sim(lambda: s1_day(qqq_after=lambda t: PREV * 0.97), V13, ext=EXT_HIGH)
    s1, s5 = by_kind(out)["S1"], by_kind(out)["S5"]
    assert s1["notional"] == pytest.approx(700.0)
    assert s5["sizing"]["limits"]["base"] == pytest.approx(2000 / 1.5 / 2)
    assert s5["notional"] == pytest.approx((1000 - 700 - 0.7) / 1.001) and s5["sizing"]["binding"] == ["high_entry_budget"]


def test_high_same_direction_combined_cap():
    full = by_kind(sim(lambda: s1_day(qqq_after=lambda t: PREV * 1.03), V13, ext=EXT_HIGH))["S5"]
    assert full["sizing"]["limits"]["same_dir_room"] == pytest.approx(2000 * 1.0 * 0.5 - 700)
    assert full["notional"] == pytest.approx((1000 - 700 - 0.7) / 1.001)  # budget 299.0 < room 300
    cap = by_kind(sim(lambda: s1_day(qqq_after=lambda t: PREV * 1.03), V13_CAP, ext=EXT_HIGH))
    assert cap["S1"]["notional"] == pytest.approx(350.0)
    assert cap["S5"]["notional"] == pytest.approx(2000 * 0.5 * 0.5 - 350) and cap["S5"]["sizing"]["binding"] == [
        "same_dir_room"]


def _gap_then_recover(gap_t):
    def tq(t):
        if t == gap_t:
            return 45.0
        return tqqq_default(t)
    return s1_day(qqq_after=lambda t: PREV * 1.03, tqqq_fn=tq)


def test_daily_lock_after_intraday_loss_then_recovery_blocks_s5():
    """11:00 gap through the stop realizes a > 3% loss; TQQQ recovers from 11:01; the close check locks the day
    from 11:01 and the 14:45 S5 is refused."""
    out = sim(lambda: _gap_then_recover(T(11, 0)), V13)
    assert [(t["kind"], t["exit_reason"], t["exit_time"]) for t in out["trades"]] == [("S1", "STOP_GAP", "11:00")]
    assert out["pnl"]["day_pnl"] <= -0.03 * 2000
    lock = out["lock"]
    assert (lock["source"], lock["time"], lock["effective_t"]) == ("close", "11:00", T(11, 1))
    assert lock["overshoot_at_trigger"] > 0 and lock["overshoot_final"] == lock["overshoot_at_trigger"]
    assert [b["kind"] for b in out["blocked_entries"]] == ["S5"] and "하루 잠금" in out["blocked_entries"][0]["why"]
    assert out["check_1445"]["triggered"] is True
    assert operational_violations({"days": [out], "account_halt": None}) == []


def test_1445_open_gap_stop_blocks_s5():
    def tq(t):
        return 45.0 if t >= T(14, 45) else tqqq_default(t)
    build = lambda: s1_day(qqq_after=lambda t: PREV * 1.03, tqqq_fn=tq)
    for pol in (V10_PLUS, V13_CAP, V13):
        out = sim(build, pol)
        assert [(t["kind"], t["exit_reason"], t["exit_time"]) for t in out["trades"]] == [("S1", "STOP_GAP", "14:45")]
        assert (out["lock"]["source"], out["lock"]["time"]) == ("pre_entry", "14:45")
        assert [b["kind"] for b in out["blocked_entries"]] == ["S5"]
    reg, pre = build()
    ref = run_day(DAY, reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert classify_regression(sim((reg, pre), V10_PLUS), ref) == MATCH and ref["halt"]["triggered"]


def _halt_day():
    """S1 with a ~3% stop; QQQ +3% (S5 TQQQ); TQQQ closes 14:45 at 49.1 (above the stop) and stays there."""
    def tq(t):
        if t == T(9, 31):
            return 48.85
        return 49.1 if t > T(14, 45) else tqqq_default(t)
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03, tqqq_fn=tq)
    return with_bar(reg, "TQQQ", T(14, 45), 50.3, 50.35, 49.05, 49.1), pre


@pytest.mark.parametrize("delay", [0, 1])
def test_account_halt_lifecycle(delay):
    acct = AccountState(equity=1310.0, hwm=2000.0)  # 10 above the 0.65 x 2000 line
    out = sim(_halt_day, V13, acct=acct, delay=delay)
    halt = out["account_halt"]
    assert (halt["time"], halt["effective_t"]) == ("14:45", T(14, 46)) and acct.halted
    assert halt["liquidation_value"] <= 0.65 * 2000 < 1310.0
    s1 = by_kind(out)["S1"]
    assert (s1["exit_reason"], s1["exit_time"], s1["exit_raw"]) == ("ACCOUNT_HALT", "14:46", 49.1)
    assert out["lock"] is None  # far from -3% of the day
    if delay:  # the S5 signalled at 14:45 waits for 14:46 and is cancelled by the halt
        assert [t["kind"] for t in out["trades"]] == ["S1"] and s1["entry_time"] == "09:46"
        assert [(b["kind"], b["why"]) for b in out["blocked_entries"]] == [("S5", "계좌 중단: 대기 진입 주문 취소")]
    else:  # S5 entered at 14:45 before the close check; stopped inside the same bar
        assert [t["kind"] for t in out["trades"]] == ["S1", "S5"] and by_kind(out)["S5"]["exit_reason"] == "STOP"
    # later days of the segment: no entries, zero-return observations, state carried
    nxt = sim(lambda: s1_day(qqq_after=lambda t: PREV * 1.03), V13, acct=acct, delay=delay)
    assert nxt["trades"] == [] and nxt["pnl"]["day_return"] == 0.0 and nxt["situations"] == ["ACCOUNT_HALTED"]
    assert nxt["equity_start"] == out["pnl"]["equity_end"] == acct.equity
    assert operational_violations({"days": [out, nxt], "account_halt": acct.halt_event}) == []
    bad = copy.deepcopy(nxt)
    bad["date"], bad["trades"] = "2026-10-06", out["trades"][:1]
    assert operational_violations({"days": [out, bad], "account_halt": acct.halt_event})


def test_account_halt_carries_across_days_in_run_account():
    """High-water mark and halt state chain through a segment; v1.3 loses > 35% over gap-stop days."""
    crash = lambda: s1_day(tqqq_fn=lambda t: 15.0 if t >= T(10, 0) else tqqq_default(t))
    seg = [day_input(crash), day_input(s1_day), day_input(s1_day)]
    acc = run_account(seg, V13)
    first, second, third = acc["days"]
    assert first["account_halt"] is not None and acc["account_halt"]["date"] == DAY
    assert second["situations"] == third["situations"] == ["ACCOUNT_HALTED"]
    assert second["pnl"]["day_return"] == third["pnl"]["day_return"] == 0.0
    assert acc["D"] >= 0.35 and acc["R"] == pytest.approx(first["pnl"]["day_return"])
    assert operational_violations(acc) == []


def test_invalid_prices_invalidate_the_run():
    for bad in (float("nan"), 0.0, -1.0, float("inf")):
        reg, pre = s1_day()
        reg = with_bar(reg, "TQQQ", T(12, 0), bad, 50.3, 50.3, 50.3)
        with pytest.raises(InvalidRun, match="finite positive"):
            sim((reg, pre), V13)
    reg, pre = s1_day()
    reg["SQQQ"] = [Bar(b.t, b.open, b.high, b.low, b.close, -1.0) if b.t == T(10, 0) else b for b in reg["SQQQ"]]
    with pytest.raises(InvalidRun, match="volume"):
        sim((reg, pre), V13)
    reg, pre = s1_day()
    reg["TQQQ"] = [b for b in reg["TQQQ"] if b.t != T(12, 0)]
    with pytest.raises(InvalidRun, match="data quality"):
        sim((reg, pre), V13)


def test_entry_refuses_stop_outside_zero_and_fill():
    bar = Bar(T(9, 45), 50.0, 50.0, 50.0, 50.0, 1.0)
    trade, why = open_trade("S1", R.LONG, T(9, 45), bar, -1.0, None, 2000.0, False, V13, BASE_COSTS, [])
    assert trade is None and "0 < 손절가" in why
    trade, why = open_trade("S5", R.LONG, T(14, 45), bar, None, 1.5, 2000.0, False, V13, BASE_COSTS, [])
    assert trade is None and "0 < 손절가" in why
    trade, why = open_trade("S1", R.LONG, T(9, 45), bar, 50.0, None, 2000.0, False, V13, BASE_COSTS, [])
    assert trade is None and "진입 시가가 이미 손절가 아래" in why
    with pytest.raises(InvalidRun):
        open_trade("S1", R.LONG, T(9, 45), Bar(T(9, 45), math.nan, 1, 1, 1, 1), 49.0, None, 2000.0, False, V13,
                   BASE_COSTS, [])


def test_cost_stress_uses_the_run_commission_rate():
    costs = BASE_COSTS.scaled(1.5)
    assert (costs.commission, costs.slippage, costs.sec_fee) == pytest.approx((0.0015, 0.00075, 0.0000309))
    out = sim(lambda: s1_day(qqq_after=lambda t: PREV * 0.97), V13, costs=costs)
    s1, s5 = by_kind(out)["S1"], by_kind(out)["S5"]
    assert s1["entry_fill"] == pytest.approx(50.3 * 1.00075) and s1["buy_commission"] == pytest.approx(1400 * 0.0015)
    assert s5["notional"] == pytest.approx((2000 - 1400 - 2.1) / 1.0015) and s5["sizing"]["binding"] == ["cash"]
    assert s1["sell_commission"] == pytest.approx(s1["proceeds"] * 0.0015)
    assert s1["sec_fee"] == pytest.approx(s1["proceeds"] * 0.0000309)
    base = by_kind(sim(lambda: s1_day(qqq_after=lambda t: PREV * 0.97), V13))["S1"]
    assert s1["pnl"] < base["pnl"]


# ---------------------------------------------------------------- stress (b): one-minute delay


def test_delay_moves_entries_one_bar_and_resizes_at_the_fill():
    out = sim(lambda: s1_day(qqq_after=lambda t: PREV * 1.03), V10_PLUS, delay=1)
    s1, s5 = by_kind(out)["S1"], by_kind(out)["S5"]
    assert (s1["entry_time"], s1["signal_t"], s5["entry_time"], s5["signal_t"]) == ("09:46", T(9, 45), "14:46",
                                                                                     T(14, 45))
    assert s5["stop"] == pytest.approx(s5["entry_fill"] * (1 - 0.015))


def test_delay_rechecks_stop_cap_and_open_at_the_fill():
    def jump(t):
        return 51.5 if t == T(9, 46) else tqqq_default(t)
    plus = sim(lambda: s1_day(tqqq_fn=jump), V10_PLUS, delay=1)
    assert plus["trades"] == [] and any("1.5% 상한" in b["why"] for b in plus["blocked_entries"])
    assert sim(lambda: s1_day(tqqq_fn=jump), V10_PLUS)["trades"]  # no delay: entered at 09:45
    assert sim(lambda: s1_day(tqqq_fn=jump), V13_CAP, delay=1)["trades"][0]["entry_raw"] == 51.5

    def through(t):
        return 49.9 if t == T(9, 46) else tqqq_default(t)
    for pol in (V10_PLUS, V13):
        out = sim(lambda: s1_day(tqqq_fn=through), pol, delay=1)
        assert out["trades"] == [] and "진입 시가가 이미 손절가 아래" in out["blocked_entries"][0]["why"]


def test_delay_moves_signal_exits_but_not_stops_or_the_forced_exit():
    assert by_kind(sim(vwap_exit_day, V10_PLUS))["S1"]["exit_time"] == "10:35"
    late = by_kind(sim(vwap_exit_day, V10_PLUS, delay=1))["S1"]
    assert (late["exit_time"], late["exit_reason"]) == ("10:36", "VWAP_5M")
    gap = lambda: s1_day(tqqq_fn=lambda t: tqqq_default(t) if t < T(11, 0) else 49.0)
    for d in (0, 1):
        assert (by_kind(sim(gap, V10_PLUS, delay=d))["S1"]["exit_time"],
                by_kind(sim(gap, V10_PLUS, delay=d))["S1"]["exit_reason"]) == ("11:00", "STOP_GAP")
        forced = by_kind(sim(s1_day, V10_PLUS, delay=d))["S1"]
        assert (forced["exit_time"], forced["exit_reason"]) == ("15:55", "FORCED_1555")


def test_delay_pending_exit_cancelled_by_a_stop_in_the_waiting_bar():
    def build():
        reg, pre = vwap_exit_day()
        stop = min(b.low for b in reg["TQQQ"] if R.OPEN <= b.t < R.DECISION)
        return with_bar(reg, "TQQQ", T(10, 35), 50.12, 50.13, stop - 0.5, 50.12), pre
    assert by_kind(sim(build, V10_PLUS))["S1"]["exit_reason"] == "VWAP_5M"  # no delay: out at the 10:35 open
    late = by_kind(sim(build, V10_PLUS, delay=1))["S1"]
    assert (late["exit_time"], late["exit_reason"]) == ("10:35", "STOP")


# ---------------------------------------------------------------- statistics (v1.1 §6.4)


def _mask_segment(pattern: str) -> dict:
    """'x' = scored, '.' = excluded, one char per consecutive session date."""
    days = [f"2024-01-{i + 1:02d}" for i in range(len(pattern))]
    return {"scored": [d for d, c in zip(days, pattern) if c == "x"],
            "excluded": [{"date": d} for d, c in zip(days, pattern) if c == "."]}


def test_runs_break_at_excluded_days():
    seg = _mask_segment("xxx.xxxxxx..xx")
    assert [len(r) for r in runs_from_mask(seg)] == [3, 6, 2]
    assert [len(r) for r in runs_from_mask(_mask_segment(".xxxxxxx"))] == [7]


def test_bootstrap_indices_are_circular_blocks_inside_each_run():
    idx = bootstrap_indices([7, 12], b=50, seed=1)
    assert idx.shape == (50, 19)
    first, second = idx[:, :7], idx[:, 7:] - 7
    assert first.min() >= 0 and first.max() < 7 and second.min() >= 0 and second.max() < 12
    for row in second:
        for k in range(0, 12, 5):
            blk = row[k:k + 5]
            assert all((b - a) % 12 == 1 for a, b in pairwise(blk))


def test_bootstrap_is_reproducible_with_the_fixed_seed():
    rng = np.random.default_rng(3)
    runs = [list(rng.normal(0.001, 0.01, 40)), list(rng.normal(0.001, 0.01, 30)), [0.5, 0.5]]
    a, b = bootstrap_test(runs), bootstrap_test(runs)
    assert a == b and a["inference_days"] == 70 and a["inference_runs"] == 2
    assert a["m"] == pytest.approx(np.mean(runs[0] + runs[1]))  # the short run is not tested
    assert 0 < a["p"] <= 1 and a["ci95"][0] < a["m"] < a["ci95"][1]
    assert bootstrap_test(runs, seed=4)["p"] != a["p"]


def test_bootstrap_p_formula():
    runs = [[0.01, 0.02, -0.005, 0.03, 0.0, 0.015, 0.01]]
    out = bootstrap_test(runs, b=200)
    flat = np.array(runs[0])
    m_star = flat[bootstrap_indices([7], b=200)].mean(axis=1)
    m = flat.mean()
    assert out["p"] == pytest.approx((1 + np.count_nonzero(m_star - m >= m)) / 201)
    assert out["ci95"] == pytest.approx(list(np.quantile(m_star, [0.025, 0.975], method="linear")))


def test_insufficient_information_cases():
    assert bootstrap_test([[0.1] * 5, [0.2] * 3])["insufficient"] == "no inference days"
    assert bootstrap_test([[0.0] * 10])["insufficient"] == "all inference d are 0"
    const = bootstrap_test([[0.01] * 10])
    assert const["insufficient"] == "m* variance is 0" and const["m"] == pytest.approx(0.01)
    assert inference_mean([[1.0] * 3]) is None and inference_mean([[1.0] * 6, [5.0]]) == 1.0


def _v(test, g=(0.1, 0.05), d=(0.05, 0.05), m_stress=0.001, invalid=()):
    return verdict(invalid=list(invalid), g_arm=g[0], g_base=g[1], d_arm=d[0], d_base=d[1], test=test,
                   m_stress=m_stress)["row"]


def test_verdict_rows():
    rng = np.random.default_rng(5)
    strong = bootstrap_test([list(rng.normal(0.01, 0.001, 30))])
    assert strong["p"] <= 0.05
    assert _v(strong, invalid=["operational check failed"]) == 0
    assert _v(strong, g=(0.04, 0.05)) == 2  # worse G
    assert _v(strong, d=(0.0701, 0.05)) == 2  # D more than 2%p worse
    assert _v(strong, d=(0.0699, 0.05)) == 6  # within 2%p
    assert _v(strong, m_stress=-0.0001) == 2
    neg = bootstrap_test([[-0.01, 0.0, -0.02, 0.01, -0.01, 0.0]])
    assert _v(neg) == 2  # m < 0
    assert _v(bootstrap_test([[0.0] * 10])) == 3  # all d zero (degenerate)
    assert _v(bootstrap_test([[0.1] * 4])) == 3  # nothing to test
    assert _v(strong, m_stress=0.0) == 3
    assert _v(strong, g=(0.05, 0.05)) == 3
    assert _v(strong, g=(-0.01, -0.02)) == 4
    assert _v(strong, g=(0.0, -0.02)) == 4
    assert _v(strong) == 6
    no_effect = bootstrap_test([list(np.tile([0.01, -0.01], 20))])
    assert no_effect["m"] == 0 and _v(no_effect) == 3


def test_verdict_no_effect_series_is_undecided():
    rng = np.random.default_rng(11)
    x = rng.normal(0.0, 0.01, 60)
    x = x - x.mean() + 0.0002  # small positive mean, far from significant
    t = bootstrap_test([list(x)])
    assert t["p"] > 0.05 and _v(t) == 5


def test_d_by_runs_follows_the_mask():
    seg = _mask_segment("xxx.xxxxxx")
    d = {"A": {day: float(i) for i, day in enumerate(seg["scored"])}}
    assert d_by_runs(d, {"segments": {"A": seg}}) == [[0.0, 1.0, 2.0], [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]]


# ---------------------------------------------------------------- §7 pre-run count


def _cand(build, ev=NO_EVENTS, ext=EXT_OK):
    reg, pre = build()
    return P.morning_candidate(DAY, reg, pre, PRIOR, ext, ev, **QC)


def test_morning_candidate_admission_per_arm():
    normal = _cand(s1_day)
    assert (normal["label"], normal["symbol"]) == ("S1", R.LONG)
    assert normal["admit"] == {"v1.0+": True, "v1.3-cap": True, "v1.3": True} and normal["cap70_binds"] is True
    wide = _cand(lambda: wide_stop_day())
    assert wide["admit"] == {"v1.0+": False, "v1.3-cap": True, "v1.3": True} and wide["cap70_binds"] is False
    assert wide["v10_refusal_is_stop_cap"] is True and wide["s5"] is None
    assert _cand(s2_day)["symbol"] == R.SHORT and _cand(s3_day)["label"] == "S3"
    assert _cand(s1_day, ev={**NO_EVENTS, "fomc": True})["s0"] is True
    assert _cand(lambda: s1_day(qqq_vol=1000.0))["label"] is None
    both = _cand(lambda: wide_stop_day(qqq_after=lambda t: PREV * 1.03))
    assert both["s5"] == R.LONG and both["risk_mode"] == "NORMAL"
    assert _cand(lambda: wide_stop_day(), ext=EXT_HIGH)["risk_mode"] == "HIGH"


def test_morning_candidate_reads_nothing_after_the_decision_bar_but_the_s5_condition():
    reg, pre = wide_stop_day(qqq_after=lambda t: PREV * 1.03)
    ref = P.morning_candidate(DAY, reg, pre, PRIOR, EXT_OK, NO_EVENTS, **QC)
    garbage = {s: [b if (b.t <= R.DECISION or (s == "QQQ" and b.t == R.S5_DECISION - 1))
                   else Bar(b.t, 7.0, 7.0, 7.0, 7.0, 1.0) for b in v] for s, v in reg.items()}
    assert P.morning_candidate(DAY, garbage, pre, PRIOR, EXT_OK, NO_EVENTS, **QC) == ref


def test_precount_aggregation(monkeypatch):
    mask = {"segments": {"A": _mask_segment("xxxxxxx.xx"), "B": _mask_segment("xxx")}}
    script = iter([
        {"label": "S2", "symbol": R.SHORT, "risk_mode": "NORMAL", "s0": False, "s5": R.LONG, "cap70_binds": False,
         "admit": {"v1.0+": False, "v1.3-cap": True, "v1.3": True}},  # new, inference, S5 opposite
        {"label": "S1", "symbol": R.LONG, "risk_mode": "HIGH", "s0": False, "s5": None, "cap70_binds": True,
         "admit": {"v1.0+": True, "v1.3-cap": True, "v1.3": True}},  # old entry
        {"label": None, "symbol": None, "risk_mode": "NORMAL", "s0": True, "s5": None, "cap70_binds": None,
         "admit": {}},
    ] + [{"label": None, "symbol": None, "risk_mode": "NORMAL", "s0": False, "s5": R.SHORT, "cap70_binds": None,
          "admit": {}}] * 4 + [
        {"label": "S1", "symbol": R.LONG, "risk_mode": "UNKNOWN_TREATED_AS_HIGH", "s0": False, "s5": R.LONG,
         "cap70_binds": False, "admit": {"v1.0+": False, "v1.3-cap": True, "v1.3": True}},  # new, short run
    ] + [{"label": None, "symbol": None, "risk_mode": "NORMAL", "s0": False, "s5": None, "cap70_binds": None,
          "admit": {}}] * 4)
    monkeypatch.setattr(P, "morning_candidate", lambda *a, **k: next(script))
    seg_days = {name: [{"day": d, "regular": None, "qqq_pre": None, "prior": None, "external": None,
                        "events": None, "prev_times": None, "expected_prev": None, "prev_early_close": False}
                       for d in seg["scored"]] for name, seg in mask["segments"].items()}
    monkeypatch.setattr(P, "segment_days", lambda *a: seg_days)
    out = P.run_precount(mask, [], CAL, {})
    a, tot = out["segments"]["A"], out["total"]
    assert a["days"] == 9 and a["s0_days"] == 1 and a["new"]["days"] == 2
    assert a["new"]["by_signal"] == {"S1": 1, "S2": 1, "S3": 0}
    assert a["new"]["inference"] == 1 and a["new"]["not_inference"] == 1
    assert a["new"]["by_risk_mode"]["UNKNOWN_TREATED_AS_HIGH"] == 1
    assert a["s5_overlap_new"] == {"days": 2, "same_direction": 1, "opposite_direction": 1}
    assert a["cap70_binds_all_v13_morning"] == 1 and a["cap70_binds_new"] == 0
    assert a["v10_entry"]["S1"] == 1 and a["v13cap_entry"] == {"S1": 2, "S2": 1, "S3": 0}
    assert a["s5_condition_days"] == 6
    assert tot["days"] == 12 and tot["new"]["days"] == 2 and tot["new"]["combo"] == a["new"]["combo"]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixed_mask(tmp_path):
    v11 = _load("v11_precount", "scripts/daytrade_v11_precount.py")
    db = _facts_db(tmp_path / "h.sqlite")
    vix, vix9d, dix = _csvs(tmp_path)
    common = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--dix-csv", str(dix),
              "--allow-dirty"]
    mpath = tmp_path / "mask.json"
    assert v11.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 0
    return common, mpath, hashlib.sha256(mpath.read_bytes()).hexdigest()


def test_precount_script_on_a_synthetic_db(tmp_path, capsys):
    mod = _load("v13_precount", "scripts/daytrade_v13_precount.py")
    common, mpath, sha = _fixed_mask(tmp_path)
    out = tmp_path / "c" / "counts.json"
    argv = [*common, "--mask", str(mpath), "--out", str(out)]
    assert mod.main(argv) == 2 and not out.exists()  # not the PREREG mask hash
    assert "84d22f46" in capsys.readouterr().err
    assert mod.main(argv, expected_mask_sha=sha) == 0
    doc = json.loads(out.read_text())
    mask = json.loads(mpath.read_text())
    assert doc["counts"]["segments"]["A"]["days"] == mask["segments"]["A"]["scored_count"] > 0
    assert doc["counts"]["total"]["new"]["days"] == 0  # flat synthetic prices: no morning signal
    m = doc["manifest"]
    assert m["mask_sha256"] == sha and m["git_commit"] and set(m["external_csv_sha256"]) == {"vix", "vix9d", "gex"}
    assert "pnl" not in json.dumps(doc["counts"]).lower()
    assert mod.main(argv, expected_mask_sha=sha) == 2  # write-once


def test_precount_script_refuses_dirty_tree_and_tampered_mask(tmp_path, monkeypatch):
    mod = _load("v13_precount", "scripts/daytrade_v13_precount.py")
    common, mpath, sha = _fixed_mask(tmp_path)
    out = tmp_path / "counts.json"
    monkeypatch.setattr(mod, "git_state", lambda: {"commit": "x", "dirty_tracked": True, "dirty_files": ["M a"]})
    clean = [a for a in common if a != "--allow-dirty"]
    assert mod.main([*clean, "--mask", str(mpath), "--out", str(out)], expected_mask_sha=sha) == 2
    monkeypatch.undo()
    mask = json.loads(mpath.read_text())
    mask["segments"]["A"]["scored"].append("2024-07-24")
    mpath.write_bytes(V.mask_bytes(mask))
    sha2 = hashlib.sha256(mpath.read_bytes()).hexdigest()
    assert mod.main([*common, "--mask", str(mpath), "--out", str(out)], expected_mask_sha=sha2) == 2
    assert not out.exists()


# ---------------------------------------------------------------- main-run script (synthetic only)


def test_backtest_script_refuses_without_user_approval(tmp_path, capsys):
    mod = _load("v13_backtest", "scripts/daytrade_v13_backtest.py")
    out = tmp_path / "r.json"
    argv = ["--db", str(tmp_path / "none.sqlite"), "--vix-csv", "x", "--vix9d-csv", "y", "--mask", "m",
            "--out", str(out), "--allow-dirty"]
    assert mod.main(argv) == 2 and not out.exists()
    assert "--i-have-user-approval" in capsys.readouterr().err


def test_backtest_script_on_a_synthetic_db(tmp_path):
    mod = _load("v13_backtest", "scripts/daytrade_v13_backtest.py")
    common, mpath, sha = _fixed_mask(tmp_path)
    out = tmp_path / "r" / "result.json"
    argv = [*common, "--mask", str(mpath), "--out", str(out), "--i-have-user-approval"]
    assert mod.main(argv) == 2 and not out.exists()  # not the PREREG mask hash
    assert mod.main(argv, expected_mask_sha=sha) == 0
    doc = json.loads(out.read_text())
    n = json.loads(mpath.read_text())["segments"]["A"]["scored_count"]
    assert doc["verdict"]["row"] == 3 and doc["invalid"] == []  # flat prices: no trades, every d is 0
    assert doc["regression"]["one"]["counts"]["match"] == n and doc["regression"]["two"]["mismatch_days"] == []
    assert set(doc["days"]) == {"base", "stress_ab", "reference_cost2x"}
    assert set(doc["days"]["base"]) == {"v1.0+", "v1.3-cap", "v1.3"}
    m = doc["manifest"]
    assert m["random_seed"] == 20261007 and m["bootstrap_B"] == 10000 and m["mask_sha256"] == sha
    assert m["variants"]["stress_ab"] == {"cost_mult": 1.5, "delay_bars": 1, "verdict": True}
    assert mod.main(argv, expected_mask_sha=sha) == 2  # write-once


def test_reports_on_synthetic_arms():
    builds = [wide_stop_day, lambda: s1_day(qqq_after=lambda t: PREV * 0.97), s2_day,
              lambda: _gap_then_recover(T(11, 0)), lambda: s1_day(qqq_vol=1000.0)] * 2
    seg = {"A": [dict(day_input(b), day=f"2024-01-{i + 1:02d}") for i, b in enumerate(builds)]}
    mask = {"segments": {"A": _mask_segment("x" * len(builds))}}
    plus, cap, full = (run_arm(seg, p) for p in (V10_PLUS, V13_CAP, V13))
    d = daily_d(cap, plus)
    dec = decompose_d(cap, plus, d, mask)
    assert dec["added_morning"]["A/S1/TQQQ"]["days"] == 2 and dec["inference_days"] == len(builds)
    assert dec["added_morning"]["A/S1/TQQQ"]["d_sum"] == pytest.approx(d["A"]["2024-01-01"] + d["A"]["2024-01-06"])
    summ = arm_summary(full)["segments"]["A"]
    assert summ["lock_days"] == 2 and summ["lock_by_source"] == {"pre_entry": 0, "close": 2}
    assert summ["stop_exits"] >= 2 and summ["actual_stop_loss"] > summ["planned_stop_loss"] > 0
    assert not_double(full, cap)["by_reason"].get("cash_or_entry_budget_binds_at_1pct", 0) >= 2
