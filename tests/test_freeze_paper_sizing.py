from __future__ import annotations

import random

from src.freeze_paper.sizing import EXPOSURE_ROOM, cash_per_share, size_entries
from src.risk.models import BUY, NEW_ENTRY, OrderIntent, PortfolioSnapshot, Position
from src.risk.risk_manager import RiskManager
from tests.freeze_paper_test_helpers import BINDING


def _eligible(opens, symbols=None):
    symbols = symbols or [f"S{i}" for i in range(len(opens))]
    return [(sym, i, o) for i, (sym, o) in enumerate(zip(symbols, opens))]


def test_observed_2026_09_29_example_sizes_1_0_2():
    sized, skipped = size_entries(2000.0, _eligible([725.93, 1075.0, 356.99],
                                                    ["META", "MU", "AVGO"]), BINDING)
    assert [(e.symbol, e.quantity) for e in sized] == [("META", 1), ("AVGO", 2)]
    assert [(s.symbol, s.rank, s.reason) for s in skipped] == [("MU", 1, EXPOSURE_ROOM)]


def test_cash_per_share_includes_entry_slippage_and_commission():
    assert cash_per_share(100.0, BINDING) == 100.0 * 1.0005 * 1.001


def _risk_accepts_all(equity, sized, symbols_leverage=None):
    """Replays the engine's risk inputs: price=reference_open, account_value=E,
    earlier same-bar entries marked at their reference_open, leverage 1."""
    rm = RiskManager(BINDING.risk_config)
    positions = {}
    cash = equity
    for entry in sized:
        intent = OrderIntent(entry.symbol, NEW_ENTRY, BUY, float(entry.quantity),
                             entry.reference_open, leverage_factor=BINDING.risk_leverage_factor)
        decision = rm.evaluate(intent, PortfolioSnapshot(
            account_value=equity, positions=dict(positions), cash=cash))
        if not decision.allowed or decision.adjusted_quantity < entry.quantity - 1e-9:
            return False, decision
        positions[entry.symbol] = Position(entry.symbol, entry.quantity, entry.entry_fill_price,
                                           entry.reference_open, leverage_factor=1)
        cash -= entry.quantity * entry.cash_per_share
    return True, None


def test_randomized_sizing_stays_under_cap_and_risk_never_rejects():
    rng = random.Random(165)
    pool = ["TQQQ", "SOXL", "SQQQ", "SOXS", "AAPL", "META", "MU"]
    for _ in range(3000):
        equity = rng.uniform(1500, 2500)
        n = rng.randint(1, 3)
        symbols = rng.sample(pool, n)
        opens = [rng.uniform(5, 2500) for _ in range(n)]
        sized, skipped = size_entries(equity, _eligible(opens, symbols), BINDING)
        used = sum(e.quantity * e.cash_per_share for e in sized)
        assert used <= BINDING.max_exposure_pct * equity + 1e-9
        assert all(e.quantity >= 1 and isinstance(e.quantity, int) for e in sized)
        assert all(s.reason == EXPOSURE_ROOM for s in skipped)
        ok, decision = _risk_accepts_all(equity, sized)
        assert ok, (equity, symbols, opens, decision)


def test_three_x_etf_uses_price_notional_so_risk_accepts_full_room():
    sized, _ = size_entries(2000.0, _eligible([50.0], ["TQQQ"]), BINDING)
    assert sized[0].quantity == 29          # 1500 / (50 * 1.0005 * 1.001)
    ok, _ = _risk_accepts_all(2000.0, sized)
    assert ok
    # With the screener's 3x factor the same entry would be cut — the reason for the binding.
    rm = RiskManager(BINDING.risk_config)
    decision = rm.evaluate(OrderIntent("TQQQ", NEW_ENTRY, BUY, 29.0, 50.0, leverage_factor=3),
                           PortfolioSnapshot(account_value=2000.0, cash=2000.0))
    assert decision.adjusted_quantity < 29
