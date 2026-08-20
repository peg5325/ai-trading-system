from __future__ import annotations

import hashlib
import inspect
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import ET, SESSION_REGULAR, Candle
from src.research.high52_rs_breakout.contract import canonical_json_bytes, load_contract
from src.research.high52_rs_breakout.development import (
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
from src.research.high52_rs_breakout.loading import (
    load_development_bars,
    official_symbols,
)
from src.research.high52_rs_breakout.report import render_markdown


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


def _feature(
    candle: Candle,
    *,
    atr: float = 2.0,
    sma: float = 90.0,
    high252: float = 101.0,
    proximity: float = 0.99,
    return63: float = 0.2,
    prior20: float = 99.0,
    breakout: float = 0.01,
    first: bool = True,
) -> FeatureBar:
    return FeatureBar(
        day=candle.ts_exchange.date(),
        candle=candle,
        atr14=atr,
        sma200=sma,
        high252=high252,
        high52_proximity=proximity,
        return63=return63,
        prior20_high=prior20,
        breakout_strength=breakout,
        first_breakout=first,
    )


def _contract():
    return load_contract()


def _candidate(
    symbol: str,
    signal_day: date,
    entry_day: date,
    exit_day: date,
    *,
    return63: float = 0.2,
) -> Candidate:
    return Candidate(
        symbol,
        signal_day,
        entry_day,
        exit_day,
        2.0,
        return63,
        0.01,
        0.99,
    )


def test_contract_and_official_universe_are_locked():
    contract = _contract()
    symbols = official_symbols(contract["data"]["universe_path"])
    assert contract["strategy_id"] == "p0_us30_high52_rs_breakout_v1"
    assert len(symbols) == 26
    assert not {"TQQQ", "SQQQ", "SOXL", "SOXS"} & set(symbols)


def test_feature_windows_match_hand_calculation():
    start = date(2023, 1, 1)
    bars = []
    for index in range(252):
        close = 100 + index * 0.1
        bars.append(
            _candle(
                "A",
                start + timedelta(days=index),
                open_=close - 0.1,
                high=close + 1,
                low=close - 1,
                close=close,
            )
        )
    features = build_features(bars)
    ordered = list(features.values())
    assert ordered[12].atr14 is None
    assert ordered[13].atr14 == pytest.approx(2.0)
    assert ordered[198].sma200 is None
    assert ordered[199].sma200 == pytest.approx(
        sum(100 + index * 0.1 for index in range(200)) / 200
    )
    last = ordered[-1]
    assert last.high252 == pytest.approx(126.1)
    assert last.high52_proximity == pytest.approx(125.1 / 126.1)
    assert last.return63 == pytest.approx(125.1 / 118.8 - 1)
    assert last.prior20_high == pytest.approx(126.0)


def test_first_breakout_excludes_current_bar_and_blocks_consecutive_signal():
    start = date(2024, 1, 1)
    bars = [
        _candle("A", start + timedelta(days=index), high=100, low=98, close=99)
        for index in range(21)
    ]
    bars.append(_candle("A", start + timedelta(days=21), high=102, low=99, close=101))
    bars.append(_candle("A", start + timedelta(days=22), high=104, low=101, close=103))
    features = list(build_features(bars).values())
    assert features[-2].prior20_high == 100
    assert features[-2].first_breakout is True
    assert features[-1].prior20_high == 102
    assert features[-1].first_breakout is False


@pytest.mark.parametrize(
    ("sma", "proximity", "expected"),
    [(99.99, 0.97, True), (100.0, 0.97, False), (99.0, 0.969999, False)],
)
def test_signal_boundaries(sma: float, proximity: float, expected: bool):
    feature = _feature(
        _candle("A", date(2024, 1, 2), close=100),
        sma=sma,
        proximity=proximity,
    )
    assert is_signal(feature, _contract()) is expected


def _cross_section_features(symbols: tuple[str, ...], days: tuple[date, ...]):
    result = {}
    for index, symbol in enumerate(symbols):
        result[symbol] = {
            day: _feature(
                _candle(symbol, day, close=100),
                return63=1.0 - index / 100,
                first=day == days[0],
            )
            for day in days
        }
    return result


def test_cross_section_uses_ceil_top_20_percent_and_is_input_invariant():
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    symbols = tuple(f"S{index:02d}" for index in range(21))
    features = _cross_section_features(symbols, (d0, d1))
    forward = prepare_candidates(features, (d0, d1), holding_days=1, contract=_contract())
    reverse = prepare_candidates(
        dict(reversed(list(features.items()))),
        (d0, d1),
        holding_days=1,
        contract=_contract(),
    )
    assert [candidate.symbol for candidate in forward.candidates] == list(symbols[:5])
    assert forward.candidates == reverse.candidates


def test_cross_section_tie_break_is_symbol_ascending():
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    symbols = tuple(f"S{index:02d}" for index in range(20))
    features = _cross_section_features(symbols, (d0, d1))
    for symbol in symbols:
        features[symbol][d0] = _feature(
            _candle(symbol, d0), return63=0.5, first=True
        )
    prepared = prepare_candidates(
        dict(reversed(list(features.items()))),
        (d0, d1),
        holding_days=1,
        contract=_contract(),
    )
    assert [candidate.symbol for candidate in prepared.candidates] == list(symbols[:4])


def test_cross_section_below_twenty_is_fail_closed_for_the_day():
    d0, d1 = date(2024, 1, 2), date(2024, 1, 3)
    symbols = tuple(f"S{index:02d}" for index in range(19))
    prepared = prepare_candidates(
        _cross_section_features(symbols, (d0, d1)),
        (d0, d1),
        holding_days=1,
        contract=_contract(),
    )
    assert prepared.candidates == ()
    assert prepared.insufficient_cross_section_days == 2


def test_missing_holding_bar_is_counted_fail_closed():
    d0, d1, d2 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    symbols = tuple(f"S{index:02d}" for index in range(20))
    features = _cross_section_features(symbols, (d0, d1, d2))
    for symbol in symbols[1:]:
        features[symbol][d0] = _feature(
            _candle(symbol, d0), return63=0.0, first=False
        )
    del features[symbols[0]][d1]
    prepared = prepare_candidates(
        features, (d0, d1, d2), holding_days=2, contract=_contract()
    )
    assert prepared.data_exclusions == 1
    assert prepared.data_exclusion_rate == 1
    assert prepared.data_exclusion_reasons == {"missing_holding_bar": 1}


def test_next_open_entry_and_fifth_day_close_exit():
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(6))
    features = {
        "A": {
            day: _feature(_candle("A", day, open_=100, high=103, low=98, close=101))
            for day in days
        }
    }
    simulation = simulate(
        features,
        days,
        [_candidate("A", days[0], days[1], days[5])],
        contract=_contract(),
    )
    trade = simulation.trades[0]
    assert trade.entry_day == days[1].isoformat()
    assert trade.exit_day == days[5].isoformat()
    assert trade.holding_days == 5
    assert trade.exit_reason == "time_exit"


