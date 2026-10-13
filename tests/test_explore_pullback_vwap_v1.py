"""V1 additions to the exploratory pullback-VWAP engine (docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md).

Synthetic data only: no test reads data/*.sqlite.
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0.config import CONFIGS, V0, V1, V1_LONGONLY
from src.explore_orb_vwap_v0.data_loader import UNIVERSE
from src.explore_orb_vwap_v0.indicators import daily_bar
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.risk import RiskManager
from src.explore_orb_vwap_v0.strategy import (
    StrategyEngine,
    daily_exclusion,
    run_backtest,
)
from src.explore_orb_vwap_v0.universe import SymbolDay, prepare_symbol
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import (
    DAY,
    _write_day,
    flat,
    long_bars,
    make_sd,
    mirror,
    setb,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def v1_sd(bars, direction=1, daily_atr=1.0, **kw):
    """make_sd plus prior-day daily EMA20/ATR14 giving the requested direction (prior close is 100)."""
    sd = make_sd(bars, **kw)
    sd.daily_ema = {1: 99.0, -1: 101.0, 0: 100.0}[direction]
    sd.daily_atr = daily_atr
    return sd


def run_cfg(cfg, sds, equity=2000.0):
    return StrategyEngine(config=cfg).simulate_day(DAY, sds, equity)


def loud(bars, ts, v=1300.0):
    """Give the bars starting at minutes `ts` volume v (ORB volume is 15 x 1000 -> per-minute mean 1000)."""
    for t in ts:
        b = bars[t - 570]
        bars[t - 570] = Bar(t, b.open, b.high, b.low, b.close, v)
    return bars


# ---------------------------------------------------------------- presets
def test_presets_v0_v1_v1_longonly():
    assert V0.universe == UNIVERSE and "IWM" in V0.universe
    assert (V0.max_price, V0.fractional, V0.direction_filter, V0.breakout_volume_mult, V0.min_range_atr_mult,
            V0.allow_short) == (250.0, False, False, None, None, True)
    assert not V0.has_v1_filters and not V0.daily_filters
    assert V1.universe == ("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR")
    assert (V1.max_price, V1.fractional, V1.direction_filter, V1.breakout_volume_mult, V1.min_range_atr_mult,
            V1.allow_short) == (None, True, True, 1.3, 0.15, True)
    assert V1_LONGONLY == replace(V1, name="V1_LONGONLY", allow_short=False)
    assert set(CONFIGS) == {"V0", "V1", "V1_LONGONLY"}
    assert StrategyEngine().config is V0 and RiskManager().config is V0


# ---------------------------------------------------------------- daily bars / EMA20 / ATR14
def test_daily_bar_from_complete_1m_session():
    bars = [Bar(570, 10.0, 11.0, 9.5, 10.5, 1), Bar(571, 10.5, 12.0, 10.0, 11.0, 1), Bar(572, 11, 11.5, 8.0, 9.0, 1)]
    assert daily_bar(bars) == (10.0, 12.0, 8.0, 9.0)


def _day_bars(p, rng=0.5):
    return [Bar(t, p, p + rng, p - rng, p, 1000.0) for t in range(570, 960)]


def _closes(n):
    return [100.0 + k + (k % 3) * 0.7 for k in range(n)]


def test_daily_ema20_atr14_seeds_use_prior_days_only():
    days = trading_days(date(2024, 1, 2), date(2024, 3, 28))[:25]
    closes = _closes(25)
    prep = prepare_symbol("AAA", {d.isoformat(): _day_bars(c) for d, c in zip(days, closes)}, days)
    tr = [1.0] + [max(1.0, abs(closes[k] + 0.5 - closes[k - 1]), abs(closes[k] - 0.5 - closes[k - 1]))
                  for k in range(1, 25)]
    atr = sum(tr[:14]) / 14
    for k in range(14, 25):  # day k sees ATR through day k-1
        assert prep[days[k].isoformat()].daily_atr == pytest.approx(atr)
        atr = (atr * 13 + tr[k]) / 14
    for k in range(20):
        assert prep[days[k].isoformat()].daily_ema is None
        assert not prep[days[k].isoformat()].daily_ready
    ema = sum(closes[:20]) / 20
    for k in range(20, 25):
        sd = prep[days[k].isoformat()]
        assert sd.daily_ema == pytest.approx(ema) and sd.daily_ready
        assert sd.prior_close == closes[k - 1]
        ema += 2 / 21 * (closes[k] - ema)


def test_daily_values_have_no_same_day_leakage():
    days = trading_days(date(2024, 1, 2), date(2024, 3, 28))[:23]
    reg = {d.isoformat(): _day_bars(c) for d, c in zip(days, _closes(23))}
    base = prepare_symbol("AAA", reg, days)
    reg2 = dict(reg)
    reg2[days[21].isoformat()] = _day_bars(500.0, rng=50.0)
    changed = prepare_symbol("AAA", reg2, days)
    d21, d22 = days[21].isoformat(), days[22].isoformat()
    assert changed[d21].daily_ema == base[d21].daily_ema and changed[d21].daily_atr == base[d21].daily_atr
    assert changed[d21].direction == base[d21].direction
    assert changed[d22].daily_ema != base[d22].daily_ema and changed[d22].daily_atr != base[d22].daily_atr


def test_daily_state_resets_on_incomplete_day():
    days = trading_days(date(2024, 1, 2), date(2024, 4, 30))[:45]
    reg = {d.isoformat(): _day_bars(c) for d, c in zip(days, _closes(45))}
    bad = days[22].isoformat()
    reg[bad] = reg[bad][:-1]
    prep = prepare_symbol("AAA", reg, days)
    assert prep[days[21].isoformat()].daily_ready
    assert not prep[bad].complete and not prep[bad].daily_ready
    # after the gap: 20 new complete days are needed (days 23..42) before day 43 is ready again
    for k in range(23, 43):
        sd = prep[days[k].isoformat()]
        assert sd.daily_ema is None and not sd.daily_ready and sd.direction is None
    assert prep[days[23].isoformat()].daily_atr is None  # prior day incomplete
    assert prep[days[37].isoformat()].daily_atr is not None  # 14 TRs from days 23..36
    sd = prep[days[43].isoformat()]
    assert sd.daily_ready and sd.daily_ema == pytest.approx(sum(_closes(45)[23:43]) / 20)


# ---------------------------------------------------------------- direction filter
def test_direction_property_compares_prior_close_with_prior_ema():
    sd = SymbolDay("AAA", DAY, True, True, prior_close=100.0)
    assert sd.direction is None
    sd.daily_atr = 1.0
    for ema, want in ((99.0, 1), (101.0, -1), (100.0, 0)):
        sd.daily_ema = ema
        assert sd.direction == want


def test_direction_filter_long_only_short_only():
    cfg = replace(V0, direction_filter=True)
    trades, skips = run_cfg(cfg, [v1_sd(long_bars(), direction=1)])
    assert [t.side for t in trades] == [1] and skips == {}
    trades, skips = run_cfg(cfg, [v1_sd(long_bars(), direction=-1)])
    assert trades == [] and skips["direction_filter"] >= 1
    trades, _ = run_cfg(cfg, [v1_sd(mirror(long_bars()), direction=-1, ema=200.0)])
    assert [t.side for t in trades] == [-1]
    trades, skips = run_cfg(cfg, [v1_sd(mirror(long_bars()), direction=1, ema=200.0)])
    assert trades == [] and skips["direction_filter"] >= 1


def test_direction_equal_and_not_ready_excluded_before_watchlist():
    cfg = replace(V0, direction_filter=True)
    assert daily_exclusion(v1_sd(long_bars(), direction=1), cfg) is None
    assert daily_exclusion(v1_sd(long_bars(), direction=0), cfg) == "direction_equal"
    sd = v1_sd(long_bars())
    sd.daily_ema = None
    assert daily_exclusion(sd, cfg) == "daily_not_ready"
    assert daily_exclusion(sd, V0) is None  # V0 never looks at daily values


def test_allow_short_false_drops_shorts():
    cfg = replace(V0, allow_short=False)
    trades, skips = run_cfg(cfg, [make_sd(mirror(long_bars()), ema=200.0)])
    assert trades == [] and skips["short_disabled"] >= 1
    assert [t.side for t in run_cfg(cfg, [make_sd(long_bars())])[0]] == [1]
    trades, skips = run_cfg(V1_LONGONLY, [v1_sd(loud(mirror(long_bars()), [590]), direction=-1, ema=200.0)])
    assert trades == [] and skips["short_disabled"] >= 1


# ---------------------------------------------------------------- breakout volume
def test_breakout_needs_volume_and_later_qualifying_bar_arms():
    cfg = replace(V0, breakout_volume_mult=1.3)
    # long_bars: all volumes 1000 -> every beyond-ORB close is rejected, nothing arms, no trade
    trades, skips = run_cfg(cfg, [make_sd(long_bars())])
    assert trades == [] and skips["breakout_volume"] == 950 - 590 + 1  # closes > 101 from 09:50 through the 15:50 bar
    # a 1299-volume bar is still short of 1.3 x 1000
    trades, _ = run_cfg(cfg, [make_sd(loud(long_bars(), [590], 1299.0))])
    assert trades == []
    # the first qualifying bar (exactly 1.3x) at 09:55 arms the breakout; earlier beyond closes were rejected
    trades, skips = run_cfg(cfg, [make_sd(loud(long_bars(), [595], 1300.0))])
    assert [(t.side, t.signal_minute) for t in trades] == [(1, 600)]
    assert skips["breakout_volume"] == 595 - 590
    # a qualifying bar that comes only after the support candle arms too late for that candle
    assert run_cfg(cfg, [make_sd(loud(long_bars(), [601], 1300.0))])[0] == []
    # V0 ignores volume
    assert len(run_cfg(V0, [make_sd(long_bars())])[0]) == 1


def test_short_breakout_volume_mirror():
    cfg = replace(V0, breakout_volume_mult=1.3)
    assert run_cfg(cfg, [make_sd(mirror(long_bars()), ema=200.0)])[0] == []
    trades, _ = run_cfg(cfg, [make_sd(loud(mirror(long_bars()), [590]), ema=200.0)])
    assert [t.side for t in trades] == [-1]


# ---------------------------------------------------------------- min range
def test_min_range_threshold():
    cfg = replace(V0, min_range_atr_mult=0.15)
    sd = v1_sd(long_bars())  # ORB 99..101 -> range 2.0
    sd.daily_atr = 2.0 / 0.15 + 0.01
    assert daily_exclusion(sd, cfg) == "min_range"
    sd.daily_atr = 2.0 / 0.15 - 0.01
    assert daily_exclusion(sd, cfg) is None


def _cand_bars(open_, c0944, vol=2000.0):
    b = flat(960, 100.0)
    b[0] = Bar(570, open_, max(open_, 100), min(open_, 100), 100, vol / 15)
    for t in range(571, 585):
        b[t - 570] = Bar(t, 100, 100, 100, 100, vol / 15)
    b[14] = Bar(584, 100, max(100, c0944), min(100, c0944), c0944, vol / 15)
    return b


def _cand(sym, open_, c0944, atr=0.1):
    sd = v1_sd(_cand_bars(open_, c0944), symbol=sym, daily_atr=atr)
    sd.rvol_base = 1000.0
    return sd


def test_min_range_and_not_ready_exclusions_happen_before_watchlist_selection():
    spy = _cand("SPY", 100.5, 100.2)
    rows = {
        "SPY": spy,
        "AAA": _cand("AAA", 99.0, 99.0, atr=100.0),  # |rs| 1.2%, ORB range 1 < 15 -> min_range
        "BBB": _cand("BBB", 101.0, 101.5),  # |rs| 1.3%, daily not ready
        "CCC": _cand("CCC", 101.0, 101.0),  # |rs| 0.8%
        "DDD": _cand("DDD", 100.6, 100.6),  # |rs| 0.4%
    }
    rows["BBB"].daily_atr = None
    prepared = {s: {DAY: sd} for s, sd in rows.items()}
    day = [date.fromisoformat(DAY)]
    cfg = replace(V0, min_range_atr_mult=0.15)
    res0 = run_backtest(prepared, day)
    assert res0.days[0].watchlist == ["BBB", "AAA"] and res0.filter_symbol_days is None
    res = run_backtest(prepared, day, config=cfg)
    assert res.days[0].watchlist == ["CCC", "DDD"]
    assert res.filter_symbol_days["min_range"]["AAA"] == 1
    assert res.filter_symbol_days["daily_not_ready"]["BBB"] == 1
    fc = BacktestReporter(res).metrics()["filter_counts"]
    assert fc["min_range_symbol_days"]["total"] == 1 and fc["daily_not_ready_symbol_days"]["total"] == 1
    assert "filter_counts" not in BacktestReporter(res0).metrics()
    with pytest.raises(ValueError):
        run_backtest(prepared, day, engine=StrategyEngine(), config=cfg)


# ---------------------------------------------------------------- fractional sizing
def test_fractional_sizing_round_cash_cap_and_minimum():
    assert RiskManager.fractional_shares(2000, 2000, 50.0, 0.7) == 28.5714  # round(28.571428.., 4)
    n = RiskManager.fractional_shares(2000, 2000, 101.52, 0.75)  # 26.6667 x 101.52 > 2000 -> cash cap
    assert n == 19.7005 and n * 101.52 <= 2000  # floor(19.700551.., 4 dp)
    assert RiskManager.fractional_shares(2000, 2000, 100.0, 1e6) == 0.0  # round(0.00002, 4) = 0
    assert RiskManager.fractional_shares(2000, 0.005, 100.0, 0.5) == 0.0  # cash cap floors to 0
    assert RiskManager.fractional_shares(2000, 0.015, 100.0, 0.5) == 0.0001
    rm = RiskManager(config=replace(V0, fractional=True))
    plan, why = rm.plan(1, 100.0, swing=99.0, atr=2.0, day_start_equity=2000, cash=2000)
    assert why == "" and plan.shares == pytest.approx(round(20 / 1.02, 4), abs=1e-12)
    plan, why = rm.plan(1, 100.0, swing=0.0, atr=1e6, day_start_equity=0.001, cash=2000)
    assert plan is None and why == "zero_shares"


def test_tp1_fractional_half_and_all_when_tiny():
    rm = RiskManager(config=replace(V0, fractional=True))
    assert rm.tp1_quantity(13.1579) == round(13.1579 / 2, 4)
    assert rm.tp1_quantity(1.2346) == 0.6173
    assert rm.tp1_quantity(0.0001) == 0.0001  # round(0.00005, 4) -> 0.0001: the whole position
    assert RiskManager().tp1_quantity(13) == 6 and RiskManager().tp1_quantity(1) == 1


def test_fractional_trade_tp1_split_and_costs_scale_with_shares():
    cfg = replace(V0, fractional=True)
    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)  # TP1 then breakeven next bar
    tr = run_cfg(cfg, [make_sd(b)])[0][0]
    n = round(20 / 1.52, 4)
    assert tr.shares == n == 13.1579
    q = round(n / 2, 4)
    assert [(x.reason, x.shares) for x in tr.legs] == [("tp1", q), ("breakeven", round(n - q, 4))]
    assert tr.commission == pytest.approx(0.005 * 2 * n)
    exit_value = q * (101.52 + 1.5 * 1.52 - 0.02) + round(n - q, 4) * (101.5 - 0.02)
    assert tr.net_pnl == pytest.approx(exit_value - n * 101.52 - 0.01 * n)
    flat_tr = run_cfg(cfg, [make_sd(long_bars())])[0][0]
    assert flat_tr.exit_reason == "flatten"
    assert flat_tr.net_pnl == pytest.approx(n * (101.48 - 101.52) - 0.005 * 2 * n)
    assert flat_tr.ref_pnl == pytest.approx(-0.001 * (n * 101.5 * 2))  # reference cost: same definition


def test_v1_preset_has_no_250_skip_and_trades_fractional_shares():
    b = [Bar(x.t, x.open * 3, x.high * 3, x.low * 3, x.close * 3, x.volume) for x in long_bars()]
    assert run_cfg(V0, [make_sd(b, atr=6.0)]) == ([], {"price_above_250": 1})
    b = loud(b, [590])
    sd = v1_sd(b, atr=6.0)
    sd.prior_close, sd.daily_ema = 300.0, 290.0
    trades, skips = run_cfg(V1, [sd])
    assert len(trades) == 1 and "price_above_250" not in skips
    tr = trades[0]
    assert tr.entry_raw == 304.5 and tr.shares == round(min(20 / (304.52 - 300.0), 1e9), 4) == 4.4248


# ---------------------------------------------------------------- V0 preset unchanged
def test_v0_preset_identical_to_default_engine():
    def legs(trs):
        return [(t.symbol, t.side, t.signal_minute, t.shares, t.net_pnl, t.ref_pnl,
                 [(x.minute, x.shares, x.fill, x.reason) for x in t.legs]) for t in trs]

    b = long_bars()
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)
    for bars in (long_bars(), b, mirror(b)):
        default = StrategyEngine().simulate_day(DAY, [make_sd(bars, ema=100.0)], 2000.0)
        v0 = run_cfg(V0, [make_sd(bars, ema=100.0)])
        assert legs(default[0]) == legs(v0[0]) and default[1] == v0[1]
        assert all(isinstance(t.shares, int) for t in v0[0])
        assert all(isinstance(x.shares, int) for t in v0[0] for x in t.legs)


# ---------------------------------------------------------------- CLI / comparison
def test_cli_config_out_name_and_provenance(tmp_path):
    cli = _load_script("explore_pullback_vwap_v0")
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(2):
            _write_day(store, "SPY", d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    cli.run(db, out, symbols=("SPY",), start=d1, end=d1 + timedelta(days=1), config=V1)
    doc = json.loads((out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.json").read_text())
    prov = doc["provenance"]
    assert prov["config_name"] == "V1" and prov["config"]["breakout_volume_mult"] == 1.3
    assert prov["spec_path"].endswith("EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md") and len(prov["spec_sha256"]) == 64
    cli.run(db, out, symbols=("SPY",), start=d1, end=d1 + timedelta(days=1), config=V1_LONGONLY)
    assert (out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1_LONGONLY.json").exists()
    cli.run(db, out, symbols=("SPY",), start=d1, end=d1 + timedelta(days=1), out_name="v0_repro.json")
    doc0 = json.loads((out / "v0_repro.json").read_text())
    assert doc0["provenance"]["config_name"] == "V0"
    assert doc0["provenance"]["spec_path"].endswith("EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md")
    assert cli.DEFAULT_OUT_NAME["V0"] == "result.json"


def test_compare_script_prints_tables(tmp_path):
    cmp_mod = _load_script("compare_pullback_vwap_versions")
    b = long_bars()
    b[0] = Bar(570, 101.0, 101.0, 100.0, 100.0, 1000.0)  # gap +1% so AAA makes the watchlist
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)
    paths = []
    for cfg in (V0, replace(V0, name="V1", fractional=True)):
        res = run_backtest({"AAA": {DAY: make_sd(b, symbol="AAA")}, "SPY": {DAY: make_sd(flat(), symbol="SPY")}},
                           [date.fromisoformat(DAY)], config=cfg)
        p = tmp_path / f"{cfg.name}.json"
        BacktestReporter(res).to_json(p, {"config_name": cfg.name})
        paths.append(p)
    md = cmp_mod.compare(paths)
    assert "| V0 | V0.json |" in md and "| V1 | V1.json |" in md
    assert "### V1 (V1.json)" in md and "#### Long / short" in md and "| AAA |" in md
    with pytest.raises(SystemExit):
        cmp_mod.main([str(paths[0])])
