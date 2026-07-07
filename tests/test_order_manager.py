"""OrderManager + PaperBrokerProvider 테스트: 정상 체결, Risk block, 중복 client_order_id,
수수료/슬리피지, EOD flatten, kill switch 하 EXIT 허용, 주문 거부, 포지션 반영."""
from __future__ import annotations

import pytest

from src.backtest.execution import ExecutionModel
from src.order.manager import OrderManager
from src.order.models import (
    ORD_FILLED,
    ORD_REJECTED,
    InMemoryOrderStore,
    InMemoryPositionStore,
)
from src.order.paper_broker import PaperBrokerProvider
from src.risk.models import (
    BUY,
    EOD_FLATTEN,
    EXIT,
    NEW_ENTRY,
    SELL,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    RiskDecision,
)
from src.risk.risk_manager import RiskManager

ALLOW = RiskDecision(True, [], 10.0, ["LEVERAGED_ETF"])
BLOCK = RiskDecision(False, ["MAX_EFFECTIVE_EXPOSURE_EXCEEDED"], 0.0, ["LEVERAGED_ETF"])


def mgr(broker=None):
    return OrderManager(broker or PaperBrokerProvider(ExecutionModel(0, 0)),
                        InMemoryOrderStore(), InMemoryPositionStore())


def buy(qty=10.0, price=194.0):
    return OrderIntent("SOXL", NEW_ENTRY, BUY, qty, price, leverage_factor=3)


# ---- 정상 체결 + 포지션 반영 ----
def test_normal_fill_and_position_update():
    om = mgr()
    rec = om.submit(buy(10), ALLOW, client_order_id="c1", ref_price=194.0)
    assert rec.status == ORD_FILLED
    assert rec.filled_quantity == 10.0
    assert rec.kind == NEW_ENTRY
    assert rec.broker_order_id == "paper-c1"
    pos = om._positions.get("SOXL")
    assert pos["quantity"] == 10.0 and pos["avg_price"] == pytest.approx(194.0)


# ---- Risk block: 브로커로 안 보내고 REJECTED ----
def test_risk_block_not_forwarded():
    om = mgr()
    rec = om.submit(buy(10), BLOCK, client_order_id="c1", ref_price=194.0)
    assert rec.status == ORD_REJECTED
    assert rec.reject_reason == "RISK_BLOCKED"
    assert "MAX_EFFECTIVE_EXPOSURE_EXCEEDED" in rec.risk_reason_codes
    assert om._positions.get("SOXL") is None    # 포지션 변화 없음


# ---- 중복 client_order_id 멱등 처리 ----
def test_duplicate_client_order_id_is_idempotent():
    broker = PaperBrokerProvider(ExecutionModel(0, 0))
    om = OrderManager(broker, InMemoryOrderStore(), InMemoryPositionStore())
    r1 = om.submit(buy(10), ALLOW, client_order_id="dup", ref_price=194.0)
    r2 = om.submit(buy(10), ALLOW, client_order_id="dup", ref_price=194.0)
    assert r1 is r2 or r1.to_dict() == r2.to_dict()
    # 두 번째는 재체결하지 않으므로 포지션은 10 (20 아님)
    assert om._positions.get("SOXL")["quantity"] == 10.0


# ---- risk-adjusted 수량으로 체결 (adjusted_quantity 사용) ----
def test_uses_risk_adjusted_quantity():
    om = mgr()
    reduced = RiskDecision(True, ["QUANTITY_REDUCED_TO_LIMIT"], 4.0, [])
    rec = om.submit(buy(10), reduced, client_order_id="c1", ref_price=194.0)
    assert rec.filled_quantity == 4.0          # 요청 10 이지만 risk 조정 4


# ---- 수수료 / 슬리피지 반영 ----
def test_fee_and_slippage_applied():
    om = mgr(PaperBrokerProvider(ExecutionModel(slippage_bps=10, fee_bps=5)))
    rec = om.submit(buy(10, 100.0), ALLOW, client_order_id="c1", ref_price=100.0)
    assert rec.avg_fill_price == pytest.approx(100.0 * 1.001)   # 매수 슬리피지 +
    assert rec.fee == pytest.approx(10 * (100.0 * 1.001) * 0.0005)


# ---- EOD flatten / EXIT 을 kind 로 구분 기록 ----
def test_eod_flatten_and_exit_kinds_recorded():
    om = mgr()
    om._positions.apply_fill("SOXL", BUY, 10, 194, 0)  # 보유 상태
    eod = om.submit(OrderIntent("SOXL", EOD_FLATTEN, SELL, 10, 193, 3),
                    ALLOW, client_order_id="e1", ref_price=193.0)
    assert eod.kind == EOD_FLATTEN and eod.status == ORD_FILLED
    assert om._positions.get("SOXL") is None    # 청산됨


# ---- kill switch 하에서도 RiskManager 가 허용한 EXIT 은 실행 ----
def test_exit_executes_under_kill_switch():
    rm = RiskManager()
    pos = {"SOXL": Position("SOXL", 10, 194, 190, leverage_factor=3)}
    snap = PortfolioSnapshot(account_value=100_000, positions=pos, kill_switch=True,
                             daily_pnl_pct=-0.09)
    exit_intent = OrderIntent("SOXL", EXIT, SELL, 10, 190, 3)
    decision = rm.evaluate(exit_intent, snap)      # RiskManager 가 청산 허용
    assert decision.allowed
    om = mgr()
    om._positions.apply_fill("SOXL", BUY, 10, 194, 0)
    rec = om.submit(exit_intent, decision, client_order_id="k1", ref_price=190.0)
    assert rec.status == ORD_FILLED and rec.kind == EXIT


# ---- 브로커 주문 거부 ----
def test_broker_reject():
    om = mgr(PaperBrokerProvider(reject_symbols={"SOXL"}))
    rec = om.submit(buy(10), ALLOW, client_order_id="c1", ref_price=194.0)
    assert rec.status == ORD_REJECTED
    assert rec.reject_reason == "PAPER_REJECT_SYMBOL"
    assert om._positions.get("SOXL") is None


# ---- SELL 후 포지션 감소 ----
def test_sell_reduces_position():
    om = mgr()
    om.submit(buy(10), ALLOW, client_order_id="b1", ref_price=194.0)
    sell = OrderIntent("SOXL", EXIT, SELL, 4, 196, 3)
    om.submit(sell, RiskDecision(True, [], 4.0, []), client_order_id="s1", ref_price=196.0)
    assert om._positions.get("SOXL")["quantity"] == pytest.approx(6.0)


# ---- 주문 저장소에 기록됨 ----
def test_order_saved_to_store():
    store = InMemoryOrderStore()
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)), store, InMemoryPositionStore())
    om.submit(buy(10), ALLOW, client_order_id="c1", ref_price=194.0)
    assert store.get("c1") is not None
    assert len(store.all()) == 1
