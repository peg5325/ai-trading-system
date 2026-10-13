"""V3_QQQ / V3_TQQQ presets of the exploratory pullback-VWAP engine
(docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md): V2_QQQ without the RVOL / gap pre-market screener.

Synthetic data only: no test reads data/*.sqlite.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields, replace
from datetime import date, timedelta

import pytest

from src.explore_orb_vwap_v0 import data_loader as DL
from src.explore_orb_vwap_v0.config import (
    ALL_PRESETS,
    CONFIGS,
    PRESETS,
    V0,
    V1,
    V1_LONGONLY,
    V2_QQQ,
    V3_QQQ,
    V3_TQQQ,
    EngineConfig,
    EngineConfigV3,
)
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.strategy import first_scored_day, run_backtest
from src.explore_orb_vwap_v0.universe import UniverseFilter, prepare_symbol
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import DAY, _write_day
from tests.test_explore_pullback_vwap_v1 import _load_script
from tests.test_explore_pullback_vwap_v2 import (
    _qqq_days,
    _result,
    _two_year_days,
    qqq_sd,
)

BASE_FIELDS = ["name", "universe", "max_price", "fractional", "direction_filter", "breakout_volume_mult",
               "min_range_atr_mult", "allow_short", "rs_reference"]


def one_day(cfg, sd):
    return run_backtest({sd.symbol: {DAY: sd}}, [date.fromisoformat(DAY)], config=cfg)


# ---------------------------------------------------------------- presets
def test_v3_preset_values():
    for cfg, sym in ((V3_QQQ, "QQQ"), (V3_TQQQ, "TQQQ")):
        assert isinstance(cfg, EngineConfigV3)
        assert [f.name for f in fields(cfg)] == BASE_FIELDS + ["rvol_min", "gap_band"]
        assert cfg.universe == (sym,) and cfg.name == f"V3_{sym}"
        assert (cfg.rvol_min, cfg.gap_band) == (None, None)
        assert (cfg.max_price, cfg.fractional, cfg.direction_filter, cfg.breakout_volume_mult,
                cfg.min_range_atr_mult, cfg.allow_short, cfg.rs_reference) == (None, True, True, 1.2, 0.15, False, None)
        assert all(getattr(cfg, k) == getattr(V2_QQQ, k) for k in BASE_FIELDS[2:])
        assert cfg.daily_filters and cfg.has_v1_filters
        assert ALL_PRESETS[cfg.name] is cfg
    assert set(ALL_PRESETS) == {"V0", "V1", "V1_LONGONLY", "V2_QQQ", "V3_QQQ", "V3_TQQQ"}
    assert EngineConfigV3(**{k: getattr(V2_QQQ, k) for k in BASE_FIELDS}) == EngineConfigV3(
        **asdict(V2_QQQ), rvol_min=1.2, gap_band=(0.003, 0.02))  # V3 defaults = the V2 screener


def test_existing_presets_fields_and_screener_defaults_unchanged():
    want = {
        "V0": ("V0", ("QQQ", "SPY", "IWM", "AMD", "AAPL", "AMZN", "PLTR"), 250.0, False, False, None, None, True,
               "SPY"),
        "V1": ("V1", ("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"), None, True, True, 1.3, 0.15, True, "SPY"),
        "V1_LONGONLY": ("V1_LONGONLY", ("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"), None, True, True, 1.3, 0.15,
                        False, "SPY"),
        "V2_QQQ": ("V2_QQQ", ("QQQ",), None, True, True, 1.2, 0.15, False, None),
    }
    for cfg in (V0, V1, V1_LONGONLY, V2_QQQ):
        assert type(cfg) is EngineConfig
        assert [f.name for f in fields(cfg)] == BASE_FIELDS  # recorded provenance config dict unchanged
        assert tuple(asdict(cfg).values()) == want[cfg.name]
        assert (cfg.rvol_min, cfg.gap_band) == (1.2, (0.003, 0.02))
    assert set(PRESETS) == {"V0", "V1", "V1_LONGONLY", "V2_QQQ"} and set(CONFIGS) == {"V0", "V1", "V1_LONGONLY"}


# ---------------------------------------------------------------- screener off
def test_low_rvol_day_trades_under_v3_not_v2():
    sd = qqq_sd()
    sd.rvol_base = sd.orb_volume / 1.1  # RVOL 1.1 < 1.2
    assert [d.status for d in one_day(V2_QQQ, sd).days] == ["no_candidates"]
    res = one_day(V3_QQQ, sd)
    assert [(d.status, d.watchlist) for d in res.days] == [("traded", ["QQQ"])]
    assert [(t.symbol, t.side, t.signal_minute) for t in res.trades] == [("QQQ", 1, 600)]


@pytest.mark.parametrize("prior_close, daily_ema", [(90.0, 80.0), (100.5, 99.0), (110.0, 100.0)])
def test_gap_outside_band_trades_under_v3_not_v2(prior_close, daily_ema):
    sd = qqq_sd()  # open 101
    sd.prior_close, sd.daily_ema = prior_close, daily_ema  # gap +12.2% / +0.5%->in band / -8.2%
    gap = 101.0 / prior_close - 1
    in_band = 0.003 <= abs(gap) <= 0.02
    assert [d.status for d in one_day(V2_QQQ, sd).days] == ["traded" if in_band else "no_candidates"]
    assert [d.status for d in one_day(V3_QQQ, sd).days] == ["traded"]


def test_missing_rvol_base_ready_only_without_rvol_filter():
    sd = qqq_sd()
    sd.rvol_base = None
    assert not sd.ready and not sd.is_ready() and sd.is_ready(False)
    assert one_day(V2_QQQ, sd).days == []  # never ready -> no scored day
    res = one_day(V3_QQQ, sd)
    assert [d.status for d in res.days] == ["traded"] and res.not_ready_symbol_days == {"QQQ": 0}
    assert UniverseFilter.metrics(sd, None) is None
    c = UniverseFilter.metrics(sd, None, need_rvol=False)
    assert c.rvol is None and c.gap == pytest.approx(0.01) and c.rs == 0.0
    assert UniverseFilter(None, None, None).passes(c)
    with pytest.raises(ValueError):
        UniverseFilter(None, 0.003, None)


def test_v3_keeps_direction_and_min_range_rules():
    sd = qqq_sd()
    sd.daily_ema = 101.0  # prior close 100 < EMA -> short only, shorts disabled
    res = one_day(V3_QQQ, sd)
    assert res.trades == [] and res.skips.get("direction_filter", 0) >= 1
    sd = qqq_sd()
    sd.daily_atr = (sd.orb_high - sd.orb_low) / 0.15 + 1.0  # range < 0.15 x ATR
    res = one_day(V3_QQQ, sd)
    assert [d.status for d in res.days] == ["no_candidates"]
    assert res.filter_symbol_days["min_range"] == {"QQQ": 1}


# ---------------------------------------------------------------- scored start without RVOL readiness
def test_scored_start_does_not_need_rvol_base():
    days, reg = _qqq_days(30)
    prepared = {"QQQ": prepare_symbol("QQQ", reg, days)}
    iso = [d.isoformat() for d in days]
    # without the daily filters only RVOL readiness separates the two rules: day 1 (prior close + warm 5m) vs day 20
    assert first_scored_day(prepared, iso, None) == 20
    assert first_scored_day(prepared, iso, None, need_rvol=False) == 1
    no_daily = {"direction_filter": False, "min_range_atr_mult": None}
    r2 = run_backtest(prepared, days, config=replace(V2_QQQ, **no_daily))
    r3 = run_backtest(prepared, days, config=replace(V3_QQQ, **no_daily))
    assert r2.days[0].day == iso[20] and r3.days[0].day == iso[1] and len(r3.days) == 29
    # with < 20 prior sessions the RVOL-ready rule scores nothing; V3 readiness still does
    short_days, short_reg = _qqq_days(15)
    short = {"QQQ": prepare_symbol("QQQ", short_reg, short_days)}
    assert run_backtest(short, short_days, config=replace(V2_QQQ, **no_daily)).days == []
    assert len(run_backtest(short, short_days, config=replace(V3_QQQ, **no_daily)).days) == 14
    # the V3 presets keep the daily EMA20 / ATR14 requirement (spec V3 §2), which needs 20 continuous complete
    # sessions, so on this data the V3 start equals the V2 start (day 20)
    assert first_scored_day(prepared, iso, None, need_daily=True, need_rvol=False) == 20
    assert run_backtest(prepared, days, config=V3_QQQ).days[0].day == iso[20]
    assert run_backtest(short, short_days, config=V3_QQQ).days == []


def test_scored_start_after_incomplete_day_v3_equals_daily_rule():
    days, reg = _qqq_days(30, bad=(3, 26))
    prepared = {"QQQ": prepare_symbol("QQQ", reg, days)}
    iso = [d.isoformat() for d in days]
    res = run_backtest(prepared, days, config=V3_QQQ)
    assert res.days[0].day == iso[24] and len(res.days) == 6
    assert {d.day: d.status for d in res.days}[iso[26]] == "no_candidates"
    assert res.excluded_symbol_days == {"QQQ": 1}
    assert BacktestReporter(res).metrics()["period"]["first_day"] == iso[24]


# ---------------------------------------------------------------- CLI
def _cli_db(tmp_path, symbol, n=2):
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(n):
            _write_day(store, symbol, d1 + timedelta(days=k))
        store.commit()
    return db, d1


def test_cli_v3_tqqq_loads_only_tqqq_and_records_spec(tmp_path, monkeypatch):
    cli = _load_script("explore_pullback_vwap_v0")
    seen = []
    real = DL.research_load

    def spy_load(db, symbol, *a, **k):
        seen.append(symbol)
        return real(db, symbol, *a, **k)

    monkeypatch.setattr(DL, "research_load", spy_load)
    db, d1 = _cli_db(tmp_path, "TQQQ")
    out = tmp_path / "out"
    cli.run(db, out, start=d1, end=d1 + timedelta(days=1), config=ALL_PRESETS["V3_TQQQ"])
    assert set(seen) == {"TQQQ"}
    doc = json.loads((out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3_TQQQ.json").read_text())
    prov = doc["provenance"]
    assert prov["config_name"] == "V3_TQQQ" and prov["symbols"] == ["TQQQ"]
    assert prov["config"]["rvol_min"] is None and prov["config"]["gap_band"] is None
    assert prov["config"]["universe"] == ["TQQQ"] and prov["config"]["rs_reference"] is None
    spec = cli.ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md"
    assert prov["spec_path"] == "docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md"
    assert prov["spec_sha256"] == hashlib.sha256(spec.read_bytes()).hexdigest()
    assert "scored_start" in prov and prov["scored_start"] is None
    assert "20-session RVOL" not in prov["scored_start_rule"]
    assert "RVOL base not required (spec V3 §2)" in prov["scored_start_rule"]
    assert cli.DEFAULT_OUT_NAME["V3_QQQ"] == "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3_QQQ.json"
    assert cli.SPECS["V3_QQQ"] == cli.SPECS["V3_TQQQ"] == spec
    with pytest.raises(SystemExit):  # write-once
        cli.run(db, out, start=d1, end=d1 + timedelta(days=1), config=V3_TQQQ)


@pytest.mark.parametrize("name", ["V3_QQQ", "V3_TQQQ"])
def test_cli_main_accepts_v3(tmp_path, monkeypatch, capsys, name):
    cli = _load_script("explore_pullback_vwap_v0")
    got = {}

    def fake_run(db, out_dir, config, out_name):
        got.update(config=config, out_name=out_name)
        return "ok\n"

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["--db", "x.sqlite", "--out-dir", str(tmp_path), "--config", name]) == 0
    assert got == {"config": ALL_PRESETS[name], "out_name": None} and capsys.readouterr().out == "ok\n\n"


def test_cli_v2_scored_start_rule_text_unchanged(tmp_path):
    cli = _load_script("explore_pullback_vwap_v0")
    db, d1 = _cli_db(tmp_path, "QQQ")
    out = tmp_path / "out"
    cli.run(db, out, start=d1, end=d1 + timedelta(days=1), config=V2_QQQ)
    prov = json.loads((out / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2_QQQ.json").read_text())["provenance"]
    assert prov["scored_start_rule"] == ("first trading day on which a traded symbol is complete with prior close, "
                                         "20-session RVOL base, warm 5m ATR/EMA and prior daily EMA20/ATR14; "
                                         "no RS reference (spec V2 §1.3)")
    assert list(prov["config"]) == BASE_FIELDS


# ---------------------------------------------------------------- compare script
def test_compare_new_columns(tmp_path):
    cmp_mod = _load_script("compare_pullback_vwap_versions")
    days = _two_year_days()
    p_v2 = _result(tmp_path, "v2", V2_QQQ, days)
    p_v3 = _result(tmp_path, "v3", V3_QQQ, days)
    p_none = _result(tmp_path, "none", V3_TQQQ, {})
    md = cmp_mod.compare([p_v2, p_v3, p_none])
    assert ("| ref final equity (USD) | ref return | ref PF | total trading days | traded days | avg win (USD) | "
            "avg loss (USD) |") in md
    header = next(line for line in md.splitlines() if line.startswith("| config |"))
    sep = md.splitlines()[md.splitlines().index(header) + 1]
    assert header.count("|") == sep.count("|") == 20
    m = json.loads(p_v3.read_text())["metrics"]
    row = next(line for line in md.splitlines() if line.startswith("| V3_QQQ | v3.json |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert len(cells) == 19
    assert cells[3] == str(m["trade_count"])  # existing columns keep their positions
    assert cells[15] == str(m["total_trading_days"]) == "2"
    assert cells[16] == str(m["trade_days"]) == "2"
    assert cells[17] == (f"{m['trade_avg_win']:,.2f}" if m["trade_avg_win"] is not None else "n/a")
    assert cells[18] == (f"{m['trade_avg_loss']:,.2f}" if m["trade_avg_loss"] is not None else "n/a")
    none_row = next(line for line in md.splitlines() if line.startswith("| V3_TQQQ | none.json |"))
    assert none_row.count("|") == 20


def test_compare_json_out_and_overwrite_refusal(tmp_path, capsys):
    cmp_mod = _load_script("compare_pullback_vwap_versions")
    days = _two_year_days()
    paths = [_result(tmp_path, "v2", V2_QQQ, days), _result(tmp_path, "v3", V3_QQQ, days),
             _result(tmp_path, "result", replace(V2_QQQ, name="V0"), days, prov=False)]
    out = tmp_path / "cmp" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.json"
    assert cmp_mod.main([*map(str, paths), "--json-out", str(out)]) == 0
    md = capsys.readouterr().out
    assert md == cmp_mod.compare(paths)
    doc = json.loads(out.read_text())
    assert [c["label"] for c in doc["configs"]] == ["V2_QQQ", "V3_QQQ", "V0"]
    assert doc["generated_from"] == [str(p) for p in paths]
    for c, p in zip(doc["configs"], paths):
        m = json.loads(p.read_text())["metrics"]
        assert c["file"] == p.name and c["file_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
        assert c["per_year"] == m["per_year"]
        for k in ("total_trading_days", "trade_days", "trade_count", "trade_win_rate", "trade_profit_factor",
                  "trade_net_pnl", "cumulative_return", "cagr", "mdd_usd", "mdd_pct", "trade_avg_win",
                  "trade_avg_loss", "longest_drawdown_trading_days", "period"):
            assert c["metrics"][k] == m[k]
        assert c["metrics"]["reference_cost"] == {k: m["reference_cost"][k]
                                                  for k in ("final_equity", "cumulative_return", "profit_factor")}
    before = out.read_bytes()
    with pytest.raises(SystemExit):
        cmp_mod.main([*map(str, paths), "--json-out", str(out)])
    assert out.read_bytes() == before
