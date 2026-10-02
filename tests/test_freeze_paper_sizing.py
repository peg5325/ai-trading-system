from __future__ import annotations

import random
from decimal import Decimal

from src.freeze_paper.sizing import (
    BELOW_MIN_ORDER_NOTIONAL,
    EXPOSURE_ROOM,
    cash_per_share,
    entry_fill_price,
    size_entries,
)
from src.risk.models import BUY, NEW_ENTRY, OrderIntent, PortfolioSnapshot, Position
from src.risk.risk_manager import RiskManager
from tests.freeze_paper_test_helpers import BINDING


def _eligible(opens, symbols=None):
    symbols = symbols or [f"S{i}" for i in range(len(opens))]
    return [(sym, i, o) for i, (sym, o) in enumerate(zip(symbols, opens))]


def test_observed_2026_09_29_example_now_sizes_every_candidate_fractionally():
    sized, skipped = size_entries(2000.0, _eligible([725.93, 1075.0, 356.99],
                                                    ["META", "MU", "AVGO"]), BINDING)
    assert [(e.symbol, e.quantity) for e in sized] == [
        ("META", 0.687739), ("MU", 0.464419), ("AVGO", 1.398503)]
    assert skipped == []


def test_observed_2026_10_01_opens_deploy_the_full_75_percent():
    # v1.2 whole shares: MSFT 1, AMZN 1, SPY skipped -> about 38% of equity deployed.
    sized, skipped = size_entries(2000.0, _eligible([517.56, 250.895, 764.0],
                                                    ["MSFT", "AMZN", "SPY"]), BINDING)
    assert skipped == []
    assert [(e.symbol, e.quantity) for e in sized] == [
        ("MSFT", 0.964624), ("AMZN", 1.989879), ("SPY", 0.65347)]
    used = sum(e.quantity * e.cash_per_share for e in sized)
    assert 1499.99 < used <= 1500.0
    for e in sized:                      # each candidate gets about room / k = 500
        assert 499.99 < e.quantity * e.cash_per_share < 500.01


def test_quantity_is_floored_to_6_decimals():
    (entry,), _ = size_entries(2000.0, _eligible([100.0]), BINDING)
    # 1500 / 100.150050... = 14.97752633...
    assert entry.quantity == 14.977526
    assert Decimal(str(entry.quantity)).as_tuple().exponent >= -6


def test_quantity_is_the_largest_6dp_amount_inside_the_budget_without_float_drift():
    rng = random.Random(1003)
    step = Decimal("0.000001")
    for _ in range(3000):
        equity = round(rng.uniform(1500, 2500), rng.randint(0, 6))
        opens = [round(rng.uniform(1, 2500), rng.randint(0, 4)) for _ in range(rng.randint(1, 3))]
        sized, _ = size_entries(equity, _eligible(opens), BINDING)
        cap = Decimal(str(BINDING.max_exposure_pct)) * Decimal(str(equity))
        used = Decimal(0)
        for index, entry in enumerate(sized):
            qty = Decimal(str(entry.quantity))
            assert qty == qty.quantize(step)
            per_share = Decimal(str(entry.cash_per_share))
            budget = (cap - used) / (len(opens) - index)
            assert qty * per_share <= budget
            assert (qty + step) * per_share > budget
            used += qty * per_share
        assert used <= cap


def test_below_usd_1_order_is_skipped_and_its_room_passes_to_later_ranks():
    # cap = 1.5: rank 0 budget 0.75 -> notional < USD 1 -> skipped; rank 1 budget 1.5.
    sized, skipped = size_entries(2.0, _eligible([725.93, 1075.0], ["META", "MU"]), BINDING)
    assert [(s.symbol, s.reason) for s in skipped] == [("META", BELOW_MIN_ORDER_NOTIONAL)]
    (mu,) = sized
    assert mu.quantity == 0.001393
    assert mu.quantity * entry_fill_price(1075.0, BINDING) >= BINDING.min_order_notional_usd


def test_zero_room_is_exposure_room():
    sized, skipped = size_entries(0.0, _eligible([10.0]), BINDING)
    assert sized == [] and [s.reason for s in skipped] == [EXPOSURE_ROOM]


def test_rank_order_budget_is_room_over_remaining_candidates():
    sized, _ = size_entries(2000.0, _eligible([100.0, 200.0, 300.0]), BINDING)
    cap = 1500.0
    used = 0.0
    for index, entry in enumerate(sized):
        budget = (cap - used) / (3 - index)
        spent = entry.quantity * entry.cash_per_share
        assert budget - entry.cash_per_share * 1e-6 <= spent <= budget + 1e-9
        used += spent


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
        assert all(e.quantity > 0 and e.quantity == round(e.quantity, 6) for e in sized)
        assert skipped == []                 # budgets >= 375 USD never fall below USD 1
        ok, decision = _risk_accepts_all(equity, sized)
        assert ok, (equity, symbols, opens, decision)


def test_three_x_etf_uses_price_notional_so_risk_accepts_full_room():
    sized, _ = size_entries(2000.0, _eligible([50.0], ["TQQQ"]), BINDING)
    assert sized[0].quantity == 29.955052   # floor6(1500 / (50 * 1.0005 * 1.001))
    ok, _ = _risk_accepts_all(2000.0, sized)
    assert ok
    # With the screener's 3x factor the same entry would be cut — the reason for the binding.
    rm = RiskManager(BINDING.risk_config)
    decision = rm.evaluate(OrderIntent("TQQQ", NEW_ENTRY, BUY, 29.955052, 50.0,
                                       leverage_factor=3),
                           PortfolioSnapshot(account_value=2000.0, cash=2000.0))
    assert decision.adjusted_quantity < 29.955052
