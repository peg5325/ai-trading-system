"""QQQ false-breakout short fade (docs/research/EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.md).

Synthetic data only: no test reads data/*.sqlite. Base day: flat 100, ORB 99..101 (range 2), daily ATR 4 (min range
0.6, runaway above 103.4), breakout bar 09:50 high 101.5 close 101.2, trap bar 09:51 close 100.8, entry open 09:52
100.8 -> fill 100.78, stop 101.52, R 0.74, TP2 max(99, 100.78 - 1.48) = 99.30.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_false_breakout_fade_v1 import engine as E
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import (
    _write_day,
    fill_from,
    flat,
    make_sd,
    setb,
)
from tests.test_explore_pullback_vwap_v1 import _load_script


def fade_bars(trap=True, breakout_high=101.5):
    b = flat()
    setb(b, 575, 100, 101, 100, 100)
    setb(b, 580, 100, 100, 99, 100)
    setb(b, 590, 100.5, breakout_high, 100.4, 101.2)
    if trap:
        setb(b, 591, 101.2, 101.3, 100.6, 100.8)
        fill_from(b, 592, 100.8)
    else:
        fill_from(b, 591, 101.2)
    return b


def sd_of(bars, atr=4.0):
    sd = make_sd(bars, symbol="QQQ")
    sd.daily_atr, sd.daily_ema = atr, 99.0  # make_sd already set the VWAP series
    return sd


def trade_of(bars, atr=4.0):
    sd = sd_of(bars, atr)
    st, s = E.find_setup(sd)
    assert st == "signal"
    plan, why = E.entry_plan(sd, s, 2000.0)
    assert why == ""
    return sd, s, plan, E.simulate_trade(sd, s, plan)


# ---------------------------------------------------------------- setup search
def test_setup_and_entry_plan_numbers():
    sd = sd_of(fade_bars())
    st, s = E.find_setup(sd)
    assert st == "signal" and sd.bars[s.index].t == 591 and s.swing_high == 101.5
    plan, _ = E.entry_plan(sd, s, 2000.0)
    assert plan["raw"] == 100.8 and plan["fill"] == pytest.approx(100.78)
    assert plan["stop"] == pytest.approx(101.52) and plan["r"] == pytest.approx(0.74)
    assert plan["tp2"] == pytest.approx(99.30)
    # 1% risk wants 27.027 shares; the cash cap allows floor(2000 / 100.78, 4 dp)
    assert plan["shares"] == math.floor(2000 / 100.78 * 10_000) / 10_000
    assert plan["shares"] * plan["fill"] <= 2000.0


def test_wick_only_breakout_bar_is_the_trap():
    b = flat()
    setb(b, 575, 100, 101, 100, 100)
    setb(b, 580, 100, 100, 99, 100)
    setb(b, 590, 100.5, 101.5, 100.4, 100.8)
    st, s = E.find_setup(sd_of(b))
    assert st == "signal" and s.index == 20 and s.swing_high == 101.5


@pytest.mark.parametrize("high, status", [(103.4, "signal"), (103.41, "runaway")])
def test_runaway_breakout_voids_the_day(high, status):
    assert E.find_setup(sd_of(fade_bars(breakout_high=high)))[0] == status


def test_runaway_after_breakout_before_trap_voids_the_day():
    b = fade_bars(trap=False)
    setb(b, 600, 101.2, 103.5, 101.1, 101.2)
    setb(b, 610, 101.2, 101.3, 100.6, 100.8)  # trap after the runaway
    assert E.find_setup(sd_of(b))[0] == "runaway"


def test_min_range_no_breakout_no_trap():
    assert E.find_setup(sd_of(fade_bars(), atr=2.0 / 0.15 + 0.01))[0] == "min_range"
    b = flat()
    setb(b, 575, 100, 101, 100, 100)
    setb(b, 580, 100, 100, 99, 100)
    assert E.find_setup(sd_of(b))[0] == "no_breakout"
    assert E.find_setup(sd_of(fade_bars(trap=False)))[0] == "no_trap"


@pytest.mark.parametrize("t, status", [(839, "signal"), (840, "late_trap")])
def test_trap_window_ends_1359(t, status):
    b = fade_bars(trap=False)
    setb(b, t, 101.2, 101.3, 100.6, 100.8)
    fill_from(b, t + 1, 100.8)
    st, s = E.find_setup(sd_of(b))
    assert st == status
    if s is not None:
        assert s.index == t - 570


def test_only_bars_up_to_the_trap_are_used():
    b1, b2 = fade_bars(), fade_bars()
    setb(b2, 700, 100.8, 110.0, 90.0, 100.8)  # later bars cannot change the setup
    assert E.find_setup(sd_of(b1)) == E.find_setup(sd_of(b2))


def test_stop_beyond_fill_skips():
    b = fade_bars()
    setb(b, 592, 101.6, 101.6, 100.8, 100.8)  # open above the stop
    sd = sd_of(b)
    _st, s = E.find_setup(sd)
    assert E.entry_plan(sd, s, 2000.0) == (None, "stop_beyond_fill")


# ---------------------------------------------------------------- exits
def test_tp1_at_vwap_then_tp2_two_r():
    b = fade_bars()
    setb(b, 600, 100.8, 100.8, 99.9, 100.0)
    fill_from(b, 601, 100.0)
    setb(b, 610, 100.0, 100.0, 99.2, 99.3)
    sd, _s, plan, tr = trade_of(b)
    v = sd.vwap[29]  # bar index of 09:59 (VWAP known at the start of the 10:00 bar)
    assert [(leg.minute, leg.reason) for leg in tr.legs] == [(600, "tp1_vwap"), (610, "tp2_2r")]
    assert tr.legs[0].raw == pytest.approx(v) and tr.legs[0].fill == pytest.approx(v + 0.02)
    assert tr.legs[0].shares == round(plan["shares"] / 2, 4)
    assert tr.legs[1].raw == pytest.approx(99.30) and tr.exit_reason == "tp2_2r" and tr.tp1
    n = plan["shares"]
    want = sum(-(leg.fill - plan["fill"]) * leg.shares for leg in tr.legs) - 0.005 * 2 * n
    assert tr.net_pnl == pytest.approx(want) and tr.side == -1


def test_stop_exits_all_with_slippage_and_has_priority():
    b = fade_bars()
    setb(b, 595, 101.0, 101.6, 99.0, 100.0)  # stop and VWAP / TP2 in the same bar -> stop
    _sd, _s, plan, tr = trade_of(b)
    assert [(leg.minute, leg.reason, leg.shares) for leg in tr.legs] == [(595, "stop", plan["shares"])]
    assert tr.legs[0].raw == pytest.approx(101.52) and tr.legs[0].fill == pytest.approx(101.54)


def test_stop_gap_through_fills_at_open():
    b = fade_bars()
    setb(b, 595, 102.0, 102.1, 101.9, 102.0)
    tr = trade_of(b)[3]
    assert tr.legs[0].raw == 102.0 and tr.exit_reason == "stop"


def test_breakeven_after_tp1_from_next_bar():
    b = fade_bars()
    setb(b, 600, 100.8, 100.8, 99.9, 100.0)
    fill_from(b, 601, 100.0)
    setb(b, 605, 100.0, 100.9, 100.0, 100.5)
    _sd, _s, plan, tr = trade_of(b)
    assert [leg.reason for leg in tr.legs] == ["tp1_vwap", "breakeven"]
    assert tr.legs[1].raw == pytest.approx(plan["fill"]) and tr.exit_reason == "breakeven"


def test_tp1_bar_does_not_use_breakeven_stop_on_the_same_bar():
    b = fade_bars()
    setb(b, 600, 100.8, 100.85, 99.9, 100.0)  # high above the fill 100.78, below the initial stop
    fill_from(b, 601, 100.0)
    tr = trade_of(b)[3]
    assert [(leg.minute, leg.reason) for leg in tr.legs] == [(600, "tp1_vwap"), (950, "flatten")]


def test_tp2_before_tp1_exits_everything():
    b = fade_bars()
    for t in range(570, 585):
        setb(b, t, 97.0, 97.1, 96.9, 97.0, 1_000_000.0)
    setb(b, 575, 97.0, 101.0, 96.9, 97.0, 1_000_000.0)  # ORB 96.9..101, heavy volume at 97 -> VWAP ~97
    setb(b, 600, 100.8, 100.8, 99.2, 99.4)
    sd, _s, plan, tr = trade_of(b)
    assert sd.vwap[29] < plan["tp2"] == pytest.approx(99.30)
    assert [(leg.reason, leg.shares) for leg in tr.legs] == [("tp2_2r", plan["shares"])] and not tr.tp1


def test_tp2_orb_low_when_closer_than_two_r():
    b = fade_bars()
    setb(b, 580, 100, 100, 99.5, 100)  # ORB low 99.5 > 99.30
    setb(b, 600, 100.8, 100.8, 99.4, 99.5)
    _sd, _s, plan, tr = trade_of(b, atr=2.0)
    assert plan["tp2"] == 99.5 and tr.exit_reason == "tp2_orb_low"
    assert [leg.reason for leg in tr.legs] == ["tp1_vwap", "tp2_orb_low"]  # same bar: TP1 then TP2


def test_flatten_at_1550_open():
    b = fade_bars()
    fill_from(b, 592, 100.5)
    for t in range(592, 960):
        b[t - 570] = Bar(t, 100.5, 100.5, 100.5, 100.5, 1000.0)  # VWAP stays below 100.5 -> no TP1
    sd, _s, _plan, tr = trade_of(b)
    assert max(sd.vwap) < 100.5
    assert [(leg.minute, leg.reason) for leg in tr.legs] == [(950, "flatten")]


# ---------------------------------------------------------------- day loop / report / CLI
def _days(n):
    from src.history.exchange_calendar import trading_days
    return trading_days(date(2024, 1, 2), date(2024, 4, 30))[:n]


def test_run_backtest_one_trade_per_day_and_statuses():
    days = _days(3)
    sds = {}
    for k, d in enumerate(days):
        sd = sd_of(fade_bars() if k != 1 else fade_bars(trap=False))
        sd.day = d.isoformat()
        sds[d.isoformat()] = sd
    # a second trap later on day 0 is ignored
    setb(sds[days[0].isoformat()].bars, 700, 101.0, 101.4, 100.6, 100.8)
    res, setups = E.run_backtest(sds, days)
    assert [d.status for d in res.days] == ["traded", "no_trap", "traded"]
    assert len(res.trades) == 2 and len(setups) == 2
    assert res.skips == {"no_trap": 1, "traded": 2}
    m = BacktestReporter(res).metrics()
    assert m["trade_count"] == 2 and m["long_short"]["short"]["trades"] == 2


def test_preflight_matches_engine_statuses():
    pre = _load_script("explore_fade_preflight")
    days = _days(2)
    sds = {}
    for k, d in enumerate(days):
        sd = sd_of(fade_bars(trap=k == 0))
        sd.day = d.isoformat()
        sds[d.isoformat()] = sd
    r = pre.preflight(sds, [d.isoformat() for d in days])
    res, _ = E.run_backtest(sds, days)
    assert r["counts"] == {"signal": 1, "no_trap": 1} and res.skips == {"traded": 1, "no_trap": 1}
    assert r["stop_pct"] == [pytest.approx(0.74 / 100.78)]


def test_cli_writes_json_once(tmp_path):
    cli = _load_script("explore_false_breakout_fade_v1")
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(2):
            _write_day(store, "QQQ", d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    md = cli.run(db, out, start=d1, end=d1 + timedelta(days=1))
    doc = json.loads((out / cli.OUT_NAME).read_text())
    assert doc["provenance"]["symbols"] == ["QQQ"] and doc["setups"] == []
    assert doc["provenance"]["spec_path"] == "docs/research/EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.md"
    assert set(doc["metrics"]["exit_legs"]) == set(cli.LEG_REASONS) and isinstance(md, str)
    with pytest.raises(SystemExit):
        cli.run(db, out, start=d1, end=d1 + timedelta(days=1))


def test_leg_counts():
    cli = _load_script("explore_false_breakout_fade_v1")
    b = fade_bars()
    setb(b, 600, 100.8, 100.8, 99.9, 100.0)
    fill_from(b, 601, 100.0)
    setb(b, 610, 100.0, 100.0, 99.2, 99.3)
    tr = trade_of(b)[3]
    c = cli.leg_counts([tr])
    assert c["tp1_vwap"]["legs"] == 1 and c["tp2_2r"]["trades"] == 1 and c["stop"]["legs"] == 0
    assert c["tp1_vwap"]["pnl_before_commission"] == pytest.approx(-(tr.legs[0].fill - tr.entry_fill) * tr.legs[0].shares)
