"""PaperBrokerProvider — 모의 체결. 토스 샌드박스 부재를 대체한다.

- 기본 full fill. slippage/fee 는 ExecutionModel 로 설정 가능(요구사항 8).
- 실주문 없음. 결정론적(난수/시각 미사용).
"""
from __future__ import annotations

from src.backtest.execution import ExecutionModel
from src.order.models import (
    ORD_FILLED,
    ORD_REJECTED,
    BrokerOrderResult,
    OrderRequest,
)


class PaperBrokerProvider:
    def __init__(self, execution: ExecutionModel | None = None,
                 reject_symbols: set[str] | None = None):
        self.execution = execution or ExecutionModel()
        self.reject_symbols = reject_symbols or set()

    def place_order(self, request: OrderRequest) -> BrokerOrderResult:
        if request.symbol in self.reject_symbols:
            return self._reject(request, "PAPER_REJECT_SYMBOL")
        if request.quantity <= 0 or request.price is None or request.price <= 0:
            return self._reject(request, "INVALID_REQUEST")

        fill_price = self.execution.fill_price(request.price, request.side)
        fee = self.execution.fee(request.quantity * fill_price)
        return BrokerOrderResult(
            client_order_id=request.client_order_id,
            broker_order_id=f"paper-{request.client_order_id}",
            symbol=request.symbol, side=request.side,
            status=ORD_FILLED, filled_quantity=request.quantity,
            avg_fill_price=fill_price, fee=fee)

    def _reject(self, request: OrderRequest, reason: str) -> BrokerOrderResult:
        return BrokerOrderResult(
            client_order_id=request.client_order_id, broker_order_id="",
            symbol=request.symbol, side=request.side, status=ORD_REJECTED,
            filled_quantity=0.0, avg_fill_price=0.0, fee=0.0, reject_reason=reason)
