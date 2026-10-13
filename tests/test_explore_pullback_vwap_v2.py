"""V2_QQQ preset of the exploratory pullback-VWAP engine (docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2.md).

Synthetic data only: no test reads data/*.sqlite.
"""
from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import date, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_orb_vwap_v0 import data_loader as DL
from src.explore_orb_vwap_v0.config import CONFIGS, PRESETS, V0, V1, V1_LONGONLY, V2_QQQ
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.strategy import first_scored_day, run_backtest
from src.explore_orb_vwap_v0.universe import SymbolDay, UniverseFilter, prepare_symbol
from src.history.exchange_calendar import trading_days
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import (
    DAY,
    _warm_day,
    _write_day,
    long_bars,
    mirror,
    setb,
)
from tests.test_explore_pullback_vwap_v1 import _load_script, loud, run_cfg, v1_sd


def qqq_sd(vol590=1200.0, symbol="QQQ"):
    """Gap +1% long setup, direction long, breakout bar 09:50 with `vol590` (per-minute ORB mean 1000)."""
    b = loud(long_bars(), [590], vol590)
    b[0] = Bar(570, 101.0, 101.0, 100.0, 100.0, 1000.0)
    return v1_sd(b, direction=1, symbol=symbol)


# ---------------------------------------------------------------- presets
def test_v2_qqq_preset_values():
    assert V2_QQQ == replace(V1_LONGONLY, name="V2_QQQ", universe=("QQQ",), breakout_volume_mult=1.2,
                             rs_reference=None)
    assert (V2_QQQ.max_price, V2_QQQ.fractional, V2_QQQ.direction_filter, V2_QQQ.min_range_atr_mult,
            V2_QQQ.allow_short) == (None, True, True, 0.15, False)
    assert V2_QQQ.daily_filters and V2_QQQ.has_v1_filters
    assert PRESETS["V2_QQQ"] is V2_QQQ and set(PRESETS) == {"V0", "V1", "V1_LONGONLY", "V2_QQQ"}


def test_v0_v1_v1_longonly_presets_unchanged():
    want = {
        "V0": ("V0", ("QQQ", "SPY", "IWM", "AMD", "AAPL", "AMZN", "PLTR"), 250.0, False, False, None, None, True,
               "SPY"),
        "V1": ("V1", ("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"), None, True, True, 1.3, 0.15, True, "SPY"),
        "V1_LONGONLY": ("V1_LONGONLY", ("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"), None, True, True, 1.3, 0.15,
                        False, "SPY"),
    }
    for cfg in (V0, V1, V1_LONGONLY):
        assert tuple(getattr(cfg, f.name) for f in fields(cfg)) == want[cfg.name]
    assert set(CONFIGS) == {"V0", "V1", "V1_LONGONLY"}
    assert all(PRESETS[k] is CONFIGS[k] for k in CONFIGS)


# ---------------------------------------------------------------- engine without SPY
def test_engine_runs_with_only_qqq_data():
    day = [date.fromisoformat(DAY)]
    res = run_backtest({"QQQ": {DAY: qqq_sd()}}, day, config=V2_QQQ)
    assert [(d.status, d.watchlist) for d in res.days] == [("traded", ["QQQ"])]
    assert [(t.symbol, t.side, t.signal_minute) for t in res.trades] == [("QQQ", 1, 600)]
    assert isinstance(res.trades[0].shares, float)
    with pytest.raises(KeyError):  # the SPY-referenced presets still need SPY
        run_backtest({"QQQ": {DAY: qqq_sd()}}, day, config=V1_LONGONLY)


def test_spy_incomplete_rule_not_applied_without_reference():
    day = [date.fromisoformat(DAY)]
    spy_bad = SymbolDay("SPY", DAY, False, True)
    prepared = {"QQQ": {DAY: qqq_sd()}, "SPY": {DAY: spy_bad}}
    cfg = replace(V1_LONGONLY, breakout_volume_mult=1.2)
    assert [d.status for d in run_backtest(prepared, day, config=cfg).days] == []  # SPY never ready: no scored day
    with_ref = run_backtest(prepared, day, config=replace(cfg, rs_reference="SPY"))
    assert with_ref.days == []
    res = run_backtest(prepared, day, config=replace(cfg, rs_reference=None))
    assert [d.status for d in res.days] == ["traded"] and res.days[0].watchlist == ["QQQ"]
    assert res.excluded_symbol_days == {"QQQ": 0, "SPY": 1}