def test_holding_period_counts_market_days_across_weekend():
    days = (
        date(2024, 5, 23),
        date(2024, 5, 24),
        date(2024, 5, 28),  # Memorial Day 5/27 제외
        date(2024, 5, 29),
        date(2024, 5, 30),
        date(2024, 5, 31),
    )
    features = {
        "A": {
            day: _feature(_candle("A", day, open_=100, high=103, low=98, close=101))
            for day in days
        }
    }
    simulation = simulate(
        features,
        days,
        [_candidate("A", days[0], days[1], days[5])],
        contract=_contract(),
    )
    trade = simulation.trades[0]
    assert trade.exit_day == "2024-05-31"
    assert trade.holding_days == 5


def test_gap_and_intraday_stop_precede_time_exit():
    d0, d1, d2 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    features = {
        "A": {
            d0: _feature(_candle("A", d0)),
            d1: _feature(_candle("A", d1, open_=100, high=102, low=98, close=101)),
            d2: _feature(_candle("A", d2, open_=96, high=98, low=95, close=97)),
        },
        "B": {
            d0: _feature(_candle("B", d0)),
            d1: _feature(_candle("B", d1, open_=100, high=110, low=96, close=109)),
            d2: _feature(_candle("B", d2)),
        },
    }
    gap = simulate(
        features,
        (d0, d1, d2),
        [_candidate("A", d0, d1, d2)],
        contract=_contract(),
    ).trades[0]
    intraday = simulate(
        features,
        (d0, d1, d2),
        [_candidate("B", d0, d1, d1)],
        contract=_contract(),
    ).trades[0]
    assert (gap.exit_reason, gap.exit_price) == ("gap_stop", 96)
    assert (intraday.exit_reason, intraday.exit_price) == ("intraday_stop", 97)


