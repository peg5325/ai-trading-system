from __future__ import annotations

import hashlib
import inspect
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import ET, SESSION_REGULAR, Candle
from src.research.close_pressure_reversal.contract import (
    canonical_json_bytes,
    load_contract,
)
from src.research.close_pressure_reversal.development import (
    Candidate,
    FeatureBar,
    PreparedCandidates,
    Trade,
    build_features,
    chronological_blocks,
    costed_pnl,
    is_signal,
    prepare_candidates,
    select_verdict,
    simulate,
    summarize,
)
from src.research.close_pressure_reversal.loading import (
    load_development_bars,
    official_symbols,
)
from src.research.close_pressure_reversal.report import render_markdown


def _candle(
    symbol: str,
    day: date,
    *,
    open_: float = 100.0,
    high: float = 102.0,
    low: float = 98.0,
    close: float = 100.0,
) -> Candle:
    ts_et = datetime(day.year, day.month, day.day, tzinfo=ET)
    return Candle(
        symbol=symbol,
        interval="1d",
        ts_utc=ts_et.astimezone(timezone.utc),
        ts_exchange=ts_et,
        session=SESSION_REGULAR,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
        currency="USD",
        adjusted=True,
        market="US",
    )


def _feature(candle: Candle, *, atr: float = 2.0, score: float = 1.0) -> FeatureBar:
    return FeatureBar(
        day=candle.ts_exchange.date(),
        candle=candle,
        atr14=atr,
        day_return=candle.close / candle.open - 1,
        close_location=(candle.close - candle.low) / (candle.high - candle.low),
        pressure_score=score,
    )


def _contract():
    return load_contract()


def test_contract_and_official_universe_are_locked():
    contract = _contract()
    symbols = official_symbols(contract["data"]["universe_path"])
    assert contract["strategy_id"] == "p0_us30_close_pressure_reversal_v1"
    assert len(symbols) == 26
    assert not {"TQQQ", "SQQQ", "SOXL", "SOXS"} & set(symbols)


def test_atr14_uses_signal_day_and_needs_fourteen_true_ranges():
    start = date(2024, 1, 1)
    bars = [
        _candle("A", start + timedelta(days=idx), high=101, low=99, close=100)
        for idx in range(14)
    ]
    features = build_features(bars)
    assert list(features.values())[-2].atr14 is None
    assert list(features.values())[-1].atr14 == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("close", "expected"),
    [
        (99.0, True),   # day return=-1%, close_location=0.2
        (99.0001, False),
        (98.0, True),
    ],
)
def test_signal_boundaries_are_inclusive(close: float, expected: bool):
    candle = _candle("A", date(2024, 1, 2), high=103, low=98, close=close)
    feature = _feature(candle, atr=2)
    assert is_signal(feature, _contract()) is expected


def test_zero_range_is_not_a_signal():
    candle = _candle("A", date(2024, 1, 2), open_=99, high=99, low=99, close=99)
    feature = FeatureBar(
        day=candle.ts_exchange.date(),
        candle=candle,
        atr14=2,
        day_return=0,
        close_location=None,
        pressure_score=0,
    )
    assert not is_signal(feature, _contract())


def test_candidate_order_is_pressure_then_return_then_symbol_and_input_invariant():
    d0, d1, d2 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    calendar = (d0, d1, d2)
    features = {}
    for symbol, score, close in (("B", 2.0, 98.0), ("A", 2.0, 98.0), ("C", 3.0, 98.5)):
        signal = _candle(symbol, d0, high=103, low=98, close=close)
        features[symbol] = {
            d0: FeatureBar(d0, signal, 2.0, close / 100 - 1, (close - 98) / 5, score),
            d1: _feature(_candle(symbol, d1)),
            d2: _feature(_candle(symbol, d2)),
        }
    forward = prepare_candidates(features, calendar, holding_days=1, contract=_contract())
    reverse = prepare_candidates(dict(reversed(list(features.items()))), calendar, holding_days=1, contract=_contract())
    expected = ["C", "A", "B"]
    assert [candidate.symbol for candidate in forward.candidates] == expected
    assert forward.candidates == reverse.candidates


