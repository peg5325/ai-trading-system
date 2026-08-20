from __future__ import annotations

import ast
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.run_dema_stoch_development import validate_review_evidence_url
from src.models import Candle
from src.research.dema_stoch.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    development_data_days,
    development_eval_days,
    fold_boundaries,
    load_contract,
)
from src.research.dema_stoch.development import (
    Candidate,
    Trade,
    allocate,
    candidates_in_segment,
    cross_side,
    trade_pnl,
)
from src.research.dema_stoch.indicators import dema, ema, stochastic_d
from src.research.dema_stoch.loading import complete_regular_5m

ET = ZoneInfo("America/New_York")


def _bar(open_: float, close: float, *, minute: int = 600) -> Candle:
    moment = datetime(2024, 8, 5, minute // 60, minute % 60, tzinfo=ET)
    return Candle.from_moment(
        symbol="SPY",
        interval="5m",
        moment=moment,
        o=open_,
        h=max(open_, close) + 1,
        low=min(open_, close) - 1,
        c=close,
        v=100,
        currency="USD",
        adjusted=True,
    )


def test_contract_is_frozen_and_blind_oos_is_not_in_development():
    contract = load_contract()
    data_days = development_data_days(contract)
    eval_days = development_eval_days(contract)
    assert len(data_days) == 690
    assert len(eval_days) == 670
    assert eval_days[-1] == date(2026, 1, 20)
    assert date.fromisoformat(contract["windows"]["blind_oos_start"]) > eval_days[-1]
    assert len(fold_boundaries(contract)) == 10
    assert PREREG_JSON_SHA256 != PREREG_MD_SHA256


def test_ema_and_dema_follow_declared_formula():
    values = [10.0, 12.0, 14.0]
    first = ema(values, 2)
    second = ema(first, 2)
    assert dema(values, 2) == pytest.approx(
        tuple(2 * one - two for one, two in zip(first, second))
    )
    assert dema([7.0] * 10, 60) == pytest.approx((7.0,) * 10)


def test_stochastic_d_is_raw_k60_smoothed_by_sma3():
    highs = [float(i + 2) for i in range(64)]
    lows = [float(i) for i in range(64)]
    closes = [float(i + 1) for i in range(64)]
    out = stochastic_d(highs, lows, closes, k_period=60, d_period=3)
    assert out[60] is None
    expected = []
    for index in (59, 60, 61):
        hi = max(highs[index - 59 : index + 1])
        lo = min(lows[index - 59 : index + 1])
        expected.append(100 * (closes[index] - lo) / (hi - lo))
    assert out[61] == pytest.approx(sum(expected) / 3)


def test_cross_side_requires_same_bar_dema_and_stochastic_confirmation():
    assert cross_side(
        _bar(9.5, 9.8),
        _bar(9.9, 10.5),
        previous_dema=10.0,
        current_dema=10.0,
        previous_stoch_d=49.0,
        current_stoch_d=51.0,
        centerline=50.0,
    ) == "LONG"
    assert cross_side(
        _bar(10.5, 10.2),
        _bar(10.1, 9.5),
        previous_dema=10.0,
        current_dema=10.0,
        previous_stoch_d=51.0,
        current_stoch_d=49.0,
        centerline=50.0,
    ) == "SHORT"
    assert cross_side(
        _bar(9.5, 9.8),
        _bar(9.9, 10.5),
        previous_dema=10.0,
        current_dema=10.0,
        previous_stoch_d=51.0,
        current_stoch_d=52.0,
        centerline=50.0,
    ) is None


def test_long_and_short_pnl_are_directionally_symmetric_before_cost():
    base = {
        "symbol": "SPY",
        "day": date(2024, 8, 5),
        "signal_ts": datetime(2024, 8, 5, 14, 0, tzinfo=ET),
        "entry_ts": datetime(2024, 8, 5, 14, 5, tzinfo=ET),
        "exit_ts": datetime(2024, 8, 5, 15, 5, tzinfo=ET),
        "qty": 100,
    }
    long = Trade(side="LONG", entry_price=100, exit_price=101, **base)
    short = Trade(side="SHORT", entry_price=101, exit_price=100, **base)
    assert trade_pnl(long, 0.0) == 100.0
    assert trade_pnl(short, 0.0) == 100.0
    assert trade_pnl(long, 0.34) < trade_pnl(long, 0.0)


def test_candidate_uses_next_open_and_twelfth_held_close(monkeypatch):
    bars = tuple(
        _bar(
            9.5 if index == 0 else 9.9 if index == 1 else 20.0 + index,
            9.8 if index == 0 else 10.5 if index == 1 else 20.5 + index,
            minute=570 + index * 5,
        )
        for index in range(14)
    )
    monkeypatch.setattr(
        "src.research.dema_stoch.development.dema",
        lambda values, period: (10.0,) * len(values),
    )
    monkeypatch.setattr(
        "src.research.dema_stoch.development.stochastic_d",
        lambda *args, **kwargs: (49.0, 51.0) + (51.0,) * 12,
    )
    candidate = candidates_in_segment(
        "SPY", bars, {date(2024, 8, 5)}, load_contract()
    )[0]
    assert candidate.signal_ts == bars[1].ts_utc + timedelta(minutes=5)
    assert candidate.entry_ts == bars[2].ts_utc
    assert candidate.entry_price == bars[2].open
    assert candidate.exit_ts == bars[13].ts_utc + timedelta(minutes=5)
    assert candidate.exit_price == bars[13].close


def _candidate(symbol: str, entry_minute: int, exit_minute: int) -> Candidate:
    day = date(2024, 8, 5)
    start = datetime(2024, 8, 5, 9, 30, tzinfo=ET)
    return Candidate(
        symbol=symbol,
        day=day,
        side="LONG",
        signal_ts=start + timedelta(minutes=entry_minute - 5),
        entry_ts=start + timedelta(minutes=entry_minute),
        exit_ts=start + timedelta(minutes=exit_minute),
        entry_price=100.0,
        exit_price=101.0,
        dema_distance_fraction=0.01,
        stochastic_distance=1.0,
    )


def test_allocator_keeps_slot_until_exit_bar_close_event():
    first_wave = tuple(_candidate(f"S{index}", 0, 60) for index in range(5))
    before_close = _candidate("EARLY", 55, 115)
    at_close = _candidate("AT_CLOSE", 60, 120)
    trades, skipped = allocate(
        first_wave + (before_close, at_close), load_contract()
    )
    assert "EARLY" not in {trade.symbol for trade in trades}
    assert "AT_CLOSE" in {trade.symbol for trade in trades}
    assert skipped["slot_cap"] == 1


def _regular_1m_day() -> tuple[Candle, ...]:
    start = datetime(2024, 8, 5, 9, 30, tzinfo=ET)
    candles = [
        Candle.from_moment(
            symbol="SPY",
            interval="1m",
            moment=start + timedelta(minutes=index),
            o=100 + index / 100,
            h=101 + index / 100,
            low=99 + index / 100,
            c=100.5 + index / 100,
            v=100,
            currency="USD",
            adjusted=True,
        )
        for index in range(390)
    ]
    candles.append(
        Candle.from_moment(
            symbol="SPY",
            interval="1m",
            moment=datetime(2024, 8, 5, 16, 0, tzinfo=ET),
            o=104,
            h=105,
            low=103,
            c=104,
            v=100,
            currency="USD",
            adjusted=True,
        )
    )
    return tuple(candles)


def test_incomplete_regular_grid_fails_closed_and_resets():
    candles = _regular_1m_day()
    bars, defects = complete_regular_5m(candles, date(2024, 8, 5), "SPY")
    assert len(bars) == 78
    assert defects == ()
    incomplete = candles[:100] + candles[101:]
    assert complete_regular_5m(
        incomplete, date(2024, 8, 5), "SPY"
    ) == ((), ("incomplete_regular_1m_grid",))


def test_complete_regular_day_does_not_require_a_post_session_frontier():
    regular_only = _regular_1m_day()[:-1]
    bars, defects = complete_regular_5m(
        regular_only, date(2024, 8, 5), "SPY"
    )
    assert len(bars) == 78
    assert defects == ()


def test_identical_duplicate_minute_is_deduplicated():
    candles = _regular_1m_day()
    duplicate = candles[100]
    bars, defects = complete_regular_5m(
        candles + (duplicate,), date(2024, 8, 5), "SPY"
    )
    assert len(bars) == 78
    assert defects == ()


def test_conflicting_duplicate_minute_fails_closed():
    candles = _regular_1m_day()
    original = candles[100]
    conflicting = Candle.from_moment(
        symbol=original.symbol,
        interval=original.interval,
        moment=original.ts_exchange,
        o=original.open,
        h=original.high + 1,
        low=original.low,
        c=original.close,
        v=original.volume,
        currency=original.currency,
        adjusted=original.adjusted,
    )
    bars, defects = complete_regular_5m(
        candles + (conflicting,), date(2024, 8, 5), "SPY"
    )
    assert bars == ()
    assert defects == ("duplicate_timestamp_value_conflict",)


def test_review_evidence_is_bound_to_stable_pr_129_comment():
    validate_review_evidence_url(
        "https://github.com/peg5325/ai-trading-system/pull/129"
        "#issuecomment-123456"
    )
    for invalid in (
        "https://github.com/peg5325/ai-trading-system/pull/129",
        "https://github.com/peg5325/ai-trading-system/pull/128#issuecomment-1",
        "https://github.com/peg5325/ai-trading-system/pull/129/files",
        "https://example.com/pull/129#issuecomment-1",
    ):
        with pytest.raises(ValueError):
            validate_review_evidence_url(invalid)


def test_performance_loader_is_the_only_candle_path():
    path = Path("src/research/dema_stoch/loading.py")
    text = path.read_text(encoding="utf-8")
    assert 'purpose="performance"' in text
    assert 'purpose="coverage"' not in text
    assert "src.history.loader" not in text


def test_official_runner_has_no_oos_unlock_or_direct_loader_import():
    path = Path("scripts/run_dema_stoch_development.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("oos_unlock" in name for name in imported)
    assert not any("history.loader" in name for name in imported)


def test_prereg_json_is_machine_readable_and_single_primary():
    contract = json.loads(
        Path(
            "docs/research/PREREG_P0_US30_DEMA_STOCH_MOMENTUM_V1.json"
        ).read_text(encoding="utf-8")
    )
    assert contract["discipline"]["primary_specs"] == 1
    assert contract["development_gate"]["primary"] == "combined-long-and-short"
    assert contract["discipline"]["blind_oos_opened"] is False
