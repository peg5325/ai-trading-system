from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import ET, SESSION_REGULAR, Candle
from src.research.box_orderblock_retest.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    load_contract,
    sha256_file,
)
from src.research.box_orderblock_retest.development import (
    BreakoutSignal,
    Candidate,
    Episode,
    ExitLeg,
    FeatureBar,
    breakout_signal,
    build_features,
    episode_net_pnl,
    prepare_arms,
    profit_factor_pass,
    simulate,
    summarize,
)
from src.research.box_orderblock_retest.loading import load_development_bars


def _candle(
    symbol: str,
    day: date,
    *,
    open_: float = 100.0,
    high: float = 102.0,
    low: float = 98.0,
    close: float = 100.0,
    volume: float = 1000.0,
) -> Candle:
    moment = datetime(day.year, day.month, day.day, tzinfo=ET)
    return Candle(
        symbol=symbol,
        interval="1d",
        ts_utc=moment.astimezone(timezone.utc),
        ts_exchange=moment,
        session=SESSION_REGULAR,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        currency="USD",
        adjusted=True,
        market="US",
    )


def _feature(
    candle: Candle,
    *,
    atr5: float = 1.0,
    atr20: float = 2.0,
    ema50: float = 95.0,
    sma200: float = 90.0,
    box_high: float = 100.0,
    box_low: float = 95.0,
    width: float = 0.05,
    median_volume: float = 1000.0,
) -> FeatureBar:
    return FeatureBar(
        day=candle.ts_exchange.date(),
        candle=candle,
        atr5_pre=atr5,
        atr20_pre=atr20,
        ema50=ema50,
        sma200=sma200,
        box_high=box_high,
        box_low=box_low,
        box_width_fraction=width,
        volume_median20=median_volume,
    )


def test_contract_hashes_and_primary_are_frozen() -> None:
    contract = load_contract()
    assert sha256_file(
        "docs/research/PREREG_P0_US30_BOX_ORDERBLOCK_RETEST_V1.json"
    ) == PREREG_JSON_SHA256
    assert sha256_file(
        "docs/research/PREREG_P0_US30_BOX_ORDERBLOCK_RETEST_V1.md"
    ) == PREREG_MD_SHA256
    assert contract["version"] == "1.2"
    assert contract["arms"]["primary"] == "order_block_retest_fixed5"
    assert contract["discipline"]["blind_oos_opened"] is False


def test_features_use_previous_fixed_windows_and_volume_does_not_reach_back() -> None:
    start = date(2020, 1, 1)
    calendar = tuple(start + timedelta(days=index) for index in range(220))
    bars = [
        _candle(
            "SPY",
            day,
            open_=100 + index / 100,
            high=102 + index / 100,
            low=98 + index / 100,
            close=101 + index / 100,
            volume=1000 + index,
        )
        for index, day in enumerate(calendar)
    ]
    features, defects = build_features(bars, calendar)
    current = features[calendar[219]]
    previous_20 = bars[199:219]
    assert current.box_high == max(item.high for item in previous_20)
    assert current.box_low == min(item.low for item in previous_20)
    assert current.volume_median20 == pytest.approx(
        sum((previous_20[9].volume, previous_20[10].volume)) / 2
    )
    assert current.ema50 is not None and current.sma200 is not None
    assert defects == {}

    broken = list(bars)
    broken[210] = _candle("SPY", calendar[210], volume=0)
    reset, _ = build_features(broken, calendar)
    assert reset[calendar[219]].volume_median20 is None
    assert reset[calendar[219]].ema50 is not None


def test_breakout_and_bullish_order_block_are_exact() -> None:
    contract = load_contract()
    previous_day, current_day = date(2024, 1, 2), date(2024, 1, 3)
    previous = _feature(
        _candle("A", previous_day, open_=100, high=101, low=97, close=98)
    )
    current = _feature(
        _candle(
            "A", current_day, open_=97.5, high=103, low=97, close=102, volume=1600
        )
    )
    spy = _feature(_candle("SPY", current_day, close=110), sma200=100)
    signal = breakout_signal(
        symbol="A", current=current, previous=previous, spy=spy, contract=contract
    )
    assert signal is not None
    assert signal.has_order_block is True
    assert signal.zone_low == 98
    assert signal.zone_high == 100
    too_extended = _feature(
        _candle("A", current_day, open_=97.5, high=104, low=97, close=103, volume=1600)
    )
    assert breakout_signal(
        symbol="A",
        current=too_extended,
        previous=previous,
        spy=spy,
        contract=contract,
    ) is None


def _candidate_features(days: tuple[date, ...]) -> dict[str, dict[date, FeatureBar]]:
    values = {
        day: _feature(_candle("A", day, open_=101, high=103, low=100, close=102))
        for day in days
    }
    return {"A": values}


def test_primary_uses_first_confirmed_retest_and_next_open() -> None:
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(10))
    features = _candidate_features(days)
    features["A"][days[1]] = _feature(
        _candle("A", days[1], open_=102, high=103, low=100.5, close=101.5)
    )
    features["A"][days[2]] = _feature(
        _candle("A", days[2], open_=100.5, high=102, low=99, close=101)
    )
    signal = BreakoutSignal(
        "A", days[0], 100, 95, 2, 1, 2, 0.05, 0.5, True, 98, 100
    )
    prepared = prepare_arms((signal,), features, days)
    primary = prepared["primary"].candidates[0]
    assert primary.confirmation_day == days[2]
    assert primary.entry_day == days[3]
    assert primary.planned_exit_day == days[7]
    assert primary.initial_stop == pytest.approx(97.8)


