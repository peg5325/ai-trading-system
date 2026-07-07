"""ReconciliationService 테스트: 일치, 수량/평균단가 불일치, 내부-only/브로커-only,
부분체결 주문 불일치, CRITICAL 플래그, tolerance, RiskManager 연동."""
from __future__ import annotations

from src.reconcile.models import (
    ACTION_BLOCK,
    ACTION_RESYNC,
    AVG_PRICE_MISMATCH,
    BROKER_ONLY_POSITION,
    INTERNAL_ONLY_POSITION,
    ORDER_FILL_MISMATCH,
    ORDER_PARTIALLY_FILLED,
    QUANTITY_MISMATCH,
    SEV_CRITICAL,
    SEV_WARNING,
    STATUS_MATCH,
    STATUS_MISMATCH,
    OrderView,
    PositionView,
)
from src.reconcile.service import ReconcileConfig, ReconciliationService


# ---- fakes (인터페이스 duck-typing) ----
class FakeBroker:
    def __init__(self, positions=(), orders=()):
        self._p, self._o = list(positions), list(orders)

    def get_positions(self):
        return list(self._p)

    def get_open_orders(self):
        return list(self._o)


class FakePositionStore:
    def __init__(self, positions=()):
        self._p = list(positions)

    def get_positions(self):
        return list(self._p)


class FakeOrderStore:
    def __init__(self, orders=()):
        self._o = list(orders)

    def get_open_orders(self):
        return list(self._o)


def _svc(internal_pos=(), broker_pos=(), internal_ord=(), broker_ord=(), cfg=None):
    return ReconciliationService(
        FakeBroker(broker_pos, broker_ord),
        FakePositionStore(internal_pos),
        FakeOrderStore(internal_ord),
        cfg,
    )


def pos(sym, qty, avg, mv=None):
    return PositionView(sym, qty, avg, mv if mv is not None else qty * avg)


# ---- 정상 일치 ----
def test_match():
    svc = _svc(internal_pos=[pos("SOXL", 10, 194.0)],
               broker_pos=[pos("SOXL", 10, 194.0)])
    r = svc.reconcile()
    assert r.status == STATUS_MATCH
    assert r.severity == "INFO"
    assert r.block_new_entries is False
    assert r.mismatches == []


# ---- 수량 불일치 (예시 시나리오) ----
def test_quantity_mismatch_critical():
    svc = _svc(internal_pos=[pos("SOXL", 10, 194.0, mv=1940)],
               broker_pos=[pos("SOXL", 8, 194.0, mv=1940)])
    r = svc.reconcile()
    assert r.status == STATUS_MISMATCH
    assert r.severity == SEV_CRITICAL
    assert r.recommended_action == ACTION_BLOCK
    assert r.block_new_entries is True
    m = r.mismatches[0]
    assert m.type == QUANTITY_MISMATCH
    assert m.internal_quantity == 10 and m.broker_quantity == 8 and m.difference == 2
    # 출력 구조
    d = r.to_dict()
    assert set(d) == {"status", "severity", "mismatches", "recommended_action", "block_new_entries"}
    assert "severity" not in d["mismatches"][0]   # 항목엔 severity 미포함


# ---- 평균단가 불일치 (WARNING) ----
def test_avg_price_mismatch_warning():
    svc = _svc(internal_pos=[pos("SOXL", 10, 194.0)],
               broker_pos=[pos("SOXL", 10, 196.0)])
    r = svc.reconcile()
    assert r.status == STATUS_MISMATCH
    assert r.severity == SEV_WARNING
    assert r.recommended_action == ACTION_RESYNC
    assert r.block_new_entries is False
    assert any(m.type == AVG_PRICE_MISMATCH for m in r.mismatches)


# ---- 내부에만 있는 포지션 ----
def test_internal_only_position_critical():
    svc = _svc(internal_pos=[pos("SOXL", 10, 194.0)], broker_pos=[])
    r = svc.reconcile()
    assert r.severity == SEV_CRITICAL and r.block_new_entries is True
    assert r.mismatches[0].type == INTERNAL_ONLY_POSITION


# ---- 브로커에만 있는 포지션 ----
def test_broker_only_position_critical():
    svc = _svc(internal_pos=[], broker_pos=[pos("NVDA", 5, 195.0)])
    r = svc.reconcile()
    assert r.severity == SEV_CRITICAL and r.block_new_entries is True
    assert r.mismatches[0].type == BROKER_ONLY_POSITION


# ---- 부분체결 주문 불일치 ----
def test_partial_fill_order_mismatch():
    internal_ord = [OrderView("o1", "SOXL", ORDER_PARTIALLY_FILLED, 10, filled_quantity=3)]
    broker_ord = [OrderView("o1", "SOXL", ORDER_PARTIALLY_FILLED, 10, filled_quantity=5)]
    svc = _svc(internal_pos=[pos("SOXL", 3, 194)], broker_pos=[pos("SOXL", 3, 194)],
               internal_ord=internal_ord, broker_ord=broker_ord)
    r = svc.reconcile()
    assert r.status == STATUS_MISMATCH
    fill = [m for m in r.mismatches if m.type == ORDER_FILL_MISMATCH]
    assert fill and fill[0].difference == -2   # 3 - 5
    assert r.severity == SEV_WARNING


# ---- tolerance 적용 ----
def test_tolerances_absorb_small_diffs():
    cfg = ReconcileConfig(quantity_tolerance=1e-3, avg_price_tolerance_pct=0.002)
    svc = _svc(
        internal_pos=[pos("SOXL", 10.0000, 194.00)],
        broker_pos=[pos("SOXL", 10.0005, 194.20)],   # 수량 5e-4<1e-3, 가격 0.1%<0.2%
        cfg=cfg,
    )
    r = svc.reconcile()
    assert r.status == STATUS_MATCH
    assert r.block_new_entries is False


# ---- CRITICAL 플래그가 RiskManager 신규진입을 차단하고 청산은 허용 ----
def test_critical_flag_blocks_entry_allows_exit_via_risk_manager():
    from src.risk.models import BUY, EXIT, NEW_ENTRY, SELL, OrderIntent, PortfolioSnapshot, Position
    from src.risk.risk_manager import RC_KILL_SWITCH, RiskManager

    report = _svc(internal_pos=[pos("SOXL", 10, 194)],
                  broker_pos=[pos("SOXL", 8, 194)]).reconcile()
    assert report.block_new_entries is True

    rm = RiskManager()
    positions = {"SOXL": Position("SOXL", 8, 194, 193, leverage_factor=3)}
    # reconciliation 결과를 신규진입 차단 플래그로 반영 (kill_switch 로 매핑)
    snap = PortfolioSnapshot(account_value=100_000, positions=positions,
                             kill_switch=report.block_new_entries)

    entry = rm.evaluate(OrderIntent("SOXL", NEW_ENTRY, BUY, 5, 194, 3), snap)
    assert not entry.allowed and RC_KILL_SWITCH in entry.reason_codes

    exit_ = rm.evaluate(OrderIntent("SOXL", EXIT, SELL, 8, 193, 3), snap)
    assert exit_.allowed and exit_.adjusted_quantity == 8   # 청산은 계속 허용


# ---- 결정론 ----
def test_deterministic():
    svc = _svc(internal_pos=[pos("SOXL", 10, 194)], broker_pos=[pos("SOXL", 8, 194)])
    assert svc.reconcile().to_dict() == svc.reconcile().to_dict()
