"""QQQ overnight drift long (docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.md).

Synthetic data only: no test reads data/*.sqlite. A "strong" day opens at 100, drifts to 101 by 15:50 (close above the
open and the VWAP), prior-day daily EMA20 99.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_overnight_drift_v1 import engine as E
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import _write_day, make_sd
from tests.test_explore_pullback_vwap_v1 import _load_script


def strong_bars(close=960, open_=100.0, end=101.0):
    """Linear drift open_ -> end over the session; every bar's open = previous close."""
    n = close - 570
    b = []
    for i in range(n):
        p0 = open_ + (end - open_) * i / n
        p1 = open_ + (end - open_) * (i + 1) / n
        b.append(Bar(570 + i, p0, max(p0, p1), min(p0, p1), p1, 1000.0))
    return b


def sd_of(bars, day, ema=99.0, prior_close=100.0):
    sd = make_sd(bars, symbol="QQQ", day=day)
    sd.daily_ema, sd.daily_atr, sd.prior_close = ema, 1.0, prior_close
    return sd


def week(n=5):
    return [d.isoformat() for d in trading_days(date(2024, 3, 4), date(2024, 4, 30))[:n]]


# ---------------------------------------------------------------- entry status
def test_strong_day_enters():
    d = week(2)
    assert E.entry_status(sd_of(strong_bars(), d[0]), sd_of(strong_bars(), d[1])) == "enter"


def test_filter_order_and_each_filter():
    d = week(2)
    nxt = sd_of(strong_bars(), d[1])
    assert E.entry_status(sd_of(strong_bars(), d[0], ema=101.5), nxt) == "below_ema"
    assert E.entry_status(sd_of(strong_bars(open_=102.0, end=101.0), d[0]), nxt) == "below_open"
    b = strong_bars()
    for t in range(700, 720):  # heavy volume at 104 pulls the VWAP above the 15:50 close
        b[t - 570] = Bar(t, 104.0, 104.0, 104.0, 104.0, 1_000_000.0)
    assert E.entry_status(sd_of(b, d[0]), nxt) == "below_vwap"
    # the benchmark ignores the three filters only
    assert E.entry_status(sd_of(strong_bars(), d[0], ema=101.5), nxt, filters=False) == "enter"


def test_strict_inequalities():
    d = week(2)
    nxt = sd_of(strong_bars(), d[1])
    sd = sd_of(strong_bars(), d[0])
    c = E.bar(sd, E.SIGNAL_MIN).close
    sd.daily_ema = c
    assert E.entry_status(sd, nxt) == "below_ema"


def test_no_entry_days():
    d = week(2)
    nxt = sd_of(strong_bars(), d[1])
    early = sd_of(strong_bars(close=780), d[0])
    assert E.entry_status(early, nxt) == "early_close"
    assert E.entry_status(sd_of(strong_bars(), d[0]), None) == "no_next_day"
    bad = sd_of(strong_bars(), d[1])
    bad.complete = False
    assert E.entry_status(sd_of(strong_bars(), d[0]), bad) == "next_incomplete"
    inc = sd_of(strong_bars(), d[0])
    inc.complete = False
    assert E.entry_status(inc, nxt) == "excluded_incomplete"
    nr = sd_of(strong_bars(), d[0])
    nr.daily_ema = None
    assert E.entry_status(nr, nxt) == "daily_not_ready"


def test_signal_uses_only_bars_through_1550():
    d = week(2)
    nxt = sd_of(strong_bars(), d[1])
    b1, b2 = strong_bars(), strong_bars()
    b2[955 - 570] = Bar(955, 50.0, 50.0, 50.0, 50.0, 1e9)  # after 15:50
    assert E.entry_status(sd_of(b1, d[0]), nxt) == E.entry_status(sd_of(b2, d[0]), nxt) == "enter"


# ---------------------------------------------------------------- trade
def test_trade_prices_sizing_costs():
    d = week(2)
    sd = sd_of(strong_bars(), d[0])
    nxt = sd_of(strong_bars(open_=101.5, end=102.0), d[1])
    tr, hold = E.overnight_trade(sd, nxt, 2000.0)
    raw = E.bar(sd, 951).open
    assert tr.entry_minute == 951 and tr.entry_raw == raw and tr.entry_fill == pytest.approx(raw + 0.02)
    assert tr.shares == round(2000.0 * 0.95 / (raw + 0.02), 4)
    leg = tr.legs[0]
    assert (leg.minute, leg.reason, leg.shares) == (575, "overnight_exit", tr.shares)
    assert leg.raw == E.bar(nxt, 575).open and leg.fill == pytest.approx(leg.raw - 0.02)
    want = (leg.fill - tr.entry_fill) * tr.shares - 0.005 * 2 * tr.shares
    assert tr.net_pnl == pytest.approx(want) and tr.exit_reason == "overnight_exit"
    ref = (leg.raw - raw) * tr.shares - 0.001 * tr.shares * (raw + leg.raw)
    assert tr.ref_pnl == pytest.approx(ref)
    assert hold.hours == pytest.approx((24 * 60 + 575 - 951) / 60)


