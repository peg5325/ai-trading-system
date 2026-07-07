"""주문 실행 계층 데이터 모델 + 인터페이스 + 인메모리 저장소.

흐름: OrderIntent → (RiskManager) → OrderRequest → BrokerProvider → BrokerOrderResult → OrderRecord
- OrderManager 는 BrokerProvider 인터페이스에만 의존한다.
- 이 단계에서는 실주문 없음. PaperBrokerProvider 로 검증한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.risk.models import BUY, SELL  # noqa: F401  (재노출)

# ---- 주문 상태 (요구사항 9) ----
ORD_NEW = "NEW"
ORD_SUBMITTED = "SUBMITTED"
ORD_FILLED = "FILLED"
ORD_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORD_CANCELED = "CANCELED"
ORD_REJECTED = "REJECTED"

# ---- 주문 타입 ----
TYPE_MARKET = "MARKET"
TYPE_LIMIT = "LIMIT"


@dataclass
class OrderRequest:
    """OrderManager 가 risk 통과 후 브로커로 보내는 요청."""
    client_order_id: str
    symbol: str
    side: str                 # BUY / SELL
    quantity: float           # risk-adjusted 수량
    order_type: str           # MARKET / LIMIT
    price: float | None       # LIMIT 가격 or MARKET 참조가
    kind: str                 # NEW_ENTRY / ADD / EXIT / EOD_FLATTEN (분류)
    leverage_factor: int = 1


@dataclass
class BrokerOrderResult:
    """브로커 체결 응답."""
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    status: str
    filled_quantity: float
    avg_fill_price: float
    fee: float
    reject_reason: str | None = None


@dataclass
class OrderRecord:
    """OrderStore 에 저장 가능한 주문 기록 (요구사항 10, 14)."""
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: str
    kind: str
    order_type: str
    requested_quantity: float
    filled_quantity: float
    avg_fill_price: float
    fee: float
    status: str
    reject_reason: str | None = None
    risk_reason_codes: list[str] = field(default_factory=list)
    ref_price: float | None = None       # 의도가(체결 참조가) — 실현손익 슬리피지 분해용

    def to_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "kind": self.kind,
            "order_type": self.order_type,
            "requested_quantity": self.requested_quantity,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "fee": self.fee,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "risk_reason_codes": self.risk_reason_codes,
            "ref_price": self.ref_price,
        }


# ---- 인터페이스 (Protocol) ----
class BrokerProvider(Protocol):
    def place_order(self, request: OrderRequest) -> BrokerOrderResult: ...


class OrderStore(Protocol):
    def save(self, record: OrderRecord) -> None: ...
    def get(self, client_order_id: str) -> OrderRecord | None: ...


class PositionStore(Protocol):
    def apply_fill(self, symbol: str, side: str, quantity: float,
                   price: float, fee: float) -> None: ...
    def get(self, symbol: str) -> dict | None: ...


# ---- 인메모리 저장소 (Paper/테스트용) ----
class InMemoryOrderStore:
    def __init__(self) -> None:
        self._d: dict[str, OrderRecord] = {}

    def save(self, record: OrderRecord) -> None:
        self._d[record.client_order_id] = record

    def get(self, client_order_id: str) -> OrderRecord | None:
        return self._d.get(client_order_id)

    def all(self) -> list[OrderRecord]:
        return list(self._d.values())


class InMemoryPositionStore:
    def __init__(self) -> None:
        self._p: dict[str, dict] = {}   # symbol -> {quantity, avg_price}

    def apply_fill(self, symbol, side, quantity, price, fee) -> None:
        cur = self._p.get(symbol, {"quantity": 0.0, "avg_price": 0.0})
        q, avg = cur["quantity"], cur["avg_price"]
        if side == BUY:
            new_q = q + quantity
            new_avg = (q * avg + quantity * price) / new_q if new_q else 0.0
            self._p[symbol] = {"quantity": new_q, "avg_price": new_avg}
        else:  # SELL
            new_q = q - quantity
            if new_q <= 1e-9:
                self._p.pop(symbol, None)
            else:
                self._p[symbol] = {"quantity": new_q, "avg_price": avg}

    def get(self, symbol) -> dict | None:
        return self._p.get(symbol)

    def all(self) -> dict[str, dict]:
        return {s: dict(v) for s, v in self._p.items()}
