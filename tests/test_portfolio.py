"""자금관리(Portfolio) 테스트: PortfolioAccount, RiskManager 자금관리 규칙,
performance_summary, PortfolioSnapshot.cash 필드."""
from __future__ import annotations

from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.portfolio.performance import performance_summary
from src.risk.models import BUY, NEW_ENTRY, SELL, OrderIntent, PortfolioSnapshot, Position
from src.risk.risk_manager import (
    RC_INSUFFICIENT_CASH,
    RC_MAX_POSITIONS,
    RC_MAX_SYMBOL_NOTIONAL,
    RC_QTY_REDUCED,
    RiskConfig,
    RiskManager,
)


# ---- PortfolioAccount ----
def test_initial_cash_equals_initial_capital():
    a = PortfolioAccount(3_000_000)
    assert a.cash == 3_000_000
    assert a.initial_capital == 3_000_000


def test_apply_buy_reduces_cash():
    a = PortfolioAccount(3_000_000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 30.0, fee=0.06, ref_price=30.0))
    assert a.cash == 3_000_000 - (10 * 30.0 + 0.06)


def test_apply_sell_increases_cash():
    a = PortfolioAccount(3_000_000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 30.0, fee=0.06, ref_price=30.0))
    cash_after_buy = a.cash
    a.apply_fill(FillEvent("SOXL", SELL, 10, 32.0, fee=0.06, ref_price=32.0))
    assert a.cash == cash_after_buy + (10 * 32.0 - 0.06)


def test_positions_aggregation_weighted_avg():
    a = PortfolioAccount(3_000_000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 30.0, ref_price=30.0))
    a.apply_fill(FillEvent("SOXL", BUY, 10, 32.0, ref_price=32.0))
    pos = a.positions()
    assert pos["SOXL"]["quantity"] == 20
    assert pos["SOXL"]["avg_price"] == (10 * 30.0 + 10 * 32.0) / 20
    assert a.open_symbol_count() == 1


def test_market_value_unrealized_and_portfolio_value():
    a = PortfolioAccount(3_000_000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 30.0, fee=0.0, ref_price=30.0))
    marks = {"SOXL": 31.0}
    assert a.market_value(marks) == 10 * 31.0
    assert a.unrealized_pnl(marks) == (31.0 - 30.0) * 10
    pv = a.portfolio_value(marks)
    assert pv.cash == a.cash
    assert pv.market_value == 310.0
    assert pv.unrealized_pnl == 10.0
    assert pv.realized_pnl == 0.0
    assert pv.total_equity == a.cash + 310.0
    d = pv.to_dict()
    assert d["total_equity"] == round(a.cash + 310.0, 6)


def test_realized_pnl_after_round_trip():
    a = PortfolioAccount(3_000_000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 30.0, fee=0.0, ref_price=30.0))
    a.apply_fill(FillEvent("SOXL", SELL, 10, 32.0, fee=0.0, ref_price=32.0))
    assert a.realized_pnl() == (32.0 - 30.0) * 10
    assert len(a.realized_trades()) == 1
    assert a.positions() == {}
    assert a.open_symbol_count() == 0


def test_can_afford():
    a = PortfolioAccount(1000)
    assert a.can_afford(1000)
    assert a.can_afford(999.999999)
    assert not a.can_afford(1000.01)


def test_mark_equity_updates_peak_drawdown_daily_return_and_curve():
    a = PortfolioAccount(1000)
    a.apply_fill(FillEvent("SOXL", BUY, 10, 100.0, fee=0.0, ref_price=100.0))  # cash=0, pos qty=10@100
    eq1 = a.mark_equity({"SOXL": 100.0})
    assert eq1 == 1000.0
    assert a.peak_equity == 1000.0
    assert a.last_drawdown_pct == 0.0
    assert a.last_daily_return_pct == 0.0

    eq2 = a.mark_equity({"SOXL": 90.0})   # equity drops to 900
    assert eq2 == 900.0
    assert a.peak_equity == 1000.0
    assert a.last_drawdown_pct == (900.0 - 1000.0) / 1000.0
    assert a.last_daily_return_pct == (900.0 - 1000.0) / 1000.0

    eq3 = a.mark_equity({"SOXL": 110.0})  # equity rises to 1100 (new peak)
    assert eq3 == 1100.0
    assert a.peak_equity == 1100.0
    assert a.last_drawdown_pct == 0.0

    assert a.equity_curve == [1000.0, 900.0, 1100.0]


def test_from_fills_rebuild_equals_sequential_applies():
    fills = [
        FillEvent("SOXL", BUY, 10, 30.0, fee=0.06, ref_price=30.0),
        FillEvent("SOXL", BUY, 5, 31.0, fee=0.03, ref_price=31.0),
        FillEvent("SOXL", SELL, 8, 33.0, fee=0.05, ref_price=33.0),
    ]
    sequential = PortfolioAccount(3_000_000)
    for f in fills:
        sequential.apply_fill(f)

    rebuilt = PortfolioAccount.from_fills(3_000_000, fills)

    assert rebuilt.cash == sequential.cash
    assert rebuilt.positions() == sequential.positions()
    assert rebuilt.realized_pnl() == sequential.realized_pnl()


# ---- RiskManager: 자금관리 규칙 ----
def _snap(**kw) -> PortfolioSnapshot:
    base = dict(account_value=100_000.0, positions={})
    base.update(kw)
    return PortfolioSnapshot(**base)


