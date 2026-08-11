from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone

import pytest

from src.models import Candle, ET, SESSION_REGULAR
from src.research.sector_weekly_momentum.contract import canonical_json_bytes, load_contract
from src.research.sector_weekly_momentum.development import (
    Episode,
    FeatureBar,
    PreparedTargets,
    Simulation,
    TargetEvent,
    build_features,
    chronological_blocks,
    equal_all_events,
    prepare_targets,
    prepared_audit,
    simulate,
    summarize,
    weekly_signal_trade_pairs,
)
from src.research.sector_weekly_momentum.loading import (
    load_development_bars,
    sector_symbols,
)
from src.research.sector_weekly_momentum.report import render_markdown


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
    symbol: str,
    day: date,
    *,
    open_: float = 100.0,
    close: float = 100.0,
    sma: float | None = 90.0,
    short: float | None = 0.2,
    long: float | None = 0.1,
) -> FeatureBar:
    return FeatureBar(
        day=day,
        candle=_candle(symbol, day, open_=open_, close=close),
        sma200=sma,
        ret63_skip5=short,
        ret126_skip5=long,
    )


def _contract():
    return load_contract()


def test_contract_and_exact_sector_universe_are_locked():
    contract = _contract()
    symbols = sector_symbols(contract["data"]["universe_path"])
    assert contract["strategy_id"] == "p0_sector_etf_weekly_momentum_v1"
    assert list(symbols) == contract["data"]["expected_symbols"]
    assert len(symbols) == 8


def test_skipped_returns_and_sma_match_hand_calculation():
    start = date(2023, 1, 1)
    bars = [
        _candle("XLE", start + timedelta(days=index), close=100 + index)
        for index in range(201)
    ]
    features = list(build_features(bars).values())
    assert features[62].ret63_skip5 is None
    assert features[63].ret63_skip5 == pytest.approx(158 / 100 - 1)
    assert features[126].ret126_skip5 == pytest.approx(221 / 100 - 1)
    assert features[198].sma200 is None
    assert features[199].sma200 == pytest.approx(sum(range(100, 300)) / 200)


def test_iso_week_uses_last_actual_trading_day_across_holiday():
    calendar = (
        date(2024, 5, 20), date(2024, 5, 21), date(2024, 5, 22),
        date(2024, 5, 23), date(2024, 5, 24),
        date(2024, 5, 28), date(2024, 5, 29), date(2024, 5, 30),
        date(2024, 5, 31), date(2024, 6, 3),
    )
    assert weekly_signal_trade_pairs(calendar) == (
        (date(2024, 5, 24), date(2024, 5, 28)),
        (date(2024, 5, 31), date(2024, 6, 3)),
    )


def _target_fixture(*, spy_close: float = 100.0, spy_sma: float = 90.0):
    signal_day = date(2024, 5, 24)
    trade_day = date(2024, 5, 28)
    symbols = tuple(_contract()["data"]["expected_symbols"])
    scores = {symbol: 0.01 for symbol in symbols}
    scores["XLE"] = 0.4
    scores["XLF"] = 0.4
    features = {
        symbol: {
            signal_day: _feature(
                symbol, signal_day, short=scores[symbol], long=scores[symbol]
            ),
            trade_day: _feature(symbol, trade_day),
        }
        for symbol in symbols
    }
    spy = {
        signal_day: _feature("SPY", signal_day, close=spy_close, sma=spy_sma),
        trade_day: _feature("SPY", trade_day),
    }
    return symbols, features, spy, (signal_day, trade_day)


def test_top_two_tie_break_and_next_open_are_locked():
    symbols, features, spy, calendar = _target_fixture()
    prepared = prepare_targets(
        dict(reversed(list(features.items()))),
        spy,
        calendar,
        symbols=symbols,
        mode="primary",
        contract=_contract(),
    )
    assert prepared.valid_weeks == 1
    assert prepared.events[0].targets == ("XLE", "XLF")
    assert prepared.events[0].signal_day == calendar[0]
    assert prepared.events[0].trade_day == calendar[1]


@pytest.mark.parametrize(
    ("spy_close", "spy_sma", "expected"),
    [(100.0, 99.99, True), (100.0, 100.0, False), (99.99, 100.0, False)],
)
def test_spy_sma_regime_is_strict(
    spy_close: float, spy_sma: float, expected: bool
):
    symbols, features, spy, calendar = _target_fixture(
        spy_close=spy_close, spy_sma=spy_sma
    )
    prepared = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="primary", contract=_contract(),
    )
    assert prepared.events[0].regime_on is expected
    assert bool(prepared.events[0].targets) is expected


def test_absolute_score_zero_is_ineligible():
    symbols, features, spy, calendar = _target_fixture()
    for symbol in symbols:
        features[symbol][calendar[0]] = _feature(
            symbol, calendar[0], short=0.0, long=0.0
        )
    prepared = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="primary", contract=_contract(),
    )
    assert prepared.events[0].targets == ()
    assert prepared.cash_weeks == 1


