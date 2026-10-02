"""PREREG_STAGE3_SWING_V1 section 7 tests: look-ahead, synthetic leverage, costs.

Synthetic data only. No real prices are read.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.research.stage3_swing import contract as C
from src.research.stage3_swing.account import (
    Drift,
    account_values,
    drift_l3,
    full_period_stats,
    log_factors,
    log_factors_drift,
    month_starts,
    order_legs,
    turnover,
    window_growth,
)
from src.research.stage3_swing.engine import index_paths
from src.research.stage3_swing.loading import (
    RawData,
    build_panel,
    calendar_flags,
    parse_cboe_vix,
    parse_french_rf,
    parse_yahoo,
    tr_level,
)
from src.research.stage3_swing.products import return_matrix, synthetic_return
from src.research.stage3_swing.rules import (
    rebalance_flags,
    rule_positions,
    series_from_arrays,
)


def _arrays(n: int = 1600, seed: int = 0) -> dict:
    dates = pd.bdate_range("1993-02-01", periods=n)
    rng = np.random.default_rng(seed)
    pr = rng.normal(0.0004, 0.014, n)
    is_me, tdm, tde, month = calendar_flags(dates)
    return {
        "pr": pr,
        "ru": pr + 0.0001,
        "rf": np.full(n, 0.0001),
        "vix": np.clip(20 + np.cumsum(rng.normal(0, 0.8, n)), 9, 80),
        "is_me": is_me,
        "td_of_month": tdm,
        "td_from_end": tde,
        "month": month,
    }


def _series(arr: dict):
    return series_from_arrays(**arr)


def _perturb_after(arr: dict, t: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    out = {k: v.copy() for k, v in arr.items()}
    m = len(out["pr"]) - t - 1
    out["pr"][t + 1 :] = rng.normal(-0.01, 0.05, m)
    out["ru"][t + 1 :] = rng.normal(0.02, 0.05, m)
    out["vix"][t + 1 :] = rng.uniform(9, 90, m)
    out["rf"][t + 1 :] = rng.uniform(0, 0.001, m)
    return out


# ---------------------------------------------------------------- (1) look-ahead


@pytest.mark.parametrize("t", [400, 777, 1001, 1333])
def test_decision_at_t_ignores_prices_after_t(t):
    arr = _arrays()
    other = _perturb_after(arr, t, seed=t)
    for mode, last_row in (("lag", t + 1), ("same", t)):
        a = rule_positions(_series(arr), mode)
        b = rule_positions(_series(other), mode)
        for rule in C.RULE_IDS:
            np.testing.assert_array_equal(a[rule][0][: last_row + 1], b[rule][0][: last_row + 1], rule)
            np.testing.assert_array_equal(a[rule][1][: last_row + 1], b[rule][1][: last_row + 1], rule)


def test_rules_actually_trade_on_synthetic_data():
    # guards against a vacuous look-ahead test: indicator rules must change position
    pos = rule_positions(_series(_arrays()))
    for rule in ("3", "5", "7", "9", "11", "12"):
        assert len(np.unique(pos[rule][1])) > 1, rule


def _yahoo_json(dates, closes, dividends=None) -> bytes:
    stamps = [int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()) for d in dates]
    result = {"timestamp": stamps, "indicators": {"quote": [{"close": list(closes)}]}}
    if dividends:
        result["events"] = {
            "dividends": {
                str(stamps[i]): {"amount": amt, "date": stamps[i]} for i, amt in dividends.items()
            }
        }
    return json.dumps({"chart": {"result": [result]}}).encode()


def test_rows_after_data_end_are_dropped_at_parse():
    dates = pd.bdate_range("2025-07-10", "2025-07-30")
    closes = [100.0 + i for i in range(len(dates))]
    df = parse_yahoo(_yahoo_json(dates, closes))
    assert df.index.max() == pd.Timestamp(C.DATA_END)

    cboe = "DATE,OPEN,HIGH,LOW,CLOSE\n07/17/2025,1,1,1,15\n07/18/2025,1,1,1,16\n07/21/2025,1,1,1,99\n"
    assert parse_cboe_vix(cboe.encode()).index.max() == pd.Timestamp(C.DATA_END)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("F-F.csv", "header\n20250717, 0.1, 0.1, 0.1, 0.017\n20250718, 0.1, 0.1, 0.1, 0.018\n"
                    "20250721, 0.1, 0.1, 0.1, 9.9\n")
    rf = parse_french_rf(buf.getvalue())
    assert rf.index.max() == pd.Timestamp(C.DATA_END)
    assert rf.iloc[-1] == pytest.approx(0.00018)


def test_panel_never_contains_rows_after_data_end():
    cal = pd.bdate_range("1992-12-01", "2025-09-30")
    rng = np.random.default_rng(1)
    n = len(cal)
    after = cal > pd.Timestamp(C.DATA_END)
    yahoo = {}
    for sym in C.YAHOO_SYMBOLS:
        close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, n))
        close[after] = 1e9  # poison: must never reach the panel
        raw = parse_yahoo(_yahoo_json(cal, close, {5: 0.1}))
        yahoo[sym] = raw
    vix = parse_cboe_vix(
        ("DATE,CLOSE\n" + "\n".join(f"{d:%m/%d/%Y},{18 + (i % 5)}" for i, d in enumerate(cal))).encode()
    )
    rf = pd.Series(0.0001, index=cal[cal <= pd.Timestamp(C.DATA_END)])
    panel = build_panel(RawData(yahoo=yahoo, cboe_vix=vix, rf=rf))
    assert panel.dates.max() <= pd.Timestamp(C.DATA_END)
    assert panel.dates.min() == pd.Timestamp(C.PREP_START)
    for idx in C.INDICES:
        assert np.abs(panel.pr[idx]).max() < 1.0
        assert np.abs(panel.ru[idx]).max() < 1.0
    assert len(month_starts(panel.dates)) == C.N_STARTS


def test_calendar_flags_are_price_free_and_final_month_is_unknown():
    dates = pd.bdate_range("2000-01-03", "2000-03-15")
    is_me, tdm, tde, _ = calendar_flags(dates)
    jan = dates.month == 1
    assert tdm[jan][0] == 1 and is_me[jan][-1] and tde[jan][-2] == 1
    assert not is_me[-1] and tde[-1] == 99  # March is cut by the data end


# ---------------------------------------------------------------- (2) synthetic leverage

RU = np.array([0.01, -0.02])
RF = np.array([0.0001, 0.0002])
S = 0.005
ER_LEV = 0.0095


def test_leverage_2x_by_hand():
    exp0 = 2 * 0.01 - 1 * 0.0001 - 1 * S / 252 - ER_LEV / 252
    exp1 = 2 * -0.02 - 1 * 0.0002 - 1 * S / 252 - ER_LEV / 252
    got = synthetic_return(RU, RF, 2.0, ER_LEV)
    np.testing.assert_allclose(got, [exp0, exp1], rtol=0, atol=1e-15)


def test_leverage_3x_by_hand():
    exp0 = 3 * 0.01 - 2 * 0.0001 - 2 * S / 252 - ER_LEV / 252
    exp1 = 3 * -0.02 - 2 * 0.0002 - 2 * S / 252 - ER_LEV / 252
    np.testing.assert_allclose(
        synthetic_return(RU, RF, 3.0, ER_LEV), [exp0, exp1], rtol=0, atol=1e-15
    )


def test_inverse_by_hand():
    exp0 = -1 * 0.01 + 2 * 0.0001 - 2 * S / 252 - ER_LEV / 252
    exp1 = -1 * -0.02 + 2 * 0.0002 - 2 * S / 252 - ER_LEV / 252
    np.testing.assert_allclose(
        synthetic_return(RU, RF, -1.0, ER_LEV), [exp0, exp1], rtol=0, atol=1e-15
    )


def test_one_x_has_no_spread_or_rf_term_and_index_specific_er():
    sp = return_matrix(RU, RF, "sp")
    ndx = return_matrix(RU, RF, "ndx")
    np.testing.assert_allclose(sp[C.X1], RU - 0.000945 / 252, rtol=0, atol=1e-15)
    np.testing.assert_allclose(ndx[C.X1], RU - 0.0020 / 252, rtol=0, atol=1e-15)
    np.testing.assert_allclose(sp[C.X3], synthetic_return(RU, RF, 3.0, 0.0095), rtol=0, atol=1e-15)
    assert not sp[C.CASH].any()  # cash earns 0%


def test_total_return_level_adds_dividend_on_ex_date():
    df = pd.DataFrame({"close": [100.0, 101.0, 100.0], "div": [0.0, 0.0, 1.0]})
    lvl = tr_level(df)
    assert lvl.iloc[1] == pytest.approx(1.01)
    assert lvl.iloc[2] / lvl.iloc[1] == pytest.approx((100.0 + 1.0) / 101.0)


# ---------------------------------------------------------------- (3) costs

C0 = C.COST_ONE_WAY


def _zero_returns(n: int) -> np.ndarray:
    return np.zeros((5, n))


def test_product_switch_charges_sell_plus_buy_once():
    prod = np.array([C.X2, C.X2, C.INV, C.INV], dtype=np.int8)
    w = np.ones(4)
    lf = log_factors(prod, w, _zero_returns(4), C0)
    assert turnover(prod, w).tolist() == [0.0, 0.0, 2.0, 0.0]
    np.testing.assert_allclose(lf, [0, 0, np.log(1 - 2 * C0), 0], atol=1e-15)


def test_cash_entry_and_exit_charge_one_leg_each():
    prod = np.array([C.CASH, C.X2, C.X2, C.CASH, C.CASH], dtype=np.int8)
    w = np.array([0.0, 1.0, 1.0, 0.0, 0.0])
    lf = log_factors(prod, w, _zero_returns(5), C0)
    np.testing.assert_allclose(lf, [0, np.log(1 - C0), 0, np.log(1 - C0), 0], atol=1e-15)
    assert order_legs(prod, w).tolist() == [0, 1, 0, 1, 0]


def test_monthly_rebalance_charges_only_weight_change():
    prod = np.array([C.X3, C.X3, C.X3, C.X3], dtype=np.int8)
    w = np.array([0.2, 0.2, 0.5, 0.5])
    lf = log_factors(prod, w, _zero_returns(4), C0)
    np.testing.assert_allclose(lf, [0, 0, np.log(1 - 0.3 * C0), 0], atol=1e-15)


def test_rule7_turnover_equals_weight_change_only():
    arr = _arrays()
    s = _series(arr)
    prod, w = rule_positions(s)["7"]
    np.testing.assert_allclose(turnover(prod, w)[1:], np.abs(np.diff(w)), atol=1e-15)
    changed = np.flatnonzero(np.diff(w) != 0) + 1
    month_end_next = np.flatnonzero(arr["is_me"]) + 1  # decided at month-end, trades next close
    assert set(changed) <= set(month_end_next)
    assert len(changed) > 5


def test_initial_buy_is_charged_exactly_once():
    n = 6
    zero = _zero_returns(n)
    prod = np.full(n, C.X1, dtype=np.int8)
    w = np.ones(n)
    lc = np.cumsum(log_factors(prod, w, zero, C0))
    assert not lc.any()  # holding trades nothing after the initial buy
    assert window_growth(lc, w, 0, n - 1, C0) == pytest.approx(1 - C0)

    # rule 1 from the rule engine: same single initial charge
    s = _series(_arrays(300))
    p1, w1 = rule_positions(s)["1"]
    lc1 = np.cumsum(log_factors(p1, w1, _zero_returns(300), C0))
    assert window_growth(lc1, w1, 10, 299, C0) == pytest.approx(1 - C0)

    # one product switch in the middle adds exactly one sell + one buy
    prod2 = np.array([C.X2, C.X2, C.X2, C.INV, C.INV, C.INV], dtype=np.int8)
    lc2 = np.cumsum(log_factors(prod2, w, zero, C0))
    assert window_growth(lc2, w, 0, 5, C0) == pytest.approx((1 - C0) * (1 - 2 * C0))
    dates = pd.bdate_range("2000-01-03", periods=n)
    stats = full_period_stats(prod2, w, lc2, 0, 5, C0, dates)
    assert stats["orders"] == 3 and stats["trade_days"] == 2


# ---- partial-weight rules (7, 8): the 3x sleeve drifts between rebalances

_R3 = np.array([0.0, 0.2, 0.2])
_W = np.array([0.5, 0.5, 0.5])


def test_drifted_rebalance_charges_gap_between_target_and_drifted_sleeve():
    reb = np.array([False, False, True])
    lf = log_factors_drift(_W, reb, _R3, C0)
    # sleeve 0.5 -> 0.6 -> 0.72, cash 0.5: V = 1.22; target sleeve 0.61; trade |0.61 - 0.72| = 0.11
    assert np.exp(lf.sum()) == pytest.approx(1.22 - C0 * 0.11, abs=1e-12)


def test_weight_drifts_within_month_without_implicit_daily_reset():
    reb = np.array([False, False, False])
    lf = log_factors_drift(_W, reb, _R3, C0)
    assert np.exp(lf.sum()) == pytest.approx(1.22, abs=1e-12)  # constant-weight model gives 1.21
    assert np.exp(lf[:2].sum()) == pytest.approx(1.1, abs=1e-12)


def test_start_inside_a_segment_uses_start_weight_until_first_rebalance():
    reb = np.array([False, False, True])
    lc = np.cumsum(log_factors_drift(_W, reb, _R3, C0))
    drift = Drift(reb, drift_l3(_R3))
    v = account_values(lc, _W, drift, np.array([1]), np.array([[1, 2]]), np.array([1.0]), C0)
    # opened at row 1 with sleeve 0.5: 0.5 -> 0.6 (V 1.1); gap |0.55 - 0.6| = 0.05
    assert v[0, 0] == pytest.approx(1.0)
    assert v[0, 1] == pytest.approx(1.1 - C0 * 0.05, abs=1e-12)
    v0 = account_values(lc, _W, drift, np.array([0]), np.array([[2]]), np.array([1.0]), C0)
    assert v0[0, 0] == pytest.approx(1.22 - C0 * 0.11, abs=1e-12)


def test_rules_7_and_8_use_drift_and_full_weight_rules_do_not():
    s = _series(_arrays())
    rmat, paths = index_paths(s, "sp", C0)
    assert paths["7"][3] is not None and paths["8"][3] is not None
    assert all(paths[r][3] is None for r in C.RULE_IDS if r not in ("7", "8"))
    flags = rebalance_flags(s)
    for rule in ("7", "8"):
        w = paths[rule][1]
        changed = np.flatnonzero(np.diff(w) != 0) + 1
        assert set(changed) <= set(np.flatnonzero(flags[rule]))
    p3, w3, lc3, _ = paths["3"]
    np.testing.assert_array_equal(lc3, np.cumsum(log_factors(p3, w3, rmat, C0)))