def test_max_positions_blocks_third_distinct_entry():
    rm = RiskManager(RiskConfig(max_positions=2))
    positions = {
        "AAA": Position("AAA", 10.0, 100.0, 100.0),
        "BBB": Position("BBB", 10.0, 100.0, 100.0),
    }
    intent = OrderIntent("CCC", NEW_ENTRY, BUY, 1.0, 100.0)
    d = rm.evaluate(intent, _snap(positions=positions))
    assert not d.allowed
    assert RC_MAX_POSITIONS in d.reason_codes


def test_max_positions_allows_when_under_limit():
    rm = RiskManager(RiskConfig(max_positions=2))
    positions = {"AAA": Position("AAA", 10.0, 100.0, 100.0)}
    intent = OrderIntent("BBB", NEW_ENTRY, BUY, 1.0, 100.0)
    d = rm.evaluate(intent, _snap(positions=positions))
    assert d.allowed


def test_max_positions_does_not_block_add_to_existing_symbol():
    rm = RiskManager(RiskConfig(max_positions=2))
    positions = {
        "AAA": Position("AAA", 10.0, 100.0, 100.0),
        "BBB": Position("BBB", 10.0, 100.0, 100.0),
    }
    # NEW_ENTRY on a symbol that's already held is blocked by RC_POSITION_ALREADY_OPEN,
    # not by RC_MAX_POSITIONS — confirm max_positions rule only targets brand-new symbols.
    intent = OrderIntent("AAA", NEW_ENTRY, BUY, 1.0, 100.0)
    d = rm.evaluate(intent, _snap(positions=positions))
    assert not d.allowed
    assert RC_MAX_POSITIONS not in d.reason_codes


def test_per_symbol_20pct_cap_still_enforced():
    rm = RiskManager()   # default max_symbol_notional_pct=0.20
    # account 100_000 * 0.20 = 20_000 cap; request 40_000 notional (>20%) -> reduced
    intent = OrderIntent("AAPL", NEW_ENTRY, BUY, 200.0, 200.0)
    d = rm.evaluate(intent, _snap())
    assert d.allowed
    assert d.adjusted_quantity == 100.0
    assert RC_QTY_REDUCED in d.reason_codes
    assert RC_MAX_SYMBOL_NOTIONAL in d.reason_codes


def test_insufficient_cash_blocks_entry():
    rm = RiskManager()
    intent = OrderIntent("AAPL", NEW_ENTRY, BUY, 10.0, 100.0)   # notional=1000
    d = rm.evaluate(intent, _snap(cash=500.0))
    assert not d.allowed
    assert RC_INSUFFICIENT_CASH in d.reason_codes


def test_cash_none_means_no_cash_check():
    rm = RiskManager()
    intent = OrderIntent("AAPL", NEW_ENTRY, BUY, 10.0, 100.0)
    d = rm.evaluate(intent, _snap(cash=None))
    assert d.allowed


# ---- performance_summary ----
def test_performance_summary_basic_metrics():
    class FakeTrade:
        def __init__(self, net_pnl):
            self.net_pnl = net_pnl

    trades = [FakeTrade(100.0), FakeTrade(-50.0), FakeTrade(30.0)]
    equity_curve = [100_000.0, 100_100.0, 100_050.0, 100_080.0]
    perf = performance_summary(100_000.0, equity_curve, trades)

    assert perf["initial_capital"] == 100_000.0
    assert perf["final_equity"] == 100_080.0
    assert perf["total_return_pct"] == round((100_080.0 - 100_000.0) / 100_000.0 * 100, 4)
    assert perf["total_trades"] == 3
    assert perf["win_rate"] == round(2 / 3, 4)
    assert perf["profit_factor"] == round(130.0 / 50.0, 4)
    assert perf["expectancy"] == round((100.0 - 50.0 + 30.0) / 3, 6)
    assert perf["max_drawdown_pct"] < 0


def test_performance_summary_sharpe_hand_check():
    equity_curve = [100, 110, 105, 120]
    perf = performance_summary(100, equity_curve, [])
    r1 = (110 - 100) / 100
    r2 = (105 - 110) / 110
    r3 = (120 - 105) / 105
    mean = (r1 + r2 + r3) / 3
    var = ((r1 - mean) ** 2 + (r2 - mean) ** 2 + (r3 - mean) ** 2) / 3
    std = var ** 0.5
    expected = round(mean / std, 4)
    assert isinstance(perf["sharpe_ratio"], float)
    assert perf["sharpe_ratio"] == expected


def test_performance_summary_sharpe_none_for_short_or_constant_curve():
    assert performance_summary(100, [100], [])["sharpe_ratio"] is None
    assert performance_summary(100, [], [])["sharpe_ratio"] is None
    assert performance_summary(100, [100, 100, 100], [])["sharpe_ratio"] is None


def test_performance_summary_no_trades_defaults():
    perf = performance_summary(100_000.0, [], [])
    assert perf["total_trades"] == 0
    assert perf["win_rate"] == 0.0
    assert perf["profit_factor"] is None
    assert perf["max_consecutive_losses"] == 0


# ---- Journal 스냅샷: cash/equity/drawdown ----
def test_portfolio_snapshot_to_dict_includes_cash():
    snap = PortfolioSnapshot(account_value=100_000.0, cash=50_000.0, drawdown_pct=-0.02,
                             daily_pnl_pct=0.01)
    d = snap.to_dict()
    assert d["cash"] == 50_000.0
    assert d["drawdown_pct"] == -0.02
    assert d["daily_pnl_pct"] == 0.01


def test_portfolio_snapshot_cash_defaults_to_none():
    snap = PortfolioSnapshot(account_value=100_000.0)
    assert snap.cash is None
    assert snap.to_dict()["cash"] is None
