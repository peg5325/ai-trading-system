"""Reconciliation 데이터 모델 + 인터페이스.

ReconciliationService 는 이 뷰 타입과 Protocol 인터페이스에만 의존한다.
Toss/KIS 구현체는 어댑터에서 이 뷰로 변환해 제공한다(여기선 비의존).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

# ---- 주문 상태 ----
ORDER_NEW = "NEW"
ORDER_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORDER_FILLED = "FILLED"
ORDER_CANCELED = "CANCELED"
ORDER_REJECTED = "REJECTED"

# ---- report status / severity / action ----
STATUS_MATCH = "MATCH"
STATUS_MISMATCH = "MISMATCH"

SEV_INFO = "INFO"
SEV_WARNING = "WARNING"
SEV_CRITICAL = "CRITICAL"
SEVERITY_RANK = {SEV_INFO: 0, SEV_WARNING: 1, SEV_CRITICAL: 2}

ACTION_NONE = "NONE"
ACTION_MONITOR = "MONITOR"
ACTION_RESYNC = "RESYNC_AND_MONITOR"
ACTION_BLOCK = "BLOCK_NEW_ENTRIES_AND_RESYNC"

# ---- mismatch types ----
QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
AVG_PRICE_MISMATCH = "AVG_PRICE_MISMATCH"
MARKET_VALUE_MISMATCH = "MARKET_VALUE_MISMATCH"
INTERNAL_ONLY_POSITION = "INTERNAL_ONLY_POSITION"
BROKER_ONLY_POSITION = "BROKER_ONLY_POSITION"
ORDER_STATUS_MISMATCH = "ORDER_STATUS_MISMATCH"
ORDER_FILL_MISMATCH = "ORDER_FILL_MISMATCH"
ORDER_MISSING_ON_BROKER = "ORDER_MISSING_ON_BROKER"
ORDER_UNKNOWN_ON_INTERNAL = "ORDER_UNKNOWN_ON_INTERNAL"


# ---- 비교용 뷰 타입 ----
@dataclass
class PositionView:
    symbol: str
    quantity: float
    average_price: float
    market_value: float = 0.0


@dataclass
class OrderView:
    order_id: str
    symbol: str
    status: str
    quantity: float
    filled_quantity: float = 0.0


@dataclass
class Mismatch:
    type: str
    symbol: str
    severity: str
    internal_quantity: float | None = None
    broker_quantity: float | None = None
    internal_avg_price: float | None = None
    broker_avg_price: float | None = None
    difference: float | None = None
    order_id: str | None = None
    internal_status: str | None = None
    broker_status: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        # severity 는 report 레벨에서 집계되므로 개별 항목 dict 에서는 제외.
        return {k: v for k, v in asdict(self).items()
                if v is not None and k != "severity"}


@dataclass
class ReconciliationReport:
    status: str                       # MATCH | MISMATCH
    severity: str                     # INFO | WARNING | CRITICAL
    mismatches: list[Mismatch] = field(default_factory=list)
    recommended_action: str = ACTION_NONE
    block_new_entries: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "severity": self.severity,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "recommended_action": self.recommended_action,
            "block_new_entries": self.block_new_entries,
        }


# ---- 인터페이스 (Protocol: 구조적 타이핑, 구현체 비의존) ----
class BrokerProvider(Protocol):
    def get_positions(self) -> list[PositionView]: ...
    def get_open_orders(self) -> list[OrderView]: ...


class PositionStore(Protocol):
    def get_positions(self) -> list[PositionView]: ...


class OrderStore(Protocol):
    def get_open_orders(self) -> list[OrderView]: ...