def test_missing_holding_bar_is_counted_fail_closed():
    d0, d1, d2, d3 = (date(2024, 1, day) for day in range(2, 6))
    signal = _candle("A", d0, high=103, low=98, close=98.5)
    features = {
        "A": {
            d0: FeatureBar(d0, signal, 2, -0.015, 0.1, 2),
            d1: _feature(_candle("A", d1)),
            # d2 missing
            d3: _feature(_candle("A", d3)),
        }
    }
    prepared = prepare_candidates(features, (d0, d1, d2, d3), holding_days=3, contract=_contract())
    assert prepared.data_exclusions == 1
    assert prepared.data_exclusion_rate == 1
    assert prepared.data_exclusion_reasons == {"missing_holding_bar": 1}


def test_zero_range_execution_bar_is_excluded_fail_closed():
    d0, d1, d2 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    signal = _candle("A", d0, high=103, low=98, close=98.5)
    zero_range = _candle(
        "A", d1, open_=100, high=100, low=100, close=100
    )
    features = {
        "A": {
            d0: FeatureBar(d0, signal, 2, -0.015, 0.1, 2),
            d1: FeatureBar(d1, zero_range, 2, 0.0, None, 0.0),
            d2: _feature(_candle("A", d2)),
        }
    }
    prepared = prepare_candidates(
        features, (d0, d1, d2), holding_days=2, contract=_contract()
    )
    assert prepared.candidates == ()
    assert prepared.data_exclusions == 1
    assert prepared.data_exclusion_reasons == {"invalid_execution_bar": 1}


def test_historical_oos_identity_touch_blocks_positive_promotion():
    assert select_verdict(
        data_gates={"DQ": True},
        economic_gates={"D": True},
        historical_oos_identity_touch=True,
    ) == "REQUIRES_NEW_PREREG"
    assert select_verdict(
        data_gates={"DQ": True},
        economic_gates={"D": False},
        historical_oos_identity_touch=True,
    ) == "NO_SELECTION"


def _candidate(symbol: str, d0: date, d1: date, d3: date, *, score: float = 1) -> Candidate:
    return Candidate(symbol, d0, d1, d3, 2.0, -0.02, score)


def test_next_open_entry_and_third_day_close_exit():
    d0, d1, d2, d3 = (date(2024, 1, day) for day in range(2, 6))
    features = {
        "A": {
            day: _feature(_candle("A", day, open_=100, high=103, low=98, close=101))
            for day in (d0, d1, d2, d3)
        }
    }
    simulation = simulate(
        features,
        (d0, d1, d2, d3),
        [_candidate("A", d0, d1, d3)],
        contract=_contract(),
    )
    trade = simulation.trades[0]
    assert trade.entry_day == d1.isoformat()
    assert trade.exit_day == d3.isoformat()
    assert trade.exit_reason == "time_exit"
    assert trade.holding_days == 3


def test_gap_through_stop_fills_at_open():
    d0, d1, d2, d3 = (date(2024, 1, day) for day in range(2, 6))
    features = {
        "A": {
            d0: _feature(_candle("A", d0)),
            d1: _feature(_candle("A", d1, open_=100, high=102, low=98, close=101)),
            d2: _feature(_candle("A", d2, open_=96, high=98, low=95, close=97)),
            d3: _feature(_candle("A", d3)),
        }
    }
    simulation = simulate(features, (d0, d1, d2, d3), [_candidate("A", d0, d1, d3)], contract=_contract())
    trade = simulation.trades[0]
    assert trade.stop_price == 97
    assert trade.exit_price == 96
    assert trade.exit_reason == "gap_stop"


def test_intraday_stop_precedes_same_day_time_exit():
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    features = {
        "A": {
            d0: _feature(_candle("A", d0)),
            d1: _feature(_candle("A", d1, open_=100, high=110, low=96, close=109)),
        }
    }
    candidate = _candidate("A", d0, d1, d1)
    simulation = simulate(features, (d0, d1), [candidate], contract=_contract())
    assert simulation.trades[0].exit_reason == "intraday_stop"
    assert simulation.trades[0].exit_price == 97


