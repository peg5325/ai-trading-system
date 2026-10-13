"""Exploratory ORB + VWAP pullback engine (docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md).

Synthetic data only: no test reads data/*.sqlite.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0 import data_loader as DL
from src.explore_orb_vwap_v0.data_loader import (
    DataLoader,
    DevBoundError,
    assert_dev_window,
    is_complete,
)
from src.explore_orb_vwap_v0.indicators import (
    IndicatorState,
    completed_five,
    completes_five,
    five_minute_bars,
    vwap_series,
)
from src.explore_orb_vwap_v0.reporter import BacktestReporter, drawdown
from src.explore_orb_vwap_v0.risk import RiskManager
from src.explore_orb_vwap_v0.strategy import (
    StrategyEngine,
    run_backtest,
    support_signal,
    swing_and_atr,
)
from src.explore_orb_vwap_v0.universe import (
    Candidate,
    SymbolDay,
    UniverseFilter,
    orb,
    prepare_symbol,
)
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore, ImportStats
from src.models import ET, MARKET_US, Candle

DAY = "2024-03-04"


# ---------------------------------------------------------------- helpers
def flat(close=960, p=100.0, v=1000.0, start=570):
    return [Bar(t, p, p, p, p, v) for t in range(start, close)]


def setb(bars, t, o, h, low, c, v=1000.0):
    bars[t - 570] = Bar(t, o, h, low, c, v)


def fill_from(bars, t0, p, v=1000.0):
    for t in range(t0, bars[-1].t + 1):
        bars[t - 570] = Bar(t, p, p, p, p, v)


def make_sd(bars, atr=2.0, ema=0.0, symbol="AAA", day=DAY):
    sd = SymbolDay(symbol, day, True, True)
    sd.bars = bars
    sd.close_minute = bars[-1].t + 1
    sd.prior_close = 100.0
    sd.rvol_base = 1.0
    sd.warm = True
    sd.orb_high, sd.orb_low, sd.orb_volume = orb(bars)
    sd.vwap = vwap_series(bars)
    sd.five = five_minute_bars(bars)
    for f in sd.five:
        f.atr, f.ema = atr, ema
    return sd


def long_bars(close=960):
    """ORB 99..101, breakout close at 09:50 (590), flat 101.5, support candle at 10:00 (600), flat 101.5 after."""
    b = flat(close)
    setb(b, 575, 100, 101, 100, 100)
    setb(b, 580, 100, 100, 99, 100)
    setb(b, 590, 100.5, 101.6, 100.4, 101.5)
    fill_from(b, 591, 101.5)
    setb(b, 600, 101.4, 101.7, 101.0, 101.6)
    return b


def mirror(bars):
    return [Bar(x.t, 200 - x.open, 200 - x.low, 200 - x.high, 200 - x.close, x.volume) for x in bars]


def run_day(sd_list, equity=2000.0):
    return StrategyEngine().simulate_day(DAY, sd_list, equity)


# ---------------------------------------------------------------- indicators
def test_vwap_is_typical_price_volume_weighted_and_causal():
    bars = [Bar(570, 10, 12, 8, 11, 100), Bar(571, 11, 13, 10, 12, 300), Bar(572, 1, 1, 1, 1, 0)]
    v = vwap_series(bars)
    tp0, tp1 = (12 + 8 + 11) / 3, (13 + 10 + 12) / 3
    assert v[0] == pytest.approx(tp0)
    assert v[1] == pytest.approx((tp0 * 100 + tp1 * 300) / 400)
    assert v[2] == pytest.approx(v[1])  # zero volume adds nothing
    changed = vwap_series(bars[:2] + [Bar(572, 50, 60, 40, 55, 10_000)])
    assert changed[:2] == v[:2]
    assert vwap_series([Bar(570, 1, 1, 1, 1, 0)]) == [None]


def test_five_minute_bars_bucket_from_0930():
    bars = [Bar(570 + i, 100 + i, 101 + i, 99 + i, 100.5 + i, 1) for i in range(10)]
    five = five_minute_bars(bars)
    assert [f.start for f in five] == [570, 575]
    assert (five[0].open, five[0].high, five[0].low, five[0].close) == (100, 105, 99, 104.5)
    assert (five[1].open, five[1].high, five[1].low, five[1].close) == (105, 110, 104, 109.5)
    assert completed_five(573) == 0 and completed_five(574) == 1 and completed_five(599) == 6
    assert completes_five(574) and completes_five(579) and not completes_five(575)
    with pytest.raises(ValueError):
        five_minute_bars([Bar(571 + i, 1, 1, 1, 1, 1) for i in range(5)])


def test_atr_wilder_and_ema_seed_with_simple_means():
    st = IndicatorState()
    for i in range(13):
        assert st.update(102, 100, 101)[0] is None
    atr, ema = st.update(102, 100, 101)
    assert atr == pytest.approx(2.0) and ema is None
    atr, _ = st.update(110, 100, 105)  # TR uses prev close 101: max(10, 9, 1) = 10
    assert atr == pytest.approx((2.0 * 13 + 10) / 14)
    for _ in range(5):  # 14 + 1 + 5 = 20 closes
        _, ema = st.update(102, 100, 101)
    assert ema == pytest.approx((101 * 19 + 105) / 20)
    _, ema2 = st.update(102, 100, 111)
    assert ema2 == pytest.approx(ema + 2 / 21 * (111 - ema))


# ---------------------------------------------------------------- dev bound / loader
def test_dev_bound_refuses_requests_reaching_2026_01_21(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("research_load must not be called")

    monkeypatch.setattr(DL, "research_load", boom)
    db = str(tmp_path / "none.sqlite")
    with pytest.raises(DevBoundError):
        DataLoader(db, date(2025, 12, 1), date(2026, 1, 21))
    with pytest.raises(DevBoundError):
        DataLoader(db, date(2026, 1, 21), date(2026, 2, 1))
    with pytest.raises(DevBoundError):
        assert_dev_window(datetime(2026, 1, 20, tzinfo=ET), datetime(2026, 1, 21, 0, 1, tzinfo=ET))
    assert_dev_window(datetime(2026, 1, 20, tzinfo=ET), datetime(2026, 1, 21, tzinfo=ET))
    ok = DataLoader(db, date(2025, 12, 1), date(2026, 1, 20))
    ok.end = date(2026, 1, 21)  # tampering after construction is still refused per chunk, before any read
    with pytest.raises(DevBoundError):
        ok.load_symbol("SPY")


def _write_day(store, symbol, d, p=100.0, skip_label=None, extra_pre_post=True):
    labels = list(range(571, 961))  # bar END minutes 09:31 .. 16:00 -> starts 09:30 .. 15:59
    if extra_pre_post:
        labels = [570] + labels + [961]  # 09:30 label = PRE bar 09:29; 16:01 label = POST bar 16:00
    candles = []
    for m in labels:
        if m == skip_label:
            continue
        moment = datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)
        candles.append(Candle.from_moment(symbol=symbol, interval="1m", moment=moment, o=p, h=p + 0.5,
                                          low=p - 0.5, c=p, v=1000.0, currency="USD", adjusted=True,
                                          market=MARKET_US))
    store.upsert_candles(candles, source="toss_backfill", run_id="r", stats=ImportStats(source="toss_backfill"))


def test_loader_shifts_end_labels_and_keeps_regular_only(tmp_path):
    db = str(tmp_path / "m.sqlite")
    d1, d2 = date(2024, 3, 4), date(2024, 3, 5)
    with HistoryStore(db) as store:
        _write_day(store, "SPY", d1)
        _write_day(store, "SPY", d2, skip_label=700)
        store.commit()
    sb = DataLoader(db, d1, d2).load_symbol("SPY")
    b1, b2 = sb.regular[d1.isoformat()], sb.regular[d2.isoformat()]
    assert [b.t for b in b1] == list(range(570, 960))
    assert is_complete(b1, d1)
    assert len(b2) == 389 and not is_complete(b2, d2)


def test_is_complete_uses_early_close_calendar():
    d = date(2024, 11, 29)  # early close 13:00
    assert is_complete(flat(780), d)
    assert not is_complete(flat(960), d)
    assert not is_complete(flat(779), d)
    assert not is_complete(flat(960), date(2024, 3, 9))  # Saturday


# ---------------------------------------------------------------- universe
def _days(n=24):
    return trading_days(date(2024, 1, 2), date(2024, 2, 5))[:n]


def _warm_day(orb_vol=1000.0, p=100.0):
    b = [Bar(t, p, p + 0.5, p - 0.5, p, 1000.0) for t in range(570, 960)]
    for t in range(570, 585):
        b[t - 570] = Bar(t, p, p + 0.5, p - 0.5, p, orb_vol / 15)
    return b


def test_incomplete_symbol_day_excluded_and_breaks_continuity():
    days = _days(24)
    reg = {d.isoformat(): _warm_day() for d in days}
    bad = days[21].isoformat()
    reg[bad] = reg[bad][:100] + reg[bad][101:]
    prep = prepare_symbol("AAA", reg, days)
    assert prep[days[20].isoformat()].ready
    assert not prep[bad].complete and not prep[bad].ready
    nxt = prep[days[22].isoformat()]
    assert nxt.complete and nxt.prior_close is None and not nxt.warm and not nxt.ready
    after = prep[days[23].isoformat()]
    assert after.ready  # one complete day re-seeds ATR/EMA; RVOL skips the incomplete session
    spy = prepare_symbol("SPY", {d.isoformat(): _warm_day() for d in days}, days)
    assert UniverseFilter.metrics(prep[bad], spy[bad]) is None
    assert UniverseFilter.metrics(nxt, spy[nxt.day]) is None


def test_rvol_uses_mean_of_prior_20_complete_sessions():
    days = _days(23)
    reg = {d.isoformat(): _warm_day(orb_vol=1000.0 + 100 * k) for k, d in enumerate(days)}
    reg[days[5].isoformat()] = reg[days[5].isoformat()][:-1]  # incomplete: not in the RVOL history
    prep = prepare_symbol("AAA", reg, days)
    assert prep[days[19].isoformat()].rvol_base is None
    sd = prep[days[21].isoformat()]
    vols = [1000.0 + 100 * k for k in range(21) if k != 5]
    assert sd.rvol_base == pytest.approx(sum(vols[-20:]) / 20)
    assert sd.orb_volume == pytest.approx(1000.0 + 100 * 21)


def test_rvol_and_gap_filters_and_rs_ranking():
    uf = UniverseFilter()
    assert uf.passes(Candidate("A", 1.2, 0.003, 0))
    assert uf.passes(Candidate("A", 5.0, -0.02, 0))
    assert not uf.passes(Candidate("A", 1.19, 0.01, 0))
    assert not uf.passes(Candidate("A", 2.0, 0.0029, 0))
    assert not uf.passes(Candidate("A", 2.0, -0.0201, 0))

    def sd_with(sym, open_, c0944, vol):
        b = flat(960, 100.0)
        b[0] = Bar(570, open_, max(open_, 100), min(open_, 100), 100, vol / 15)
        for t in range(571, 585):
            b[t - 570] = Bar(t, 100, 100, 100, 100, vol / 15)
        b[14] = Bar(584, 100, max(100, c0944), min(100, c0944), c0944, vol / 15)
        sd = make_sd(b, symbol=sym)
        sd.rvol_base = 1000.0
        return sd

    spy = sd_with("SPY", 100.5, 100.2, 2000)
    rows = {
        "SPY": spy,
        "AAA": sd_with("AAA", 101.0, 101.0, 2000),  # rs +0.8%
        "BBB": sd_with("BBB", 99.0, 99.0, 2000),  # rs -1.2%
        "CCC": sd_with("CCC", 101.0, 103.0, 1100),  # rvol 1.1 -> out
        "DDD": sd_with("DDD", 103.0, 101.0, 2000),  # gap 3% -> out
    }
    wl = uf.watchlist(rows, spy)
    assert [c.symbol for c in wl] == ["BBB", "AAA"]
    assert wl[0].rs == pytest.approx(-0.012) and wl[1].gap == pytest.approx(0.01)
    assert UniverseFilter.metrics(spy, spy).rs == 0.0


# ---------------------------------------------------------------- risk
def test_sizing_floor_cash_cap_and_zero_shares():
    assert RiskManager.shares(2000, 2000, 50.0, 0.7) == 28  # floor(20 / 0.7) = 28, 28*50 <= 2000
    assert RiskManager.shares(2000, 2000, 101.52, 0.75) == 19  # floor(26.67) = 26 -> cash cap floor(19.7)
    assert RiskManager.shares(2000, 2000, 100.0, 25.0) == 0
    rm = RiskManager()
    plan, why = rm.plan(1, 100.0, swing=75.0, atr=100.0, day_start_equity=2000, cash=2000)
    assert plan is None and why == "zero_shares"
    plan, why = rm.plan(1, 100.0, swing=100.5, atr=1.0, day_start_equity=2000, cash=2000)
    assert plan is None and why == "stop_beyond_fill"
    plan, _ = rm.plan(1, 100.0, swing=99.0, atr=2.0, day_start_equity=2000, cash=2000)
    assert plan.fill == pytest.approx(100.02) and plan.stop == pytest.approx(99.0)  # nearer of swing/ATR
    assert plan.shares == 19 and plan.target == pytest.approx(100.02 + 1.5 * 1.02)
    plan, _ = rm.plan(-1, 100.0, swing=101.5, atr=0.5, day_start_equity=2000, cash=2000)
    assert plan.fill == pytest.approx(99.98) and plan.stop == pytest.approx(100.73)


def test_price_above_250_is_skipped():
    plan, why = RiskManager().plan(1, 250.01, swing=240, atr=5, day_start_equity=2000, cash=2000)
    assert plan is None and why == "price_above_250"
    b = [Bar(x.t, x.open * 3, x.high * 3, x.low * 3, x.close * 3, x.volume) for x in long_bars()]
    trades, skips = run_day([make_sd(b, atr=6.0)])
    assert trades == [] and skips == {"price_above_250": 1}


def test_kill_switch_triggers():
    assert RiskManager.kill_switch(2000, -50.0, 0)
    assert not RiskManager.kill_switch(2000, -49.99, 1)
    assert RiskManager.kill_switch(2000, 10.0, 2)


# ---------------------------------------------------------------- strategy
def test_entry_fills_next_open_and_flattens_at_1550():
    trades, _ = run_day([make_sd(long_bars())])
    assert len(trades) == 1
    tr = trades[0]
    assert (tr.side, tr.signal_minute, tr.entry_minute) == (1, 600, 601)
    assert tr.entry_raw == 101.5 and tr.entry_fill == pytest.approx(101.52)
    assert tr.initial_stop == pytest.approx(100.0)  # swing low 100 nearer than 101.52 - 3
    assert tr.shares == 13
    assert tr.exit_reason == "flatten" and [leg.minute for leg in tr.legs] == [950]
    assert tr.legs[0].fill == pytest.approx(101.48)
    assert tr.net_pnl == pytest.approx(13 * (101.48 - 101.52) - 0.005 * 26)
    assert tr.ref_pnl == pytest.approx(0 - 0.001 * (13 * 101.5 * 2))


def test_early_close_flattens_ten_minutes_before_close():
    trades, _ = run_day([make_sd(long_bars(close=780))])
    assert trades[0].exit_reason == "flatten" and trades[0].legs[0].minute == 770


def test_no_look_ahead():
    base = long_bars()
    sd = make_sd(base)
    sig = support_signal(sd, 30, 590, None)
    swing = swing_and_atr(sd, 600, 1)
    future = base[:31] + [Bar(t, 150, 190, 10, 20, 9e6) for t in range(601, 960)]
    sd_f = make_sd(future)
    assert support_signal(sd_f, 30, 590, None) == sig == 1
    assert swing_and_atr(sd_f, 600, 1) == swing
    # bar 600 itself is not yet in a completed 5-minute bar at t=600; changing it leaves the swing unchanged
    b2 = list(base)
    setb(b2, 600, 101.4, 101.7, 50.0, 101.6)
    assert swing_and_atr(make_sd(b2), 600, 1) == swing
    # the fill is the next bar's open
    b3 = list(base)
    setb(b3, 601, 101.7, 101.7, 101.7, 101.7)
    trades, _ = run_day([make_sd(b3)])
    assert trades[0].entry_raw == 101.7 and trades[0].signal_minute == 600
    # a support candle that is itself the first breakout bar is not a signal
    b4 = flat()
    setb(b4, 575, 100, 101, 100, 100)
    setb(b4, 580, 100, 100, 99, 100)
    setb(b4, 600, 101.4, 101.7, 101.0, 101.6)
    fill_from(b4, 601, 100.5)
    assert run_day([make_sd(b4)])[0] == []


def test_signal_window_starts_at_1000():
    b = long_bars()
    setb(b, 600, 101.5, 101.5, 101.5, 101.5)
    setb(b, 595, 101.4, 101.7, 101.0, 101.6)
    assert run_day([make_sd(b)])[0] == []


def test_stop_first_when_stop_and_tp1_touch_same_bar():
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 99.9, 101.5)
    tr = run_day([make_sd(b)])[0][0]
    assert tr.exit_reason == "stop" and not tr.tp1 and len(tr.legs) == 1
    assert tr.legs[0].raw == 100.0 and tr.legs[0].fill == pytest.approx(99.98)


def test_stop_gap_through_exits_at_open():
    b = long_bars()
    fill_from(b, 605, 99.0)
    tr = run_day([make_sd(b)])[0][0]
    assert tr.exit_reason == "stop" and tr.legs[0].raw == 99.0 and tr.legs[0].fill == pytest.approx(98.98)


def test_tp1_half_then_breakeven_from_next_bar():
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)  # TP1 103.8 hit; low 101.0 < BE 101.52 but BE starts next bar
    tr = run_day([make_sd(b)])[0][0]
    assert tr.tp1 and [(leg.minute, leg.shares, leg.reason) for leg in tr.legs] == [(605, 6, "tp1"),
                                                                                    (606, 7, "breakeven")]
    assert tr.legs[0].raw == pytest.approx(101.52 + 1.5 * 1.52) and tr.legs[0].fill == pytest.approx(103.78)
    assert tr.legs[1].raw == pytest.approx(101.5) and tr.legs[1].fill == pytest.approx(101.48)
    assert tr.exit_reason == "breakeven"


def test_tp1_with_one_share_exits_all():
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)
    tr = run_day([make_sd(b)], equity=200.0)[0][0]
    assert tr.shares == 1 and tr.exit_reason == "tp1" and len(tr.legs) == 1


def test_ema20_exit_only_after_tp1():
    no_tp1 = run_day([make_sd(long_bars(), ema=200.0)])[0][0]
    assert no_tp1.exit_reason == "flatten"
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.6, 103.0)
    fill_from(b, 606, 102.5)
    tr = run_day([make_sd(b, ema=200.0)])[0][0]
    assert [leg.reason for leg in tr.legs] == ["tp1", "ema20"]
    assert tr.legs[1].minute == 610 and tr.legs[1].fill == pytest.approx(102.48)  # 609 closes 5m bar
    held = run_day([make_sd(b, ema=0.0)])[0][0]
    assert [leg.reason for leg in held.legs] == ["tp1", "flatten"]


def test_short_is_mirror_of_long():
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)
    long_tr = run_day([make_sd(b)])[0][0]
    short_tr = run_day([make_sd(mirror(b), ema=200.0)])[0][0]
    assert short_tr.side == -1 and long_tr.side == 1
    assert short_tr.entry_fill == pytest.approx(200 - long_tr.entry_fill)
    assert short_tr.initial_stop == pytest.approx(200 - long_tr.initial_stop)
    assert short_tr.shares == long_tr.shares
    assert [(x.minute, x.shares, x.reason) for x in short_tr.legs] == [(x.minute, x.shares, x.reason)
                                                                       for x in long_tr.legs]
    assert short_tr.net_pnl == pytest.approx(long_tr.net_pnl)
    # reference cost is a % of notional, and the mirrored prices are ~99 instead of ~101, so it differs slightly


def _two_losses_bars(gap_loss=False):
    b = long_bars()
    if gap_loss:
        setb(b, 603, 95.0, 95.0, 95.0, 95.0)
    else:
        setb(b, 603, 101.5, 101.5, 99.9, 101.5)
    setb(b, 610, 101.4, 101.7, 101.0, 101.6)
    setb(b, 613, 101.5, 101.5, 99.8, 101.5)
    setb(b, 620, 101.4, 101.7, 101.0, 101.6)
    return b


def test_kill_switch_two_losing_trades(monkeypatch):
    trades, _ = run_day([make_sd(_two_losses_bars())])
    assert [t.signal_minute for t in trades] == [600, 610]
    assert all(t.net_pnl < 0 for t in trades)
    assert trades[0].net_pnl > -0.025 * 2000  # the count rule, not the loss rule, stops the 3rd entry
    monkeypatch.setattr(RiskManager, "kill_switch", staticmethod(lambda *a: False))
    assert [t.signal_minute for t in run_day([make_sd(_two_losses_bars())])[0]] == [600, 610, 620]


def test_kill_switch_daily_loss(monkeypatch):
    trades, _ = run_day([make_sd(_two_losses_bars(gap_loss=True))])
    assert len(trades) == 1 and trades[0].net_pnl <= -0.025 * 2000
    monkeypatch.setattr(RiskManager, "kill_switch", staticmethod(lambda *a: False))
    assert len(run_day([make_sd(_two_losses_bars(gap_loss=True))])[0]) > 1


def test_reentry_needs_new_support_candle_after_exit():
    b = long_bars()
    setb(b, 603, 101.5, 101.5, 99.9, 101.5)  # stop; no later support candle
    trades, _ = run_day([make_sd(b)])
    assert len(trades) == 1


def test_one_position_at_a_time():
    a, c = make_sd(long_bars(), symbol="AAA"), make_sd(long_bars(), symbol="BBB")
    trades, _ = run_day([a, c])
    assert [t.symbol for t in trades] == ["AAA"]


# ---------------------------------------------------------------- end to end (synthetic)
def test_run_backtest_and_report_on_synthetic_days():
    days = _days(23)
    aaa = {d.isoformat(): _warm_day() for d in days}
    spy = {d.isoformat(): _warm_day() for d in days}
    last = long_bars()
    last[0] = Bar(570, 101.0, 101.0, 100.0, 100.0, 2000 / 15)
    for t in range(571, 585):
        o = last[t - 570]
        last[t - 570] = Bar(t, o.open, o.high, o.low, o.close, 2000 / 15)
    aaa[days[-1].isoformat()] = last
    prepared = {"AAA": prepare_symbol("AAA", aaa, days), "SPY": prepare_symbol("SPY", spy, days)}
    res = run_backtest(prepared, days)
    assert res.days[0].day == days[20].isoformat()
    assert [d.status for d in res.days] == ["no_candidates", "no_candidates", "traded"]
    assert res.days[-1].watchlist == ["AAA"]
    assert len(res.trades) == 1 and res.trades[0].side == 1
    m = BacktestReporter(res).metrics()
    assert m["total_trading_days"] == 3 and m["trade_days"] == 1 and m["no_trade_days"] == 2
    assert m["final_equity"] == pytest.approx(2000 + res.trades[0].net_pnl)
    assert m["reference_cost"]["final_equity"] == pytest.approx(2000 + res.trades[0].ref_pnl)
    assert "## Summary" in BacktestReporter(res).markdown()


def test_drawdown_metrics():
    mdd, pct, longest = drawdown(100.0, [110, 99, 105, 120, 118, 119, 121])
    assert mdd == pytest.approx(11) and pct == pytest.approx(11 / 110) and longest == 2


def test_cli_writes_json_with_provenance(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "explore_cli", __file__.replace("tests/test_explore_orb_vwap_v0.py", "scripts/explore_pullback_vwap_v0.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(2):
            _write_day(store, "SPY", d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    md = cli.run(db, out, symbols=("SPY",), start=d1, end=d1 + timedelta(days=1))
    doc = json.loads((out / "result.json").read_text())
    assert len(doc["provenance"]["spec_sha256"]) == 64 and doc["provenance"]["git_head"]
    assert doc["provenance"]["rows_read"] == {"SPY": 2 * 392}
    assert md == "No scored days.\n"
    with pytest.raises(SystemExit):
        cli.run(db, out, symbols=("SPY",), start=d1, end=d1 + timedelta(days=1))


def test_bar_with_open_outside_range_makes_day_incomplete():
    from datetime import date

    from src.daytrade_paper.core import Bar
    from src.daytrade_paper.store import regular_close_minute
    from src.explore_orb_vwap_v0.data_loader import OPEN_MIN, is_complete

    day = date(2025, 3, 3)
    close = regular_close_minute(day)
    bars = [Bar(t, 10.0, 10.1, 9.9, 10.0, 100.0) for t in range(OPEN_MIN, close)]
    assert is_complete(bars, day)
    bars[5] = Bar(bars[5].t, 10.2, 10.1, 9.9, 10.0, 100.0)  # open above high
    assert not is_complete(bars, day)
