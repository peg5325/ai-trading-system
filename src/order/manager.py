"""OrderManager — RiskDecision 을 통과한 주문만 브로커로 전달·기록·반영.

원칙:
- BrokerProvider 인터페이스에만 의존 (Paper/Toss 교체 가능).
- RiskDecision.allowed=True 인 주문만 브로커로 전달 (AI 승인만으로는 불가).
- client_order_id 로 중복 방지/멱등 처리.
- 체결 결과를 OrderStore(주문)와 PositionStore(포지션)에 반영.
- kill switch 등은 RiskManager 가 판단 → 여기선 allowed 만 신뢰
  (그래서 kill switch 하에서도 RiskManager 가 허용한 EXIT/EOD_FLATTEN 은 실행됨).
"""
from __future__ import annotations

from src.order.models import (
    ORD_REJECTED,
    TYPE_MARKET,
    BrokerProvider,
    OrderRecord,
    OrderRequest,
    OrderStore,
    PositionStore,
)
from src.risk.models import OrderIntent, RiskDecision


class OrderManager:
    def __init__(self, broker: BrokerProvider, order_store: OrderStore,
                 position_store: PositionStore):
        self._broker = broker
        self._orders = order_store
        self._positions = position_store

    def submit(self, intent: OrderIntent, risk_decision: RiskDecision, *,
               client_order_id: str, ref_price: float,
               order_type: str = TYPE_MARKET, limit_price: float | None = None) -> OrderRecord:
        # 1) 멱등: 동일 client_order_id 는 기존 기록 반환(재전송 방지)
        existing = self._orders.get(client_order_id)
        if existing is not None:
            return existing

        # 2) 리스크 게이트: allowed 아니면 브로커로 보내지 않고 REJECTED 기록
        if not risk_decision.allowed:
            return self._record_reject(
                client_order_id, intent, order_type, "RISK_BLOCKED",
                risk_decision.reason_codes)

        qty = risk_decision.adjusted_quantity
        if qty <= 0:
            return self._record_reject(
                client_order_id, intent, order_type, "NON_POSITIVE_QUANTITY",
                risk_decision.reason_codes)

        # 3) OrderRequest 구성 후 브로커 전달
        price = limit_price if order_type != TYPE_MARKET else ref_price
        request = OrderRequest(
            client_order_id=client_order_id, symbol=intent.symbol, side=intent.side,
            quantity=qty, order_type=order_type, price=price, kind=intent.kind,
            leverage_factor=intent.leverage_factor)
        result = self._broker.place_order(request)

        # 4) 기록 + 포지션 반영
        record = OrderRecord(
            client_order_id=result.client_order_id,
            broker_order_id=result.broker_order_id or None,
            symbol=result.symbol, side=result.side, kind=intent.kind,
            order_type=order_type, requested_quantity=qty,
            filled_quantity=result.filled_quantity, avg_fill_price=result.avg_fill_price,
            fee=result.fee, status=result.status, reject_reason=result.reject_reason,
            risk_reason_codes=list(risk_decision.reason_codes))
        self._orders.save(record)

        if result.filled_quantity > 0:
            self._positions.apply_fill(
                result.symbol, result.side, result.filled_quantity,
                result.avg_fill_price, result.fee)
        return record

    def _record_reject(self, client_order_id, intent: OrderIntent, order_type,
                       reason, risk_codes) -> OrderRecord:
        record = OrderRecord(
            client_order_id=client_order_id, broker_order_id=None,
            symbol=intent.symbol, side=intent.side, kind=intent.kind,
            order_type=order_type, requested_quantity=intent.quantity,
            filled_quantity=0.0, avg_fill_price=0.0, fee=0.0,
            status=ORD_REJECTED, reject_reason=reason,
            risk_reason_codes=list(risk_codes))
        self._orders.save(record)
        return record