def test_capacity_uses_deterministic_candidate_rank():
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    symbols = tuple("ABCDEF")
    features = {
        symbol: {
            d0: _feature(_candle(symbol, d0)),
            d1: _feature(_candle(symbol, d1, close=101)),
        }
        for symbol in symbols
    }
    candidates = [
        _candidate(symbol, d0, d1, d1, score=float(10 - idx))
        for idx, symbol in enumerate(symbols)
    ]
    simulation = simulate(features, (d0, d1), reversed(candidates), contract=_contract())
    assert simulation.skipped_capacity == 1
    assert {trade.symbol for trade in simulation.trades} == set("ABCDE")


def test_round_trip_cost_is_charged_once_on_entry_notional():
    trade = Trade("A", "2024-01-02", "2024-01-03", "2024-01-03", 100, 101, 97, 10, 1000, 10, "time_exit", 1)
    assert costed_pnl(trade, 0.34) == pytest.approx(6.6)


def test_chronological_blocks_cover_calendar_once_and_in_order():
    calendar = tuple(date(2024, 1, 1) + timedelta(days=idx) for idx in range(23))
    blocks = chronological_blocks(calendar, 10)
    assert tuple(day for block in blocks for day in block) == calendar
    assert [len(block) for block in blocks] == [3, 3, 3, 2, 2, 2, 2, 2, 2, 2]


def test_holding_calendar_skips_weekend_without_extending_holding_count():
    friday = date(2024, 1, 5)
    monday = date(2024, 1, 8)
    tuesday = date(2024, 1, 9)
    wednesday = date(2024, 1, 10)
    features = {
        "A": {
            day: _feature(_candle("A", day, close=101))
            for day in (friday, monday, tuesday, wednesday)
        }
    }
    simulation = simulate(
        features,
        (friday, monday, tuesday, wednesday),
        [_candidate("A", friday, monday, wednesday)],
        contract=_contract(),
    )
    assert simulation.trades[0].holding_days == 3
    assert simulation.trades[0].exit_day == wednesday.isoformat()


def test_data_exclusion_rate_boundary_is_inclusive():
    prepared = PreparedCandidates(
        candidates=tuple(_candidate("A", date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 2)) for _ in range(19)),
        qualifying_signals=20,
        boundary_skips=0,
        data_exclusions=1,
        data_exclusion_reasons={"missing_holding_bar": 1},
    )
    assert prepared.data_exclusion_rate == pytest.approx(0.05)


def test_mark_to_market_drawdown_and_loss_streak_are_reported():
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    features = {
        "A": {
            d1: _feature(_candle("A", d1, open_=100, high=101, low=99, close=100)),
            d2: _feature(_candle("A", d2, open_=100, high=101, low=79, close=80)),
            d3: _feature(_candle("A", d3, open_=100, high=111, low=99, close=110)),
        }
    }
    trade = Trade("A", d1.isoformat(), d1.isoformat(), d3.isoformat(), 100, 110, 70, 10, 1000, 100, "time_exit", 3)
    metrics = summarize(
        (trade,),
        calendar=(d1, d2, d3),
        features=features,
        cost_pct_points=0,
        initial_capital=1000,
    )
    assert metrics["max_drawdown_pct"] == pytest.approx(-20)
    assert metrics["daily_equity"][1]["equity"] == pytest.approx(800)
    assert metrics["max_consecutive_losses"] == 0


def test_consecutive_losses_and_contributions_use_costed_pnl():
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    features = {
        symbol: {
            d1: _feature(_candle(symbol, d1)),
            d2: _feature(_candle(symbol, d2)),
            d3: _feature(_candle(symbol, d3)),
        }
        for symbol in ("A", "B", "C")
    }
    trades = (
        Trade("A", d1.isoformat(), d1.isoformat(), d1.isoformat(), 100, 99, 97, 1, 100, -1, "time_exit", 1),
        Trade("B", d1.isoformat(), d2.isoformat(), d2.isoformat(), 100, 99, 97, 1, 100, -1, "time_exit", 1),
        Trade("C", d1.isoformat(), d3.isoformat(), d3.isoformat(), 100, 105, 97, 1, 100, 5, "time_exit", 1),
    )
    metrics = summarize(
        trades,
        calendar=(d1, d2, d3),
        features=features,
        cost_pct_points=0.34,
        initial_capital=1000,
    )
    assert metrics["max_consecutive_losses"] == 2
    assert metrics["net_pnl_by_symbol"]["A"] == pytest.approx(-1.34)
    assert metrics["net_pnl_by_entry_day"][d3.isoformat()] == pytest.approx(4.66)


