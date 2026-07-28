from __future__ import annotations

from datetime import date

import pytest

from src.research.orb.contract import load_contract
from src.research.orb.execution import ExitFill, close_trade
from src.research.orb.portfolio import Position
from src.research.orb.stats import compute_stats, maximum_drawdown, profit_factor
from src.research.orb.windows import Fold, development_eval_days, folds
from tests.orb_test_helpers import bar


def _trade(day: date, symbol: str, entry: float, exit_price: float):
    entry_bar = bar(day, 590, symbol=symbol, o=entry, h=entry, low=entry, c=entry)
    exit_bar = bar(day, 595, symbol=symbol, o=exit_price, h=exit_price,
                   low=exit_price, c=exit_price)
    position = Position(
        symbol=symbol, day=day, spec_name="primary",
        entry_bar_ts_utc=entry_bar.ts_utc, entry_minute=590,
        entry_price=entry, qty=10, leverage=1, stop=entry - 1,
        target=entry + 2, one_r=1, rvol=2, gap=.01, rank=1,
        binding_cap="risk")
    fill = ExitFill(symbol, day, exit_bar.ts_utc, 595, exit_price, "target")
    return close_trade(load_contract(), position, fill)


def test_mdd_uses_running_peak_and_t0_initial_capital():
    assert maximum_drawdown([100, 120, 108, 110]) == pytest.approx(.10)
    assert maximum_drawdown([100, 90]) == pytest.approx(.10)


def test_profit_factor_is_infinite_without_losses():
    assert profit_factor([1, 2, 0]) == float("inf")
    assert profit_factor([6, -3]) == 2


def test_stats_curve_includes_t0_and_no_trade_days_and_folds_stay_continuous():
    days = [date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)]
    folds = (
        Fold("F01", days[0], days[1], 2),
        Fold("F02", days[2], days[2], 1),
    )
    trades = [_trade(days[0], "AAPL", 100, 102), _trade(days[2], "MSFT", 100, 99)]
    stats = compute_stats(
        trades=trades, evaluation_days=days, folds=folds,
        cost_level="official", contract=load_contract())
    assert len(stats.equity_curve) == 4
    assert stats.equity_curve[0] == 100_000
    assert stats.equity_curve[2] == stats.equity_curve[1]
    assert stats.fold_pnl["F01"] > 0
    assert stats.fold_pnl["F02"] < 0
    assert stats.positive_folds == 1
    assert stats.trading_days_with_trade == 2


def test_family_concentration_aggregates_qqq_family():
    day = date(2024, 3, 4)
    folds = (Fold("F01", day, day, 1),)
    stats = compute_stats(
        trades=[_trade(day, "QQQ", 100, 101), _trade(day, "TQQQ", 100, 101)],
        evaluation_days=[day], folds=folds, cost_level="official",
        contract=load_contract())
    assert set(stats.family_profit_share) == {"QQQ_FAMILY"}
    assert stats.family_profit_share["QQQ_FAMILY"] == 1


def test_frozen_development_curve_has_t0_plus_670_days():
    contract = load_contract()
    days = development_eval_days(contract)
    stats = compute_stats(
        trades=[], evaluation_days=days, folds=folds(contract),
        cost_level="official", contract=contract)
    assert len(days) == 670
    assert len(stats.equity_curve) == 671
    assert stats.positive_folds == 0
