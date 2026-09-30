from __future__ import annotations

import pytest

from src.backtest.execution import ExecutionModel
from src.freeze_paper.costs import CostModel, derived_cost_drags
from src.order.models import OrderRequest
from src.order.paper_broker import PaperBrokerProvider
from src.pnl.fifo import FifoMatcher
from src.pnl.models import FillEvent
from src.risk.models import BUY, SELL
from tests.freeze_paper_test_helpers import BINDING

COST = CostModel.from_binding(BINDING)


def test_sec_fee_minimum_applies_to_small_sells():
    assert COST.sec_fee(100.0) == 0.01          # 0.00206 -> ceil 0.01
    assert COST.sec_fee(0.5) == 0.01


def test_sec_fee_rounds_up_to_the_cent():
    assert COST.sec_fee(1000.0) == 0.03         # 0.0206 -> 0.03
    assert COST.sec_fee(10_000.0) == 0.21       # 0.206 -> 0.21


def test_sec_fee_decimal_boundary_is_exact():
    # 150000 * 0.0000206 is exactly 3.09; binary float gives 3.0900000000000003 and a naive
    # ceil would charge 3.10.
    assert 150000.0 * (0.00206 / 100) > 3.09
    assert COST.sec_fee(150000.0) == 3.09
    assert COST.sec_fee(150000.01) == 3.10
    assert COST.sec_fee(970.8737864077669) == 0.02
    assert COST.sec_fee(970.88) == 0.03


def test_fee_is_commission_on_buys_and_commission_plus_sec_on_sells():
    assert COST.fee(BUY, 1000.0) == pytest.approx(1.0)
    assert COST.fee(SELL, 1000.0) == pytest.approx(1.0 + 0.03)
    assert COST.fee_parts(SELL, 1000.0) == {"commission": pytest.approx(1.0), "sec_fee": 0.03}


def test_paper_broker_fee_model_is_optional_and_additive():
    request = OrderRequest("AAPL", "AAPL", SELL, 10, "MARKET", 100.0, "EXIT")
    default = PaperBrokerProvider(ExecutionModel(5.0, 10.0)).place_order(request)
    assert default.fee == pytest.approx(10 * 99.95 * 0.001)
    with_model = PaperBrokerProvider(ExecutionModel(5.0, 10.0), fee_model=COST.fee).place_order(request)
    assert with_model.avg_fill_price == default.avg_fill_price
    assert with_model.fee == pytest.approx(10 * 99.95 * 0.001 + 0.03)   # SEC 0.0206 -> 0.03


def test_fifo_net_equals_gross_minus_slippage_minus_commission_with_sec_fee():
    broker = PaperBrokerProvider(ExecutionModel(5.0, 10.0), fee_model=COST.fee)
    buy = broker.place_order(OrderRequest("b", "AAPL", BUY, 7, "MARKET", 200.0, "NEW_ENTRY"))
    sell = broker.place_order(OrderRequest("s", "AAPL", SELL, 7, "MARKET", 203.0, "TAKE_PROFIT"))
    m = FifoMatcher(bar_minutes=1.0)
    m.ingest(FillEvent("AAPL", BUY, 7, buy.avg_fill_price, fee=buy.fee, ref_price=200.0))
    (trade,) = m.ingest(FillEvent("AAPL", SELL, 7, sell.avg_fill_price, fee=sell.fee,
                                  ref_price=203.0))
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.slippage - trade.commission)
    assert trade.commission == pytest.approx(buy.fee + sell.fee)
    assert sell.fee == pytest.approx(7 * sell.avg_fill_price * 0.001 + COST.sec_fee(
        7 * sell.avg_fill_price))


def test_official_and_stress_drags_are_per_side_views_over_filled_notional():
    fills = [{"quantity": 2, "price": 100.0}, {"quantity": 2, "price": 101.0}]
    drags = derived_cost_drags(fills, BINDING)
    assert drags["filled_notional"] == pytest.approx(402.0)
    assert drags["official_drag"] == pytest.approx(402.0 * 0.0002)
    assert drags["stress_drag"] == pytest.approx(402.0 * 0.00105)