def test_spy_excluded_day_after_start_only_with_reference():
    d1, d2 = "2024-03-04", "2024-03-05"
    days = [date.fromisoformat(d1), date.fromisoformat(d2)]
    spy_ok = v1_sd(long_bars(), symbol="SPY", day=d1)
    prepared = {"QQQ": {d1: qqq_sd(), d2: qqq_sd()}, "SPY": {d1: spy_ok, d2: SymbolDay("SPY", d2, False, True)}}
    cfg = replace(V1_LONGONLY, breakout_volume_mult=1.2)
    assert [d.status for d in run_backtest(prepared, days, config=cfg).days][1] == "spy_excluded"
    statuses = [d.status for d in run_backtest(prepared, days, config=replace(cfg, rs_reference=None)).days]
    assert "spy_excluded" not in statuses and statuses[1] == "traded"


def test_metrics_without_reference_has_zero_rs():
    sd = qqq_sd()
    c = UniverseFilter.metrics(sd, None)
    assert c.rs == 0.0 and c.gap == pytest.approx(0.01)
    sd2 = qqq_sd(symbol="AAA")
    wl = UniverseFilter().watchlist({"QQQ": sd, "AAA": sd2, "BBB": qqq_sd(symbol="BBB")}, None)
    assert [c.symbol for c in wl] == ["AAA", "BBB"]  # all RS 0: ties by symbol, top 2


# ---------------------------------------------------------------- breakout volume 1.2 and long only
def test_breakout_volume_mult_1_2_boundary():
    trades, skips = run_cfg(V2_QQQ, [qqq_sd(1200.0)])
    assert [(t.side, t.signal_minute) for t in trades] == [(1, 600)] and "breakout_volume" not in skips
    trades, skips = run_cfg(V2_QQQ, [qqq_sd(1199.99)])
    assert trades == [] and skips["breakout_volume"] >= 1
    assert run_cfg(V1_LONGONLY, [qqq_sd(1200.0)])[0] == []  # 1.3 still needs 1300


def test_v2_is_long_only():
    b = loud(mirror(long_bars()), [590], 1200.0)
    trades, skips = run_cfg(V2_QQQ, [v1_sd(b, direction=-1, ema=200.0, symbol="QQQ")])
    assert trades == [] and skips["short_disabled"] >= 1


# ---------------------------------------------------------------- scored start
def _qqq_days(n, bad=()):
    days = trading_days(date(2024, 1, 2), date(2024, 4, 30))[:n]
    reg = {d.isoformat(): _warm_day() for d in days}
    for k in bad:
        reg[days[k].isoformat()] = reg[days[k].isoformat()][:-1]
    return days, reg


def test_scored_start_is_first_day_qqq_ready_including_daily_values():
    days, reg = _qqq_days(30, bad=(3, 26))
    prepared = {"QQQ": prepare_symbol("QQQ", reg, days)}
    iso = [d.isoformat() for d in days]
    # RVOL base (20 complete sessions skipping day 3) is ready at day 21; the daily EMA20 restarts after day 3 and
    # needs days 4..23, so the prior daily values are first known on day 24
    assert first_scored_day(prepared, iso, None) == 21
    assert first_scored_day(prepared, iso, None, need_daily=True) == 24
    assert not prepared["QQQ"][iso[23]].daily_ready and prepared["QQQ"][iso[24]].ready
    res = run_backtest(prepared, days, config=V2_QQQ)
    assert res.days[0].day == iso[24] and res.days[-1].day == iso[29] and len(res.days) == 6
    st = {d.day: d.status for d in res.days}
    assert st[iso[26]] == "no_candidates" and "spy_excluded" not in st.values()
    assert res.excluded_symbol_days == {"QQQ": 1}
    m = BacktestReporter(res).metrics()
    assert m["period"]["first_day"] == iso[24] and m["total_trading_days"] == 6
    # with a complete SPY added, the SPY-referenced rule keeps the old start (SPY itself is V0-ready on day 20;
    # need_daily is ignored when a reference is given)
    prepared["SPY"] = prepare_symbol("SPY", {d.isoformat(): _warm_day() for d in days}, days)
    assert first_scored_day(prepared, iso, "SPY", need_daily=True) == first_scored_day(prepared, iso, "SPY") == 20


