"""V4_RVOL_SNIPER preset of the exploratory pullback-VWAP engine
(docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md): seven stocks, the single highest-RVOL qualifying
stock per day, minimum range checked after the pick, signals 10:00 ~ 14:29.

Synthetic data only: no test reads data/*.sqlite.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import date, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0 import data_loader as DL
from src.explore_orb_vwap_v0.config import (
    ALL_PRESETS,
    CLI_PRESETS,
    V2_QQQ,
    V3_QQQ,
    V4_RVOL_SNIPER,
    EngineConfigV4,
)
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.strategy import run_backtest
from src.explore_orb_vwap_v0.universe import (
    SniperFilter,
    UniverseFilter,
    prepare_symbol,
)
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import (
    DAY,
    _warm_day,
    _write_day,
    fill_from,
    flat,
    setb,
)
from tests.test_explore_pullback_vwap_v1 import _load_script, loud, v1_sd
from tests.test_explore_pullback_vwap_v2 import qqq_sd
from tests.test_explore_pullback_vwap_v3 import BASE_FIELDS

V4 = V4_RVOL_SNIPER


def stock(symbol, rvol=2.0, vol590=1200.0):
    """qqq_sd (gap +1%, prior close 100 > daily EMA20 99, ORB 99..101, support at 10:00) with the given RVOL."""
    sd = qqq_sd(vol590, symbol=symbol)
    sd.rvol_base = sd.orb_volume / rvol
    return sd


def one_day(cfg, sds):
    return run_backtest({sd.symbol: {DAY: sd} for sd in sds}, [date.fromisoformat(DAY)], config=cfg)


# ---------------------------------------------------------------- preset
def test_v4_preset_values_and_other_presets_unchanged():
    assert isinstance(V4, EngineConfigV4)
    assert [f.name for f in fields(V4)] == BASE_FIELDS + ["rvol_min", "gap_band", "gap_up_min", "signal_last",
                                                         "last_fill", "split_gap"]
    assert V4.universe == ("NVDA", "TSLA", "AAPL", "AMD", "MSFT", "AMZN", "META")
    assert (V4.rvol_min, V4.gap_band, V4.gap_up_min, V4.signal_last, V4.last_fill, V4.split_gap) == (
        1.5, None, 0.008, 869, 870, 0.30)
    assert all(getattr(V4, k) == getattr(V2_QQQ, k) for k in BASE_FIELDS[2:])
    assert CLI_PRESETS["V4_RVOL_SNIPER"] is V4 and set(CLI_PRESETS) == set(ALL_PRESETS) | {"V4_RVOL_SNIPER"}
    assert "V4_RVOL_SNIPER" not in ALL_PRESETS
    for cfg in ALL_PRESETS.values():  # window stays a class attribute, not a recorded field
        assert (cfg.signal_last, cfg.last_fill) == (899, 900)
        assert "signal_last" not in {f.name for f in fields(cfg)}


# ---------------------------------------------------------------- screener
@pytest.mark.parametrize("direction, ok", [(1, True), (0, False), (-1, False)])
def test_sniper_needs_prior_close_above_daily_ema(direction, ok):
    sd = stock("NVDA")
    sd.daily_ema = {1: 99.0, 0: 100.0, -1: 101.0}[direction]
    assert [c.symbol for c in SniperFilter(1.5, 0.008).watchlist({"NVDA": sd})] == (["NVDA"] if ok else [])


@pytest.mark.parametrize("prior_close, ok", [(100.0, True), (101.0 / 1.008, True), (101.0 / 1.0079, False),
                                             (102.0, False), (90.0, True)])
def test_sniper_gap_is_signed_with_no_cap(prior_close, ok):
    sd = stock("NVDA")  # open 101
    sd.prior_close, sd.daily_ema = prior_close, prior_close - 1.0
    assert bool(SniperFilter(1.5, 0.008).watchlist({"NVDA": sd})) is ok


@pytest.mark.parametrize("rvol, ok", [(1.5, True), (1.4999, False), (9.0, True)])
def test_sniper_rvol_threshold(rvol, ok):
    assert bool(SniperFilter(1.5, 0.008).watchlist({"NVDA": stock("NVDA", rvol)})) is ok


def test_sniper_needs_daily_values_and_rvol_base():
    sd = stock("NVDA")
    sd.daily_atr = None
    assert SniperFilter(1.5, 0.008).watchlist({"NVDA": sd}) == []
    sd = stock("NVDA")
    sd.rvol_base = None
    assert SniperFilter(1.5, 0.008).watchlist({"NVDA": sd}) == []


def test_sniper_keeps_only_highest_rvol_ties_by_symbol():
    rows = {"AMD": stock("AMD", 2.0), "NVDA": stock("NVDA", 3.0), "TSLA": stock("TSLA", 3.0)}
    f = SniperFilter(1.5, 0.008)
    assert [c.symbol for c in f.passing(rows)] == ["NVDA", "TSLA", "AMD"]
    assert [c.symbol for c in f.watchlist(rows)] == ["NVDA"]


# ---------------------------------------------------------------- day loop
def test_v4_trades_only_the_pick_and_records_it():
    res = one_day(V4, [stock("AMD", 4.0), stock("NVDA", 2.0)])
    assert [(d.status, d.watchlist) for d in res.days] == [("traded", ["AMD"])]
    assert {t.symbol for t in res.trades} == {"AMD"}
    pk = res.sniper["picks"]
    assert len(pk) == 1 and pk[0]["symbol"] == "AMD" and pk[0]["passing"] == ["AMD", "NVDA"]
    assert pk[0]["rvol"] == pytest.approx(4.0) and pk[0]["gap"] == pytest.approx(0.01) and pk[0]["min_range_ok"]
    assert res.sniper["per_symbol"]["AMD"] == {"picked": 1, "min_range_after_pick": 0, "passing": 1}
    assert res.sniper["per_symbol"]["NVDA"] == {"picked": 0, "min_range_after_pick": 0, "passing": 1}
    assert res.filter_symbol_days is None


def test_min_range_checked_after_pick_without_fallback():
    top = stock("AMD", 4.0)
    top.daily_atr = (top.orb_high - top.orb_low) / 0.15 + 1.0  # range < 0.15 x ATR
    res = one_day(V4, [top, stock("NVDA", 2.0)])  # NVDA alone would trade
    assert [(d.status, d.watchlist, d.trades) for d in res.days] == [("min_range_after_pick", ["AMD"], 0)]
    assert res.trades == [] and res.sniper["per_symbol"]["AMD"]["min_range_after_pick"] == 1
    assert [d.status for d in one_day(V4, [stock("NVDA", 2.0)]).days] == ["traded"]
    m = BacktestReporter(res).metrics()
    assert m["day_status"] == {"min_range_after_pick": 1} and m["sniper"]["pick_days"] == 1


def test_no_qualifying_stock_is_no_candidates():
    res = one_day(V4, [stock("AMD", 1.2), stock("NVDA", 1.0)])
    assert [d.status for d in res.days] == ["no_candidates"] and res.sniper["picks"] == []


def _support_at(t):
    """Breakout 09:50 (volume 1200), flat 101.5, a single support candle starting at minute t."""
    b = flat()
    setb(b, 575, 100, 101, 100, 100)
    setb(b, 580, 100, 100, 99, 100)
    setb(b, 590, 100.5, 101.6, 100.4, 101.5)
    fill_from(b, 591, 101.5)
    setb(b, t, 101.4, 101.7, 101.0, 101.6)
    b = loud(b, [590], 1200.0)
    b[0] = Bar(570, 101.0, 101.0, 100.0, 100.0, 1000.0)
    return b


@pytest.mark.parametrize("t, v4_trades, v3_trades", [(869, 1, 1), (870, 0, 1), (899, 0, 1)])
def test_v4_signal_window_ends_1429(t, v4_trades, v3_trades):
    def sd(sym):
        s = v1_sd(_support_at(t), direction=1, symbol=sym)
        s.rvol_base = s.orb_volume / 2.0
        return s
    r4 = one_day(V4, [sd("NVDA")])
    assert len(r4.trades) == v4_trades
    if v4_trades:
        assert (r4.trades[0].signal_minute, r4.trades[0].entry_minute) == (869, 870)
    assert len(one_day(V3_QQQ, [sd("QQQ")]).trades) == v3_trades


def test_v4_reporter_and_v3_reporter_keys():
    m4 = BacktestReporter(one_day(V4, [stock("AMD", 4.0)])).metrics()
    assert m4["sniper"]["per_symbol"]["AMD"]["picked"] == 1 and m4["sniper"]["split_breaks"] == {}
    assert "filter_counts" not in m4
    m3 = BacktestReporter(one_day(V3_QQQ, [stock("QQQ")])).metrics()
    assert "sniper" not in m3


def test_v4_rejects_custom_universe_filter():
    with pytest.raises(ValueError):
        run_backtest({"AMD": {DAY: stock("AMD")}}, [date.fromisoformat(DAY)], config=V4,
                     universe_filter=UniverseFilter())


# ---------------------------------------------------------------- split break
def _split_days(n=45, k=30, ratio=10.0):
    days = trading_days(date(2024, 1, 2), date(2024, 4, 30))[:n]
    reg = {d.isoformat(): _warm_day(p=100.0 if i < k else 100.0 / ratio) for i, d in enumerate(days)}
    return days, reg


def test_split_gap_breaks_history_only_when_enabled():
    days, reg = _split_days()
    iso = [d.isoformat() for d in days]
    on = prepare_symbol("NVDA", reg, days, split_gap=0.30)
    off = prepare_symbol("NVDA", reg, days)
    assert [d for d in iso if on[d].split_break] == [iso[30]] and not any(off[d].split_break for d in iso)
    assert off[iso[30]].ready and on[iso[30]].prior_close is None and not on[iso[30]].is_ready()
    # RVOL history restarts on the split day: fewer than 20 sessions since it through day 44
    assert len(iso) == 45 and all(on[iso[j]].rvol_base is None for j in range(30, 45))
    assert off[iso[35]].rvol_base is not None
    assert on[iso[31]].prior_close == pytest.approx(10.0) and on[iso[31]].daily_ema is None


def test_small_gap_is_not_a_split():
    days, reg = _split_days(ratio=100.0 / 71.0)  # 100 -> 71: gap -29% < 30%
    on = prepare_symbol("NVDA", reg, days, split_gap=0.30)
    assert not any(sd.split_break for sd in on.values())


# ---------------------------------------------------------------- CLI
def test_cli_v4_loads_seven_stocks_plus_qqq_benchmark(tmp_path, monkeypatch):
    cli = _load_script("explore_pullback_vwap_v0")
    seen = []
    real = DL.research_load

    def spy_load(db, symbol, *a, **k):
        seen.append(symbol)
        return real(db, symbol, *a, **k)

    monkeypatch.setattr(DL, "research_load", spy_load)
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for s in V4.universe + ("QQQ",):
            for k in range(2):
                _write_day(store, s, d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    cli.run(db, out, start=d1, end=d1 + timedelta(days=1), config=CLI_PRESETS["V4_RVOL_SNIPER"])
    assert set(seen) == set(V4.universe) | {"QQQ"}
    doc = json.loads((out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.json").read_text())
    prov = doc["provenance"]
    assert prov["config_name"] == "V4_RVOL_SNIPER" and prov["symbols"] == list(V4.universe)
    assert prov["benchmark_only"] == ["QQQ"] and set(prov["rows_read"]) == set(V4.universe) | {"QQQ"}
    assert prov["config"]["gap_up_min"] == 0.008 and prov["config"]["split_gap"] == 0.30
    spec = cli.ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md"
    assert prov["spec_path"] == "docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md"
    assert prov["spec_sha256"] == hashlib.sha256(spec.read_bytes()).hexdigest()
    assert "spec V4" in prov["scored_start_rule"]
    assert doc["sniper_picks"] == []