@pytest.mark.parametrize("gap, reason, minute", [(-0.015, "disaster_stop", 571), (-0.0149, "overnight_exit", 575)])
def test_disaster_stop_boundary(gap, reason, minute):
    d = week(2)
    sd = sd_of(strong_bars(), d[0])
    d_close = sd.bars[-1].close
    nxt = sd_of(strong_bars(open_=d_close * (1 + gap), end=d_close), d[1])
    tr, _ = E.overnight_trade(sd, nxt, 2000.0)
    assert (tr.exit_reason, tr.legs[0].minute) == (reason, minute)
    assert tr.legs[0].raw == E.bar(nxt, minute).open


def test_weekend_holding_hours():
    fri, mon = "2024-03-08", "2024-03-11"
    _tr, hold = E.overnight_trade(sd_of(strong_bars(), fri), sd_of(strong_bars(), mon), 2000.0)
    assert hold.hours == pytest.approx((3 * 24 * 60 + 575 - 951) / 60)


# ---------------------------------------------------------------- day loop
def _week_data(n, builders=None):
    days = trading_days(date(2024, 3, 4), date(2024, 4, 30))[:n]
    qqq = {}
    for k, d in enumerate(days):
        f = builders[k] if builders else strong_bars
        qqq[d.isoformat()] = sd_of(f(), d.isoformat())
    return days, qqq


def test_run_backtest_compounds_and_skips_last_day():
    days, qqq = _week_data(4)
    res, holds = E.run_backtest(qqq, days)
    assert [d.status for d in res.days] == ["traded", "traded", "traded", "no_next_day"]
    assert len(res.trades) == len(holds) == 3
    t1, t2 = res.trades[:2]
    assert res.days[1].start_equity == pytest.approx(2000.0 + t1.net_pnl)
    assert t2.shares == round(res.days[1].start_equity * 0.95 / t2.entry_fill, 4)
    assert res.skips == {"no_next_day": 1}
    assert BacktestReporter(res).metrics()["trade_count"] == 3


def test_benchmark_trades_on_filtered_days():
    weak = lambda: strong_bars(open_=102.0, end=101.0)
    days, qqq = _week_data(3, [weak, strong_bars, strong_bars])
    res, _ = E.run_backtest(qqq, days)
    bench, _ = E.run_backtest(qqq, days, filters=False)
    assert [d.status for d in res.days] == ["below_open", "traded", "no_next_day"]
    assert [d.status for d in bench.days] == ["traded", "traded", "no_next_day"]


def test_first_scored_day_needs_ema():
    days, qqq = _week_data(3)
    qqq[days[0].isoformat()].daily_ema = None
    res, _ = E.run_backtest(qqq, days)
    assert res.days[0].day == days[1].isoformat()


def test_return_split():
    days, qqq = _week_data(2)
    iso = [d.isoformat() for d in days]
    qqq[iso[1]].prior_close = 100.5
    s = E.return_split(qqq, iso)
    on = (qqq[iso[0]].bars[0].open / 100.0) * (qqq[iso[1]].bars[0].open / 100.5)
    intra = (qqq[iso[0]].bars[-1].close / qqq[iso[0]].bars[0].open) * (qqq[iso[1]].bars[-1].close / qqq[iso[1]].bars[0].open)
    assert s["days"] == 2 and s["overnight_return"] == pytest.approx(on - 1)
    assert s["intraday_return"] == pytest.approx(intra - 1)
    assert s["combined_return"] == pytest.approx(on * intra - 1)


def test_preflight_matches_engine():
    pre = _load_script("explore_overnight_preflight")
    weak = lambda: strong_bars(open_=102.0, end=101.0)
    days, qqq = _week_data(3, [weak, strong_bars, strong_bars])
    r = pre.preflight(qqq, [d.isoformat() for d in days])
    res, _ = E.run_backtest(qqq, days)
    assert r["counts"] == {"below_open": 1, "enter": 1, "no_next_day": 1}
    assert res.skips == {"below_open": 1, "no_next_day": 1} and len(res.trades) == 1


def test_cli_writes_json_once(tmp_path):
    cli = _load_script("explore_overnight_drift_v1")
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(2):
            _write_day(store, "QQQ", d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    md = cli.run(db, out, start=d1, end=d1 + timedelta(days=1))
    doc = json.loads((out / cli.OUT_NAME).read_text())
    assert doc["provenance"]["spec_path"] == "docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.md"
    assert set(doc["benchmarks"]) == {"no_filter_overnight", "qqq_return_split"} and doc["holds"] == []
    assert isinstance(md, str)
    with pytest.raises(SystemExit):
        cli.run(db, out, start=d1, end=d1 + timedelta(days=1))