# ---------------------------------------------------------------- CLI
def test_cli_v2_qqq_loads_only_qqq_and_records_spec(tmp_path, monkeypatch):
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
        for k in range(2):
            _write_day(store, "QQQ", d1 + timedelta(days=k))
        store.commit()
    out = tmp_path / "out"
    cli.run(db, out, start=d1, end=d1 + timedelta(days=1), config=V2_QQQ)
    assert set(seen) == {"QQQ"}
    doc = json.loads((out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2_QQQ.json").read_text())
    prov = doc["provenance"]
    assert prov["config_name"] == "V2_QQQ" and prov["symbols"] == ["QQQ"]
    assert prov["config"]["rs_reference"] is None and prov["config"]["breakout_volume_mult"] == 1.2
    assert prov["spec_path"].endswith("EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2.md") and len(prov["spec_sha256"]) == 64
    assert prov["scored_start"] is None and "no RS reference" in prov["scored_start_rule"]
    assert cli.DEFAULT_OUT_NAME["V2_QQQ"] == "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2_QQQ.json"
    with pytest.raises(SystemExit):
        cli.main(["--db", db, "--out-dir", str(out), "--config", "NOPE"])


# ---------------------------------------------------------------- compare / overlap scripts
def _result(tmp_path, name, cfg, sds_by_day, prov=True):
    prepared: dict = {}
    for d, sds in sds_by_day.items():
        for sd in sds:
            prepared.setdefault(sd.symbol, {})[d] = sd
    res = run_backtest(prepared, [date.fromisoformat(d) for d in sds_by_day], config=cfg)
    p = tmp_path / f"{name}.json"
    BacktestReporter(res).to_json(p, {"config_name": cfg.name} if prov else {})
    return p


def _two_year_days():
    d1, d2 = "2024-12-31", "2025-01-02"
    b = loud(long_bars(), [590], 1300.0)
    b[0] = Bar(570, 101.0, 101.0, 100.0, 100.0, 1000.0)
    setb(b, 605, 101.5, 104.0, 101.0, 103.0)
    return {d1: [v1_sd(b, direction=1, symbol="QQQ", day=d1)], d2: [v1_sd(list(b), direction=1, symbol="QQQ", day=d2)]}


def test_compare_main_table_columns_and_per_year(tmp_path):
    cmp_mod = _load_script("compare_pullback_vwap_versions")
    days = _two_year_days()
    p_old = _result(tmp_path, "result", replace(V2_QQQ, name="V0"), days, prov=False)
    p_v2 = _result(tmp_path, "v2", V2_QQQ, days)
    md = cmp_mod.compare([p_old, p_v2])
    assert ("| config | file | period | trades | win rate | PF | net P&L (USD) | cumulative return | CAGR | "
            "MDD (USD) | MDD (%) | longest drawdown (trading days) | ref final equity (USD) | ref return | ref PF |") in md
    assert "| V0 | result.json |" in md  # no provenance config_name -> V0
    doc = json.loads(p_v2.read_text())
    m = doc["metrics"]
    row = next(line for line in md.splitlines() if line.startswith("| V2_QQQ | v2.json |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[3] == str(m["trade_count"]) == "2"
    assert cells[6] == f"{m['trade_net_pnl']:,.2f}"
    assert cells[11] == str(m["longest_drawdown_trading_days"])
    assert cells[12] == f"{m['reference_cost']['final_equity']:,.2f}"
    assert "## By year" in md and "| config | year | trades | net P&L (USD) | return | ref return |" in md
    year_rows = [line for line in md.splitlines() if line.startswith("| V2_QQQ | 20")]
    assert [r.split("|")[2].strip() for r in year_rows] == ["2023", "2024", "2025", "2026"]
    assert year_rows[0] == "| V2_QQQ | 2023 | n/a | n/a | n/a | n/a |"
    y24 = m["per_year"]["2024"]
    assert year_rows[1] == (f"| V2_QQQ | 2024 | 1 | {y24['trades_net_pnl']:,.2f} | {y24['return'] * 100:.2f}% | "
                            f"{y24['ref_return'] * 100:.2f}% |")
    assert "### V2_QQQ (v2.json)" in md and "#### By symbol" in md and "#### Long / short" in md


def test_overlap_script(tmp_path, capsys):
    ov = _load_script("pullback_vwap_overlap")

    def t(day, minute, sym="QQQ", shares=2.0, rps=1.5):
        return {"day": day, "entry_minute": minute, "symbol": sym, "shares": shares, "risk_per_share": rps}

    a = {"trades": [t("2024-01-02", 601), t("2024-01-03", 611), t("2024-01-02", 601, sym="AMD")]}
    b = {"trades": [t("2024-01-02", 601, shares=1.0), t("2024-01-03", 612, shares=4.0),
                    t("2024-01-04", 620, shares=3.0), t("2024-01-03", 611, sym="AMD")]}
    r = ov.overlap(a, b, "QQQ")
    assert r == {"symbol": "QQQ", "a_trades": 2, "b_trades": 3, "b_shared_with_a": 1,
                 "b_risk_usd_median": 4.5, "b_risk_usd_max": 6.0}
    assert ov.overlap(a, {"trades": []}, "QQQ")["b_risk_usd_median"] is None
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(a))
    pb.write_text(json.dumps(b))
    assert ov.main([str(pa), str(pb), "QQQ"]) == 0
    out = capsys.readouterr().out
    assert "sharing (day, entry_minute) with A: 1 / 3" in out and "median 4.50, max 6.00" in out
