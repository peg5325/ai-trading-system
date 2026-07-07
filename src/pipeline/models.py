"""TradingPipeline 입출력 모델.

Pipeline 은 시스템 연결자일 뿐이며, 지표/AI/리스크 판단을 직접 구현하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- 사이클 결과 사유 ----
R_NO_DATA = "NO_DATA"
R_NO_CANDIDATE = "NO_CANDIDATE"
R_AI_WAIT = "AI_WAIT"
R_AI_REJECT = "AI_REJECT"
R_RISK_BLOCK = "RISK_BLOCK"
R_ORDERED = "ORDERED"
R_EXIT_ORDERED = "EXIT_ORDERED"
R_EXIT_BLOCKED = "EXIT_BLOCKED"

ACTION_ORDERED = "ORDERED"
ACTION_SKIPPED = "SKIPPED"


@dataclass
class SystemState:
    """시스템 전역 상태 (Reconciliation CRITICAL 등 반영)."""
    kill_switch: bool = False
    block_new_entries: bool = False   # reconciliation block_new_entries 를 여기로


@dataclass
class SymbolOutcome:
    symbol: str
    action: str                 # ORDERED / SKIPPED
    reason: str
    ai_decision: dict | None = None
    risk_decision: dict | None = None
    order_record: dict | None = None
    # 감사(Journal)용 상세
    indicator_snapshot: dict | None = None
    candidate: dict | None = None
    ai_log: dict | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "reason": self.reason,
            "ai_decision": self.ai_decision,
            "risk_decision": self.risk_decision,
            "order_record": self.order_record,
            "indicator_snapshot": self.indicator_snapshot,
            "candidate": self.candidate,
            "ai_log": self.ai_log,
        }


@dataclass
class PipelineResult:
    cycle_id: str
    outcomes: list[SymbolOutcome] = field(default_factory=list)

    def orders(self) -> list[SymbolOutcome]:
        return [o for o in self.outcomes if o.order_record is not None]

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "order_count": len(self.orders()),
        }