def test_development_loader_uses_locked_half_open_range_and_no_oos_grant(monkeypatch):
    calls = []

    def fake_research_load(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "src.research.close_pressure_reversal.loading.research_load",
        fake_research_load,
    )
    load_development_bars(
        "ignored.sqlite",
        symbols=("SPY",),
        start=date(2016, 3, 18),
        end_inclusive=date(2025, 7, 18),
    )
    _, kwargs = calls[0]
    assert kwargs["start"].date() == date(2016, 3, 18)
    assert kwargs["end"].date() == date(2025, 7, 19)
    assert kwargs["purpose"] == "performance"
    assert kwargs["track"] == "swing"
    assert "execution_grant" not in kwargs
    assert "unlock" not in kwargs
    assert "OOSUnlock" not in inspect.getsource(load_development_bars)


def test_json_and_markdown_rendering_are_deterministic():
    result = {
        "verdict": "NO_SELECTION",
        "strategy_id": "x",
        "contract_sha256": "a",
        "data_content_sha256": "b",
        "development_window": {"start": "2024-01-01", "end_inclusive": "2024-01-02", "calendar_days": 2},
        "symbol_count": 1,
        "blind_oos_opened": True,
        "blind_oos_performance_accessed": False,
        "current_execution_blind_oos_ohlcv_accessed": False,
        "prereg_compliance": "DEVIATION_ACCEPTED_BY_APPEND_ONLY_ERRATUM",
        "erratum_path": "docs/research/ERRATUM.md",
        "payload_sha256": "abc",
        "execution_audit": {
            "issue": "daily_identity_hash_scanned_blind_oos_ohlcv",
            "fix": "development_bounded_hash",
            "strategy_rules_changed": False,
            "result_regenerated": True,
        },
        "governance_gates": {
            "G0_positive_promotion_requires_pristine_prereg": False
        },
        "specs": {
            name: {
                "gross": _minimal_metrics(),
                "cost_levels": {level: _minimal_metrics() for level in ("low", "official", "stress")},
                "candidate_audit": {"qualifying_signals": 0, "boundary_skips": 0, "prepared_candidates": 0, "data_exclusions": 0, "data_exclusion_rate": 0},
                "portfolio_skips": {"capacity": 0, "duplicate_symbol": 0, "quantity_lt_one": 0},
                "exit_reasons": {},
                "holding_days_mean": 0,
                "holding_days_median": 0,
                "trades_sha256": "0" * 64,
                "folds_official": [],
            }
            for name in ("primary_3d", "sensitivity_1d")
        },
        "data_gates": {"DQ": True},
        "economic_gates": {"D": False},
    }
    assert canonical_json_bytes(result) == canonical_json_bytes(result)
    assert render_markdown(result) == render_markdown(result)


def test_tracked_result_payload_and_markdown_are_reproducible():
    json_path = Path(
        "docs/research/RESULT_P0_US30_CLOSE_PRESSURE_REVERSAL_DEVELOPMENT.json"
    )
    md_path = Path(
        "docs/research/RESULT_P0_US30_CLOSE_PRESSURE_REVERSAL_DEVELOPMENT.md"
    )
    result = json.loads(json_path.read_text(encoding="utf-8"))
    expected = result.pop("payload_sha256")
    assert hashlib.sha256(canonical_json_bytes(result)).hexdigest() == expected
    result["payload_sha256"] = expected
    assert render_markdown(result) == md_path.read_text(encoding="utf-8")


def _minimal_metrics():
    return {
        "trade_count": 0,
        "total_net_pnl": 0,
        "net_expectancy": 0,
        "profit_factor": None,
        "win_rate": 0,
        "max_drawdown_pct": 0,
        "max_consecutive_losses": 0,
        "net_pnl_by_symbol": {},
        "net_pnl_by_entry_day": {},
    }
