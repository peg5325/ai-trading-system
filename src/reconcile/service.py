"""ReconciliationService — 내부 상태 vs 브로커 실계좌 대조.

- BrokerProvider / PositionStore / OrderStore 인터페이스에만 의존 (구현체 비의존).
- 순수 서비스: 앱 시작/주문 전/주문 후/주기 점검 어디서든 호출 가능.
- 결정론적: 현재 시각에 의존하지 않고 조회된 상태만 비교한다.
- CRITICAL 불일치 → block_new_entries=True (RiskManager/SystemState 로 반영).
  청산/리스크 축소 주문은 RiskManager 가 이 플래그와 무관하게 계속 허용한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.reconcile.models import (
    ACTION_BLOCK,
    ACTION_MONITOR,
    ACTION_NONE,
    ACTION_RESYNC,
    AVG_PRICE_MISMATCH,
    BROKER_ONLY_POSITION,
    INTERNAL_ONLY_POSITION,
    MARKET_VALUE_MISMATCH,
    ORDER_FILL_MISMATCH,
    ORDER_MISSING_ON_BROKER,
    ORDER_STATUS_MISMATCH,
    ORDER_UNKNOWN_ON_INTERNAL,
    QUANTITY_MISMATCH,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARNING,
    SEVERITY_RANK,
    STATUS_MATCH,
    STATUS_MISMATCH,
    BrokerProvider,
    Mismatch,
    OrderStore,
    PositionStore,
    ReconciliationReport,
)


@dataclass(frozen=True)
class ReconcileConfig:
    quantity_tolerance: float = 1e-6            # 수량 절대 허용오차
    avg_price_tolerance_pct: float = 0.001      # 평균단가 상대 허용오차(0.1%)
    market_value_tolerance_pct: float = 0.02    # 평가금액 상대 허용오차(2%)
    compare_market_value: bool = True
    compare_orders: bool = True


class ReconciliationService:
    def __init__(self, broker: BrokerProvider, position_store: PositionStore,
                 order_store: OrderStore | None = None,
                 config: ReconcileConfig | None = None):
        self._broker = broker
        self._positions = position_store
        self._orders = order_store
        self.cfg = config or ReconcileConfig()

    def reconcile(self) -> ReconciliationReport:
        mismatches: list[Mismatch] = []
        mismatches += self._reconcile_positions()
        if self.cfg.compare_orders and self._orders is not None:
            mismatches += self._reconcile_orders()
        return self._build_report(mismatches)

    # ---- positions ----
    def _reconcile_positions(self) -> list[Mismatch]:
        tol = self.cfg.quantity_tolerance
        internal = {p.symbol: p for p in self._positions.get_positions()
                    if abs(p.quantity) > tol}
        broker = {p.symbol: p for p in self._broker.get_positions()
                  if abs(p.quantity) > tol}
        out: list[Mismatch] = []
        for sym in sorted(set(internal) | set(broker)):
            i, b = internal.get(sym), broker.get(sym)
            if i and b:
                if abs(i.quantity - b.quantity) > tol:
                    out.append(Mismatch(
                        QUANTITY_MISMATCH, sym, SEV_CRITICAL,
                        internal_quantity=i.quantity, broker_quantity=b.quantity,
                        difference=_r(i.quantity - b.quantity)))
                ref = abs(b.average_price) or abs(i.average_price) or 1.0
                if abs(i.average_price - b.average_price) > self.cfg.avg_price_tolerance_pct * ref:
                    out.append(Mismatch(
                        AVG_PRICE_MISMATCH, sym, SEV_WARNING,
                        internal_avg_price=i.average_price, broker_avg_price=b.average_price,
                        difference=_r(i.average_price - b.average_price)))
                if self.cfg.compare_market_value:
                    mref = abs(b.market_value) or 1.0
                    if abs(i.market_value - b.market_value) > self.cfg.market_value_tolerance_pct * mref:
                        out.append(Mismatch(
                            MARKET_VALUE_MISMATCH, sym, SEV_INFO,
                            detail=f"internal={i.market_value} broker={b.market_value}"))
            elif i and not b:
                out.append(Mismatch(
                    INTERNAL_ONLY_POSITION, sym, SEV_CRITICAL,
                    internal_quantity=i.quantity, broker_quantity=0.0, difference=_r(i.quantity)))
            else:  # b and not i
                out.append(Mismatch(
                    BROKER_ONLY_POSITION, sym, SEV_CRITICAL,
                    internal_quantity=0.0, broker_quantity=b.quantity, difference=_r(-b.quantity)))
        return out

    # ---- orders ----
    def _reconcile_orders(self) -> list[Mismatch]:
        tol = self.cfg.quantity_tolerance
        internal = {o.order_id: o for o in self._orders.get_open_orders()}
        broker = {o.order_id: o for o in self._broker.get_open_orders()}
        out: list[Mismatch] = []
        for oid in sorted(set(internal) | set(broker)):
            i, b = internal.get(oid), broker.get(oid)
            if i and b:
                if i.status != b.status:
                    out.append(Mismatch(
                        ORDER_STATUS_MISMATCH, i.symbol, SEV_WARNING,
                        order_id=oid, internal_status=i.status, broker_status=b.status))
                if abs(i.filled_quantity - b.filled_quantity) > tol:
                    out.append(Mismatch(
                        ORDER_FILL_MISMATCH, i.symbol, SEV_WARNING,
                        order_id=oid, internal_quantity=i.filled_quantity,
                        broker_quantity=b.filled_quantity,
                        difference=_r(i.filled_quantity - b.filled_quantity)))
            elif i and not b:
                out.append(Mismatch(
                    ORDER_MISSING_ON_BROKER, i.symbol, SEV_WARNING,
                    order_id=oid, internal_status=i.status))
            else:  # b and not i
                out.append(Mismatch(
                    ORDER_UNKNOWN_ON_INTERNAL, b.symbol, SEV_WARNING,
                    order_id=oid, broker_status=b.status))
        return out

    # ---- report ----
    def _build_report(self, mismatches: list[Mismatch]) -> ReconciliationReport:
        if not mismatches:
            return ReconciliationReport(STATUS_MATCH, SEV_INFO, [], ACTION_NONE, False)
        severity = max((m.severity for m in mismatches), key=lambda s: SEVERITY_RANK[s])
        if severity == SEV_CRITICAL:
            action, block = ACTION_BLOCK, True
        elif severity == SEV_WARNING:
            action, block = ACTION_RESYNC, False
        else:
            action, block = ACTION_MONITOR, False
        return ReconciliationReport(STATUS_MISMATCH, severity, mismatches, action, block)


def _r(x: float) -> float:
    return round(x, 8)