def test_primary_and_sensitivity_scores_are_independent_locked_specs():
    symbols, features, spy, calendar = _target_fixture()
    features["XLE"][calendar[0]] = _feature(
        "XLE", calendar[0], short=0.9, long=0.01
    )
    features["XLF"][calendar[0]] = _feature(
        "XLF", calendar[0], short=0.01, long=0.8
    )
    primary = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="primary", contract=_contract(),
    )
    sensitivity = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="sensitivity", contract=_contract(),
    )
    assert primary.events[0].targets[0] == "XLE"
    assert sensitivity.events[0].targets[0] == "XLF"


def _flat_features(symbols: tuple[str, ...], calendar: tuple[date, ...]):
    return {
        symbol: {day: _feature(symbol, day) for day in calendar}
        for symbol in symbols
    }


def test_retained_positions_have_zero_extra_turnover_and_cost():
    symbols = ("XLE", "XLF", "XLI")
    calendar = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(15))
    events = (
        TargetEvent(calendar[0], calendar[1], ("XLE", "XLF"), True),
        TargetEvent(calendar[5], calendar[6], ("XLE", "XLF"), True),
        TargetEvent(calendar[10], calendar[11], ("XLF", "XLI"), True),
    )
    simulation = simulate(
        _flat_features(symbols, calendar),
        calendar,
        events,
        entry_notional=25_000,
        round_trip_cost_pct_points=0.34,
        initial_capital=100_000,
    )
    assert simulation.buy_count == 3
    assert simulation.sell_count == 3
    assert simulation.retained_count == 3
    assert simulation.turnover_notional == pytest.approx(150_000)
    assert simulation.total_cost == pytest.approx(255)
    assert sum(episode.net_pnl for episode in simulation.episodes) == pytest.approx(-255)


def test_cost_levels_preserve_identical_gross_episode_path():
    symbols = ("XLE", "XLF")
    calendar = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(8))
    events = (TargetEvent(calendar[0], calendar[1], symbols, True),)
    features = _flat_features(symbols, calendar)
    low = simulate(
        features, calendar, events,
        entry_notional=25_000,
        round_trip_cost_pct_points=0.14,
        initial_capital=100_000,
    )
    official = simulate(
        features, calendar, events,
        entry_notional=25_000,
        round_trip_cost_pct_points=0.34,
        initial_capital=100_000,
    )
    low_path = [
        (item.symbol, item.entry_day, item.exit_day, item.entry_price, item.exit_price,
         item.shares, item.gross_pnl)
        for item in low.episodes
    ]
    official_path = [
        (item.symbol, item.entry_day, item.exit_day, item.entry_price, item.exit_price,
         item.shares, item.gross_pnl)
        for item in official.episodes
    ]
    assert low_path == official_path
    assert official.total_cost > low.total_cost


def test_equal_all_uses_same_fifty_thousand_total_entry_exposure():
    symbols, features, spy, calendar = _target_fixture()
    prepared = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="primary", contract=_contract(),
    )
    events = equal_all_events(prepared, symbols)
    simulation = simulate(
        features,
        calendar,
        events,
        entry_notional=6_250,
        round_trip_cost_pct_points=0,
        initial_capital=100_000,
    )
    assert simulation.buy_count == 8
    assert sum(episode.entry_notional for episode in simulation.episodes) == 50_000


def test_regime_off_sells_before_remaining_in_cash():
    symbols = ("XLE", "XLF")
    calendar = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(8))
    events = (
        TargetEvent(calendar[0], calendar[1], symbols, True),
        TargetEvent(calendar[5], calendar[6], (), False),
    )
    simulation = simulate(
        _flat_features(symbols, calendar), calendar, events,
        entry_notional=25_000,
        round_trip_cost_pct_points=0,
        initial_capital=100_000,
    )
    assert simulation.sell_count == 2
    assert {episode.exit_reason for episode in simulation.episodes} == {"rebalance"}


def test_development_boundary_liquidates_at_close():
    symbol = "XLE"
    d0, d1 = date(2024, 1, 5), date(2024, 1, 8)
    features = {
        symbol: {
            d0: _feature(symbol, d0),
            d1: _feature(symbol, d1, open_=100, close=110),
        }
    }
    simulation = simulate(
        features, (d0, d1), (TargetEvent(d0, d1, (symbol,), True),),
        entry_notional=25_000,
        round_trip_cost_pct_points=0,
        initial_capital=100_000,
    )
    episode = simulation.episodes[0]
    assert episode.exit_reason == "boundary"
    assert episode.exit_price == 110
    assert episode.gross_pnl == pytest.approx(2_500)