def test_pre_entry_close_below_zone_invalidates_without_data_exclusion() -> None:
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(10))
    features = _candidate_features(days)
    features["A"][days[1]] = _feature(
        _candle("A", days[1], open_=99, high=100, low=96, close=97)
    )
    signal = BreakoutSignal(
        "A", days[0], 100, 95, 2, 1, 2, 0.05, 0.5, True, 98, 100
    )
    prepared = prepare_arms((signal,), features, days)["primary"]
    assert prepared.candidates == ()
    assert prepared.data_exclusions == 0
    assert prepared.strategy_filters["pre_entry_invalidation"] == 1


def _candidate(days: tuple[date, ...], *, arm: str = "primary") -> Candidate:
    return Candidate(
        arm=arm,
        symbol="A",
        signal_day=days[0],
        confirmation_day=days[1],
        entry_day=days[2],
        planned_exit_day=days[6],
        initial_stop=95,
        breakout_atr=1,
        volume_ratio=2,
    )


def test_partial_same_bar_stop_and_target_is_full_stop_even_with_target_open() -> None:
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(8))
    features = _candidate_features(days)
    features["A"][days[2]] = _feature(
        _candle("A", days[2], open_=100, high=102, low=99, close=101)
    )
    features["A"][days[3]] = _feature(
        _candle("A", days[3], open_=106, high=108, low=94, close=101)
    )
    simulation = simulate(
        (_candidate(days),), features, days, arm="partial", contract=load_contract()
    )
    episode = simulation.episodes[0]
    assert len(episode.exits) == 1
    assert episode.exits[0].reason == "same_bar_stop_before_target"
    assert episode.exits[0].price == 95
    assert episode.exits[0].quantity == episode.quantity


def test_partial_target_on_time_exit_day_sells_half_target_then_half_close() -> None:
    days = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(8))
    features = _candidate_features(days)
    features["A"][days[2]] = _feature(
        _candle("A", days[2], open_=100, high=102, low=99, close=101)
    )
    features["A"][days[6]] = _feature(
        _candle("A", days[6], open_=103, high=106, low=99, close=104)
    )
    simulation = simulate(
        (_candidate(days),), features, days, arm="partial", contract=load_contract()
    )
    episode = simulation.episodes[0]
    assert [leg.reason for leg in episode.exits] == [
        "target_1r_half",
        "time_exit_after_target",
    ]
    assert episode.exits[0].price == 105
    assert episode.exits[0].quantity == episode.quantity * 0.5
    assert episode.exits[1].price == 104


def test_round_trip_cost_uses_actual_entry_and_partial_exit_notional() -> None:
    episode = Episode(
        arm="partial",
        symbol="A",
        signal_day=date(2024, 1, 1),
        confirmation_day=date(2024, 1, 2),
        entry_day=date(2024, 1, 3),
        planned_exit_day=date(2024, 1, 9),
        entry_price=100,
        initial_stop=95,
        quantity=3,
        initial_risk_cash=15,
        exits=(
            ExitLeg(date(2024, 1, 4), 105, 1.5, "target_1r_half"),
            ExitLeg(date(2024, 1, 9), 102, 1.5, "time_exit"),
        ),
    )
    gross = episode.gross_pnl
    expected_cost = (300 + 105 * 1.5 + 102 * 1.5) * 0.0034 / 2
    assert episode_net_pnl(episode, 0.34) == pytest.approx(gross - expected_cost)


def test_all_win_profit_factor_is_unbounded_and_passes_gate() -> None:
    days = (date(2024, 1, 2), date(2024, 1, 3))
    features = _candidate_features(days)
    episode = Episode(
        arm="primary",
        symbol="A",
        signal_day=days[0],
        confirmation_day=days[0],
        entry_day=days[0],
        planned_exit_day=days[1],
        entry_price=100,
        initial_stop=95,
        quantity=10,
        initial_risk_cash=50,
        exits=(ExitLeg(days[1], 105, 10, "time_exit"),),
    )
    metrics = summarize(
        (episode,), features, days, cost_pct_points=0, initial_capital=1000
    )
    assert metrics["profit_factor"] == "Infinity"
    assert metrics["max_consecutive_losses"] == 0
    assert metrics["holding_days_mean"] == 2
    assert metrics["entry_turnover_notional"] == 1000
    assert profit_factor_pass(metrics["profit_factor"], 1.2)
    assert not profit_factor_pass(None, 1.2)


def test_conflicting_duplicate_is_audited_instead_of_raising() -> None:
    day = date(2024, 1, 2)
    first = _candle("A", day, close=100)
    conflict = _candle("A", day, close=101, high=102)
    features, defects = build_features((first, conflict), (day,))
    assert features == {}
    assert defects["duplicate_value_conflict"] == 1


def test_loader_is_development_bounded_and_has_no_grant(monkeypatch) -> None:
    calls = []

    def fake_load(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(
        "src.research.box_orderblock_retest.loading.research_load", fake_load
    )
    load_development_bars(
        "ignored.sqlite",
        symbols=("SPY",),
        start=date(2016, 3, 18),
        end_inclusive=date(2025, 7, 18),
    )
    _, kwargs = calls[0]
    assert kwargs["end"].date() == date(2025, 7, 19)
    assert kwargs["purpose"] == "performance"
    assert kwargs["track"] == "swing"
    assert "execution_grant" not in kwargs and "unlock" not in kwargs


def test_official_runner_has_no_unlock_import_and_no_result_exists() -> None:
    path = Path("scripts/run_box_orderblock_retest_development.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("oos_unlock" in name for name in imports)
    assert not Path(
        "docs/research/RESULT_P0_US30_BOX_ORDERBLOCK_RETEST_DEVELOPMENT.json"
    ).exists()