def test_capacity_uses_relative_strength_rank():
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
        _candidate(symbol, d0, d1, d1, return63=1 - index / 10)
        for index, symbol in enumerate(symbols)
    ]
    simulation = simulate(features, (d0, d1), reversed(candidates), contract=_contract())
    assert simulation.skipped_capacity == 1
    assert {trade.symbol for trade in simulation.trades} == set("ABCDE")


def test_round_trip_cost_is_charged_once():
    trade = Trade(
        "A", "2024-01-02", "2024-01-03", "2024-01-03",
        100, 101, 97, 10, 1000, 10, "time_exit", 1,
    )
    assert costed_pnl(trade, 0.34) == pytest.approx(6.6)


def test_mark_to_market_drawdown_and_loss_streak():
    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    features = {
        "A": {
            d1: _feature(_candle("A", d1, close=100)),
            d2: _feature(_candle("A", d2, open_=80, high=81, low=79, close=80)),
            d3: _feature(_candle("A", d3, open_=110, high=111, low=109, close=110)),
        }
    }
    trade = Trade(
        "A", d1.isoformat(), d1.isoformat(), d3.isoformat(),
        100, 110, 70, 10, 1000, 100, "time_exit", 3,
    )
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


def test_chronological_blocks_cover_calendar_once():
    calendar = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(23))
    blocks = chronological_blocks(calendar, 10)
    assert tuple(day for block in blocks for day in block) == calendar
    assert [len(block) for block in blocks] == [3, 3, 3, 2, 2, 2, 2, 2, 2, 2]


def test_data_exclusion_rate_boundary_is_inclusive():
    candidate = _candidate("A", date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 2))
    prepared = PreparedCandidates(
        candidates=(candidate,) * 19,
        cross_section_days=1,
        insufficient_cross_section_days=0,
        qualifying_signals=20,
        boundary_skips=0,
        data_exclusions=1,
        data_exclusion_reasons={"missing_holding_bar": 1},
    )
    assert prepared.data_exclusion_rate == pytest.approx(0.05)


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


def test_development_loader_uses_swing_half_open_range_without_grant(monkeypatch):
    calls = []

    def fake_research_load(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "src.research.high52_rs_breakout.loading.research_load", fake_research_load
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


def test_json_and_markdown_are_deterministic():
    result = {
        "verdict": "NO_SELECTION",
        "strategy_id": "x",
        "contract_sha256": "a",
        "data_content_sha256": "b",
        "development_window": {
            "start": "2024-01-01",
            "end_inclusive": "2024-01-02",
            "calendar_days": 2,
        },
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
                "cost_levels": {
                    level: _minimal_metrics()
                    for level in ("low", "official", "stress")
                },
                "candidate_audit": {
                    "cross_section_days": 0,
                    "insufficient_cross_section_days": 0,
                    "qualifying_signals": 0,
                    "boundary_skips": 0,
                    "prepared_candidates": 0,
                    "data_exclusions": 0,
                    "data_exclusion_rate": 0,
                },
                "portfolio_skips": {
                    "capacity": 0,
                    "duplicate_symbol": 0,
                    "quantity_lt_one": 0,
                },
                "exit_reasons": {},
                "holding_days_mean": 0,
                "holding_days_median": 0,
                "trades_sha256": "0" * 64,
                "folds_official": [],
            }
            for name in ("primary_5d", "sensitivity_3d")
        },
        "data_gates": {"DQ": True},
        "economic_gates": {"D": False},
    }
    assert canonical_json_bytes(result) == canonical_json_bytes(result)
    assert render_markdown(result) == render_markdown(result)


def test_tracked_result_payload_and_markdown_are_reproducible():
    json_path = Path(
        "docs/research/RESULT_P0_US30_HIGH52_RS_BREAKOUT_DEVELOPMENT.json"
    )
    md_path = Path(
        "docs/research/RESULT_P0_US30_HIGH52_RS_BREAKOUT_DEVELOPMENT.md"
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
        "max_symbol_contribution": None,
        "max_entry_day_contribution": None,
        "net_pnl_by_symbol": {},
        "net_pnl_by_entry_day": {},
    }
