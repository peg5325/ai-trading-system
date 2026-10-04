"""PREREG_STAGE3_REGIME_V1 section 5 tests: look-ahead, costs, selection. Synthetic data only."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research.stage3_regime import contract as C
from src.research.stage3_regime.engine import (
    COMBOS,
    classify,
    held_labels,
    positions,
    select,
    tie_key,
    window_factors,
)
from src.research.stage3_regime.runner import run_confirm, run_explore
from src.research.stage3_swing import contract as SC
from src.research.stage3_swing.loading import RawData


def _signal(n: int = 900, seed: int = 0):
    rng = np.random.default_rng(seed)
    px = 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, n))
    vix = np.clip(20 + np.cumsum(rng.normal(0, 1.0, n)), 9, 80)
    return px, vix


def _raw(seed: int = 0, poison_after=None) -> RawData:
    cal = pd.bdate_range("1993-01-25", "2021-06-30")  # panel keeps rows from PREP_START
    rng = np.random.default_rng(seed)
    n = len(cal)
    yahoo = {}
    for sym in SC.YAHOO_SYMBOLS:
        close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
        yahoo[sym] = pd.DataFrame({"close": close, "div": 0.0}, index=cal)
    vix = pd.Series(np.clip(20 + np.cumsum(rng.normal(0, 0.8, n)), 9, 80), index=cal)
    rf = pd.Series(0.0001, index=cal)
    raw = RawData(yahoo=yahoo, cboe_vix=vix, rf=rf)
    if poison_after is not None:
        ts = pd.Timestamp(poison_after)
        for df in raw.yahoo.values():
            df.loc[df.index > ts, "close"] = 1e9
        raw.cboe_vix[raw.cboe_vix.index > ts] = 99.0
        raw.rf[raw.rf.index > ts] = 0.5
    return raw


# (1) look-ahead -------------------------------------------------------------


@pytest.mark.parametrize("t", [400, 650, 899])
def test_regime_at_t_ignores_prices_after_t(t):
    px, vix = _signal()
    base = classify(px, vix)
    px2, vix2 = px.copy(), vix.copy()
    px2[t + 1 :] *= np.linspace(0.3, 3.0, len(px) - t - 1)
    vix2[t + 1 :] = 80.0
    alt = classify(px2, vix2)
    np.testing.assert_array_equal(base[: t + 1], alt[: t + 1])
    combo = (3, 2, -1, 1)
    p1, _, l1 = positions(base, combo)
    p2, _, l2 = positions(alt, combo)
    # the position held after close t+1 is decided by the regime at t
    np.testing.assert_array_equal(p1[: t + 2], p2[: t + 2])
    np.testing.assert_array_equal(l1[: t + 2], l2[: t + 2])


def test_lag_sets_exposure_one_close_after_the_regime():
    labels = np.array([0, 0, 3, 3, 2, 1], dtype=np.int8)
    combo = (1, 2, -1, 3)
    prod, _, lab = positions(labels, combo, "lag")
    assert list(prod) == [SC.CASH, SC.X1, SC.X1, SC.X3, SC.X3, SC.INV]
    assert list(lab) == [C.UNDEFINED, 0, 0, 3, 3, 2]
    sp, _, slab = positions(labels, combo, "same")
    assert list(sp) == [SC.X1, SC.X1, SC.X3, SC.X3, SC.INV, SC.X2]
    # the day's label is the regime that decided the position earning that day
    assert list(held_labels(lab, 2, 5)) == [0, 0, 3, 3]
    assert list(held_labels(slab, 2, 5)) == [0, 3, 3, 2]


def test_regime_thresholds():
    n = 260
    px = np.r_[np.linspace(100, 120, n - 1), 130.0]  # far above the 200-day mean
    lab = classify(px, np.r_[np.full(n - 1, 15.0), 20.0])
    assert lab[-2] == 0 and lab[-1] == 1  # VIX 20 is "unstable"
    down = np.r_[np.linspace(120, 100, n - 2), 99.0, 98.0]
    assert classify(down, np.full(n, 15.0))[-1] == 3  # two down days: RSI(2) = 0 < 10
    assert classify(px, np.full(n, 15.0))[0] == C.UNDEFINED  # warm-up


def test_explore_never_uses_rows_after_explore_end():
    clean = run_explore(_raw(), {})
    poisoned = run_explore(_raw(poison_after=C.EXPLORE_END), {})
    assert clean["data"]["last_row"] <= str(C.EXPLORE_END)
    assert clean["indices"] == poisoned["indices"]
    assert clean["explore_window"] == {"start": "2006-07-03", "end": "2019-12-31"}


def test_explore_and_confirm_run_end_to_end():
    raw = _raw(seed=3)
    sel = run_explore(raw, {})
    for row in sel["indices"].values():
        assert row["n_combos"] == 625
        if row["status"] == "SELECTED":
            assert -row["explore"]["max_drawdown"] < C.MDD_LIMIT
    res = run_confirm(raw, sel, {})
    assert res["confirm_window"]["start"] == "2020-01-02"
    for row in res["indices"].values():
        assert row["verdict"] in ("PASS", "FAIL", "NO_CANDIDATE")
        if row["verdict"] != "NO_CANDIDATE":
            days = sum(r["days"] for r in row["base"]["by_regime"].values())
            a, b = res["confirm_window"]["start"], res["confirm_window"]["end"]
            assert days == len(pd.bdate_range(a, b)) - 1
            assert row["stress_cost"]["strategy"]["final_usd"] <= row["base"]["strategy"]["final_usd"]


# (2) costs ------------------------------------------------------------------


def _zero_rmat(n: int) -> np.ndarray:
    return np.zeros((5, n))


def test_cost_charged_once_per_change_including_switch_and_initial_buy():
    # decided: A A A C C D D  -> held (lag): -, A, A, A, C, C, D
    labels = np.array([0, 0, 0, 2, 2, 3, 3], dtype=np.int8)
    combo = (1, 1, -1, 0)  # A: 1x, C: inverse, D: cash
    prod, w, _ = positions(labels, combo)
    c = C.COST_ONE_WAY
    v0, lf = window_factors(prod, w, _zero_rmat(7), 1, 6, c)
    assert v0 == pytest.approx(C.START_EQUITY * (1 - c))  # initial buy of 1x at close 1
    final = v0 * np.exp(lf.sum())
    # 1x -> inverse at close 4 (sell + buy = 2 legs), inverse -> cash at close 6 (1 leg)
    assert final == pytest.approx(C.START_EQUITY * (1 - c) * (1 - 2 * c) * (1 - c))
    assert np.count_nonzero(lf) == 2


def test_no_cost_without_exposure_change_and_none_for_cash_start():
    labels = np.array([2, 2, 2, 2, 2], dtype=np.int8)
    prod, w, _ = positions(labels, (3, 3, 0, 3))
    v0, lf = window_factors(prod, w, _zero_rmat(5), 1, 4, C.COST_ONE_WAY)
    assert v0 == C.START_EQUITY and not lf.any()


def test_leverage_change_charges_weight_switch_not_difference():
    # 1x -> 3x is a product switch: sell 1 + buy 1
    labels = np.array([0, 1, 1], dtype=np.int8)
    prod, w, _ = positions(labels, (1, 3, 0, 0))
    _, lf = window_factors(prod, w, _zero_rmat(3), 1, 2, C.COST_ONE_WAY)
    assert lf[1] == pytest.approx(np.log(1 - 2 * C.COST_ONE_WAY))


# (3) selection --------------------------------------------------------------


def _idx(combo) -> int:
    return COMBOS.index(combo)


def test_selection_respects_drawdown_limit():
    growth = np.zeros(len(COMBOS))
    mdd = np.full(len(COMBOS), -0.10)
    growth[_idx((3, 3, 3, 3))] = 5.0
    mdd[_idx((3, 3, 3, 3))] = -0.35  # exactly 35% is not < 35%
    growth[_idx((2, 2, 0, 3))] = 4.0
    assert COMBOS[select(growth, mdd)] == (2, 2, 0, 3)


def test_selection_no_candidate():
    assert select(np.ones(len(COMBOS)), np.full(len(COMBOS), -0.5)) is None


def test_tie_break_smaller_abs_sum_then_exposure_order():
    growth = np.zeros(len(COMBOS))
    mdd = np.full(len(COMBOS), -0.10)
    for combo in [(2, 2, 0, 0), (1, 1, -1, 0), (1, 1, 1, 0)]:
        growth[_idx(combo)] = 1.0
    growth[_idx((1, 1, 1, 0))] += 1e-13  # within tie tolerance
    # abs sums 4, 3, 3 -> (1,1,-1,0) vs (1,1,1,0): C exposure 1 ranks before -1
    assert COMBOS[select(growth, mdd)] == (1, 1, 1, 0)
    assert tie_key((0, 0, 0, -1)) > tie_key((0, 0, 0, 1))
    assert tie_key((0, 0, 0, -1)) < tie_key((0, 0, 0, 2))