def test_summary_uses_mark_to_market_drawdown_and_symbol_contribution():
    curve = (
        {"day": "2024-01-02", "equity": 100_000.0},
        {"day": "2024-01-03", "equity": 90_000.0},
        {"day": "2024-01-04", "equity": 105_000.0},
    )
    episodes = (
        Episode("XLE", "2024-01-01", "2024-01-02", "2024-01-04", 100, 120,
                100, 10_000, 12_000, 0, 0, 2_000, 2_000, 3, "boundary"),
        Episode("XLF", "2024-01-01", "2024-01-02", "2024-01-04", 100, 130,
                100, 10_000, 13_000, 0, 0, 3_000, 3_000, 3, "boundary"),
    )
    metrics = summarize(
        Simulation(episodes, curve, 2, 2, 0, 45_000, 0),
        initial_capital=100_000,
    )
    assert metrics["max_drawdown_pct"] == pytest.approx(-10)
    assert metrics["max_symbol_contribution"] == pytest.approx(0.6)
    assert metrics["holding_days_median"] == 3


def test_chronological_blocks_cover_calendar_once():
    calendar = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(23))
    blocks = chronological_blocks(calendar, 10)
    assert tuple(day for block in blocks for day in block) == calendar
    assert [len(block) for block in blocks] == [3, 3, 3, 2, 2, 2, 2, 2, 2, 2]


def test_invalid_week_rate_boundary_is_inclusive():
    prepared = PreparedTargets((), 21, 0, 19, 1, 1, 0, 0)
    assert prepared.invalid_week_rate == pytest.approx(0.05)
    assert prepared_audit(prepared)["invalid_week_rate"] == pytest.approx(0.05)


def test_missing_trade_bar_is_fail_closed_after_warmup():
    symbols, features, spy, calendar = _target_fixture()
    del features[symbols[0]][calendar[1]]
    prepared = prepare_targets(
        features, spy, calendar,
        symbols=symbols, mode="primary", contract=_contract(),
    )
    assert prepared.valid_weeks == 0
    assert prepared.invalid_weeks == 1
    assert prepared.events == ()


def test_development_loader_uses_swing_half_open_range_without_grant(monkeypatch):
    calls = []

    def fake_research_load(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "src.research.sector_weekly_momentum.loading.research_load",
        fake_research_load,
    )
    load_development_bars(
        "ignored.sqlite",
        symbols=("XLE",),
        calendar_symbol="SPY",
        start=date(2016, 3, 18),
        end_inclusive=date(2025, 7, 18),
    )
    assert len(calls) == 2
    for _, kwargs in calls:
        assert kwargs["start"].date() == date(2016, 3, 18)
        assert kwargs["end"].date() == date(2025, 7, 19)
        assert kwargs["purpose"] == "performance"
        assert kwargs["track"] == "swing"
        assert "execution_grant" not in kwargs
        assert "unlock" not in kwargs
    assert "OOSUnlock" not in inspect.getsource(load_development_bars)


def test_json_and_markdown_are_deterministic():
    metrics = {
        "episode_count": 0,
        "total_net_pnl": 0,
        "return_on_initial_pct": 0,
        "net_expectancy": 0,
        "profit_factor": None,
        "win_rate": 0,
        "max_drawdown_pct": 0,
        "max_consecutive_losses": 0,
        "max_symbol_contribution": None,
        "turnover_notional": 0,
        "total_cost": 0,
        "buy_count": 0,
        "sell_count": 0,
        "retained_count": 0,
        "holding_days_mean": 0,
        "holding_days_median": 0,
        "net_pnl_by_symbol": {},
        "net_pnl_by_entry_week": {},
        "daily_equity": [],
        "final_equity": 100_000,
    }
    portfolio = {
        "metrics": {level: metrics for level in ("gross", "low", "official", "stress")},
        "episode_digest_sha256": "0" * 64,
        "official_cost_drag": 0,
        "official_cost_drag_ratio_to_gross_pnl": None,
    }
    result = {
        "verdict": "NO_SELECTION",
        "strategy_id": "x",
        "contract_sha256": "a",
        "data_content_sha256": "b",
        "development_window": {"start": "x", "end_inclusive": "y", "calendar_days": 2},
        "symbols": ["XLE"],
        "blind_oos_opened": False,
        "primary": portfolio,
        "sensitivity": portfolio,
        "equal_all": portfolio,
        "signal_week_audit": {
            name: {
                "scheduled_weeks": 0, "warmup_weeks": 0, "valid_weeks": 0,
                "invalid_weeks": 0, "invalid_week_rate": 0, "boundary_weeks": 0,
                "regime_on_weeks": 0, "cash_weeks": 0,
            }
            for name in ("primary", "sensitivity")
        },
        "data_gates": {"DQ": True},
        "economic_gates": {"D": False},
        "folds_official": [],
        "spy_buy_hold_reference_gross_pnl_50k": 0,
    }
    assert canonical_json_bytes(result) == canonical_json_bytes(result)
    assert render_markdown(result) == render_markdown(result)
