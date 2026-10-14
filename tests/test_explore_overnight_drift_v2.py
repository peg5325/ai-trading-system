"""Overnight drift V2 ablation (docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.md). Synthetic data only."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.explore_overnight_drift_v1 import ablation as A
from src.explore_overnight_drift_v1 import engine as E
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore, ImportStats
from src.models import ET, MARKET_US, Candle
from tests.test_explore_orb_vwap_v0 import _write_day
from tests.test_explore_overnight_drift_v1 import _week_data, sd_of, strong_bars
from tests.test_explore_pullback_vwap_v1 import _load_script


def _flat_day(p):
    return [Bar(t, p, p + 0.5, p - 0.5, p, 1000.0) for t in range(570, 960)]


def _prepared(n, bad=(), step=0.1):
    days = trading_days(date(2024, 1, 2), date(2024, 9, 30))[:n]
    assert len(days) == n
    reg = {d.isoformat(): _flat_day(100.0 + step * k) for k, d in enumerate(days)}
    for k in bad:
        reg[days[k].isoformat()] = reg[days[k].isoformat()][:-1]
    return days, prepare_symbol("QQQ", reg, days)


# ---------------------------------------------------------------- EMA series
def test_ema20_series_equals_prepare_symbol_including_gaps():
    days, qqq = _prepared(70, bad=(25,))
    iso = [d.isoformat() for d in days]
    ema = A.daily_ema_series(qqq, iso, 20)
    for d in iso:
        assert ema[d] == qqq[d].daily_ema


def test_ema50_seed_and_restart_after_gap():
    days, qqq = _prepared(120, bad=(60,))
    iso = [d.isoformat() for d in days]
    ema = A.daily_ema_series(qqq, iso, 50)
    closes = [qqq[d].bars[-1].close if qqq[d].complete else None for d in iso]  # day 60 incomplete
    assert ema[iso[49]] is None and ema[iso[50]] == pytest.approx(sum(closes[:50]) / 50)
    want = sum(closes[:50]) / 50 + 2 / 51 * (closes[50] - sum(closes[:50]) / 50)
    assert ema[iso[51]] == pytest.approx(want)
    assert ema[iso[60]] is None and all(ema[iso[k]] is None for k in range(61, 111))
    assert ema[iso[111]] == pytest.approx(sum(closes[61:111]) / 50)


# ---------------------------------------------------------------- entry / exit
def test_trend_entry_only_checks_ema():
    d = [x.isoformat() for x in trading_days(date(2024, 3, 4), date(2024, 3, 8))]
    weak = sd_of(strong_bars(open_=102.0, end=101.0), d[0])  # below open: V1 rejects
    nxt = sd_of(strong_bars(), d[1])
    c = E.bar(weak, E.SIGNAL_MIN).close
    assert E.entry_status(weak, nxt) == "below_open"
    assert A.trend_entry({d[0]: c - 0.01})(weak, nxt) == "enter"
    assert A.trend_entry({d[0]: c})(weak, nxt) == "below_ema"
    assert A.trend_entry({d[0]: None})(weak, nxt) == "daily_not_ready"
    assert A.trend_entry({"2026-01-20": 1.0})(sd_of(strong_bars(), "2026-01-20"),
                                             sd_of(strong_bars(), "2026-01-21")) == "no_next_day"


@pytest.mark.parametrize("gap", [-0.02, 0.0])
def test_open_exit_always_0931(gap):
    d = [x.isoformat() for x in trading_days(date(2024, 3, 4), date(2024, 3, 8))]
    sd = sd_of(strong_bars(), d[0])
    c = sd.bars[-1].close
    nxt = sd_of(strong_bars(open_=c * (1 + gap), end=c), d[1])
    tr, _ = E.overnight_trade(sd, nxt, 2000.0, exit_minute=E.STOP_EXIT_MIN, gap_stop=False)
    assert (tr.exit_reason, tr.legs[0].minute, tr.legs[0].raw) == ("open_exit", 571, E.bar(nxt, 571).open)
    v1, _ = E.overnight_trade(sd, nxt, 2000.0)
    assert v1.exit_reason == ("disaster_stop" if gap <= -0.015 else "overnight_exit")


def test_run_all_v1_and_bench_equal_v1_engine():
    days, qqq = _week_data(5, [strong_bars, lambda: strong_bars(open_=102.0, end=101.0), strong_bars, strong_bars,
                               strong_bars])
    # entry_for("v1" / "none") must be exactly the V1 engine's own entry rule and benchmark
    v1, _ = E.run_backtest(qqq, days)
    alt, _ = E.run_backtest(qqq, days, entry=A.entry_for(A.CONFIGS[0], {}, {}))
    assert len(v1.trades) == 3
    assert [asdict(t) for t in v1.trades] == [asdict(t) for t in alt.trades]
    assert [asdict(d) for d in v1.days] == [asdict(d) for d in alt.days]
    bench, _ = E.run_backtest(qqq, days, filters=False)
    alt, _ = E.run_backtest(qqq, days, entry=A.entry_for(A.CONFIGS[4], {}, {}))
    assert [asdict(t) for t in bench.trades] == [asdict(t) for t in alt.trades]
    assert [asdict(d) for d in bench.days] == [asdict(d) for d in alt.days]


def test_year_mdd():
    from src.explore_orb_vwap_v0.strategy import DayRecord
    days = [DayRecord("2024-12-30", "traded", 100, 110, 0, [], 1), DayRecord("2024-12-31", "traded", 110, 99, 0, [], 1),
            DayRecord("2025-01-02", "traded", 99, 90, 0, [], 1), DayRecord("2025-01-03", "traded", 90, 95, 0, [], 1)]
    m = A.year_mdd(days, 100)
    assert m["2024"]["mdd_usd"] == pytest.approx(11) and m["2024"]["mdd_pct"] == pytest.approx(0.1)
    assert m["2025"]["mdd_usd"] == pytest.approx(9) and m["2025"]["mdd_pct"] == pytest.approx(9 / 99)


def test_paired_exit_diff():
    d = [x.isoformat() for x in trading_days(date(2024, 3, 4), date(2024, 3, 8))]
    sd = sd_of(strong_bars(), d[0])
    nxt = sd_of(strong_bars(open_=101.0, end=103.0), d[1])  # rising morning: 09:35 open > 09:31 open
    a, _ = E.overnight_trade(sd, nxt, 2000.0)
    b, _ = E.overnight_trade(sd, nxt, 2000.0, exit_minute=571, gap_stop=False)
    # a second pair where V2A took its disaster stop: both exit at the same 09:31 bar -> difference 0
    c = sd.bars[-1].close
    gap_day = sd_of(strong_bars(open_=c * 0.98, end=c), d[2])
    sd2 = sd_of(strong_bars(), d[1])
    a2, _ = E.overnight_trade(sd2, gap_day, 2000.0)
    b2, _ = E.overnight_trade(sd2, gap_day, 2000.0, exit_minute=571, gap_stop=False)
    assert a2.exit_reason == "disaster_stop"
    r = A.paired_exit_diff([a, a2], [b, b2])
    x = (b.legs[0].raw - a.legs[0].raw) / a.entry_raw
    assert x < 0 and r["pairs"] == 2 and r["a_disaster_stop_pairs"] == 1
    assert r["all"]["n"] == 2 and r["all"]["mean_pct"] == pytest.approx(x / 2) and r["all"]["ties"] == 1
    assert r["all"]["b_better_share_of_non_ties"] == 0.0
    assert r["a_normal_exit"]["n"] == 1 and r["a_normal_exit"]["mean_pct"] == pytest.approx(x)
    assert r["a_normal_exit"]["t"] is None and r["a_normal_exit"]["ties"] == 0


# ---------------------------------------------------------------- CLI: reproduction gate
def _db(tmp_path):
    db = str(tmp_path / "m.sqlite")
    cal = trading_days(date(2024, 6, 3), date(2024, 7, 12))
    with HistoryStore(db) as store:
        for k, d in enumerate(cal):
            _write_day(store, "QQQ", d, p=100.0 + 0.1 * k)
        store.commit()
    return db, cal


def test_cli_reproduces_v1_then_writes_all(tmp_path):
    v1cli = _load_script("explore_overnight_drift_v1")
    v2cli = _load_script("explore_overnight_drift_v2")
    db, cal = _db(tmp_path)
    v1cli.run(db, tmp_path / "v1", start=cal[0], end=cal[-1])
    v1json = tmp_path / "v1" / v1cli.OUT_NAME
    out = tmp_path / "v2"
    md = v2cli.run(db, out, v1json, start=cal[0], end=cal[-1])
    names = {p.name for p in out.iterdir()}
    assert "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.json" in names
    assert {f"EXPLORE_OVERNIGHT_DRIFT_2000USD_V2_{c.name}.json" for c in A.CONFIGS} <= names
    doc = json.loads((out / "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.json").read_text())
    assert set(doc["configs"]) == {c.name for c in A.CONFIGS} and "q2" in doc and "Q1" in md
    # EMA20 needs 20 sessions -> scored from the 21st; EMA50 never ready in 28 sessions
    assert doc["provenance"]["scored_start"] == cal[20].isoformat()
    assert doc["configs"]["V2C_EMA50_OPEN_EXIT"]["metrics"]["trade_count"] == 0
    assert doc["ema50_not_ready_days"] > 0
    # flat intraday prices: 15:50 close == 09:30 open, so V1 never enters while V2A (close > EMA20) does
    assert doc["configs"]["V1_BASE"]["metrics"]["trade_count"] == 0
    assert doc["configs"]["V2A_EMA20_ONLY"]["metrics"]["trade_count"] > 0
    with pytest.raises(SystemExit):
        v2cli.run(db, out, v1json, start=cal[0], end=cal[-1])


def _write_rising_day(store, d, p0):
    """Regular-session 1m candles drifting up 0.4 through the day (bar END labels 09:31 .. 16:00, ET)."""
    candles = []
    for i, m in enumerate(range(571, 961)):
        o = p0 + 0.4 * i / 390
        c = p0 + 0.4 * (i + 1) / 390
        moment = datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)
        candles.append(Candle.from_moment(symbol="QQQ", interval="1m", moment=moment, o=o, h=c, low=o, c=c,
                                          v=1000.0, currency="USD", adjusted=True, market=MARKET_US))
    store.upsert_candles(candles, source="toss_backfill", run_id="r", stats=ImportStats(source="toss_backfill"))


def test_cli_gate_with_v1_trades(tmp_path):
    v1cli = _load_script("explore_overnight_drift_v1")
    v2cli = _load_script("explore_overnight_drift_v2")
    db = str(tmp_path / "m.sqlite")
    cal = trading_days(date(2024, 6, 3), date(2024, 7, 12))
    with HistoryStore(db) as store:
        for k, d in enumerate(cal):
            _write_rising_day(store, d, 100.0 + 0.5 * k)
        store.commit()
    v1cli.run(db, tmp_path / "v1", start=cal[0], end=cal[-1])
    v1json = tmp_path / "v1" / v1cli.OUT_NAME
    assert json.loads(v1json.read_text())["metrics"]["trade_count"] > 0
    out = tmp_path / "v2"
    v2cli.run(db, out, v1json, start=cal[0], end=cal[-1])
    doc = json.loads((out / "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.json").read_text())
    v1n = doc["configs"]["V1_BASE"]["metrics"]["trade_count"]
    assert v1n > 0 and doc["configs"]["V2A_EMA20_ONLY"]["metrics"]["trade_count"] >= v1n
    per = json.loads((out / "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2_V1_BASE.json").read_text())
    assert per["trades"] == json.loads(v1json.read_text())["trades"]
    assert doc["q2"]["pairs"] == doc["configs"]["V2A_EMA20_ONLY"]["metrics"]["trade_count"]


def test_cli_refuses_when_v1_not_reproduced(tmp_path):
    v1cli = _load_script("explore_overnight_drift_v1")
    v2cli = _load_script("explore_overnight_drift_v2")
    db, cal = _db(tmp_path)
    v1cli.run(db, tmp_path / "v1", start=cal[0], end=cal[-1])
    v1json = tmp_path / "v1" / v1cli.OUT_NAME
    doc = json.loads(v1json.read_text())
    doc["metrics"]["trade_count"] += 1
    v1json.write_text(json.dumps(doc))
    out = tmp_path / "v2"
    with pytest.raises(SystemExit) as e:
        v2cli.run(db, out, v1json, start=cal[0], end=cal[-1])
    assert e.value.code == 2 and not out.exists()


def test_preflight_counts_match_engine(tmp_path):
    pre = _load_script("explore_overnight_v2_preflight")
    days, qqq = _prepared(40, bad=(30,), step=0.1)
    iso = [d.isoformat() for d in days]
    r = pre.preflight(qqq, iso)
    out = A.run_all(qqq, days)["results"]
    for c in A.CONFIGS:
        res, _ = out[c.name]
        eng = {**res.skips}
        if res.trades:
            eng["enter"] = len(res.trades)
        assert r["counts"][c.name] == dict(sorted(eng.items())), c.name
