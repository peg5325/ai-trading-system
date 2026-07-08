"""운영 지표 집계. 사이클 리포트에서 카운트/시간을 누적한다."""
from __future__ import annotations

from dataclasses import dataclass, field

from src.ai.models import DEC_APPROVE, DEC_REJECT, DEC_WAIT
from src.pipeline.models import R_RISK_BLOCK
from src.risk.models import EOD_FLATTEN, TRIGGERED_EXITS
from src.order.models import BUY, ORD_FILLED, SELL


@dataclass
class RuntimeMetrics:
    iterations: int = 0
    cycles_run: int = 0
    skipped: int = 0
    api_errors: int = 0
    pipeline_errors: int = 0
    exceptions: int = 0
    sqlite_locks: int = 0
    # per-symbol 집계
    candidates: int = 0
    ai_approve: int = 0
    ai_wait: int = 0
    ai_reject: int = 0
    risk_block: int = 0
    orders: int = 0
    buys: int = 0
    sells: int = 0
    exits: int = 0
    eods: int = 0
    # 시간
    loop_times_s: list[float] = field(default_factory=list)
    ai_latencies_ms: list[float] = field(default_factory=list)

    def record_cycle(self, report, dt_s: float) -> None:
        self.iterations += 1
        self.loop_times_s.append(dt_s)
        st, rs = report.status, report.reason
        if st == "RAN":
            self.cycles_run += 1
            self._aggregate(report.pipeline_result or {})
        elif st == "SKIPPED":
            if rs == "DATA_ERROR":
                self.api_errors += 1
            else:
                self.skipped += 1
        elif st == "ERROR":
            self.pipeline_errors += 1

    def _aggregate(self, pr: dict) -> None:
        for o in pr.get("outcomes", []):
            if o.get("candidate") is not None:
                self.candidates += 1
            ai = o.get("ai_decision")
            if ai:
                d = ai.get("decision")
                if d == DEC_APPROVE:
                    self.ai_approve += 1
                elif d == DEC_WAIT:
                    self.ai_wait += 1
                elif d == DEC_REJECT:
                    self.ai_reject += 1
            if o.get("reason") == R_RISK_BLOCK:
                self.risk_block += 1
            al = o.get("ai_log")
            if al and al.get("latency_ms") is not None:
                self.ai_latencies_ms.append(al["latency_ms"])
            order = o.get("order_record")
            if order and order.get("status") == ORD_FILLED:
                self.orders += 1
                if order.get("side") == BUY:
                    self.buys += 1
                elif order.get("side") == SELL:
                    self.sells += 1
                if order.get("kind") in TRIGGERED_EXITS:
                    self.exits += 1
                elif order.get("kind") == EOD_FLATTEN:
                    self.eods += 1

    def avg_loop_ms(self) -> float:
        return round(1000.0 * sum(self.loop_times_s) / len(self.loop_times_s), 2) if self.loop_times_s else 0.0

    def avg_ai_ms(self) -> float:
        return round(sum(self.ai_latencies_ms) / len(self.ai_latencies_ms), 2) if self.ai_latencies_ms else 0.0

    def to_dict(self) -> dict:
        return {
            "iterations": self.iterations, "cycles_run": self.cycles_run,
            "skipped": self.skipped, "api_errors": self.api_errors,
            "pipeline_errors": self.pipeline_errors, "exceptions": self.exceptions,
            "sqlite_locks": self.sqlite_locks, "candidates": self.candidates,
            "ai_approve": self.ai_approve, "ai_wait": self.ai_wait, "ai_reject": self.ai_reject,
            "risk_block": self.risk_block, "orders": self.orders, "buys": self.buys,
            "sells": self.sells, "exits": self.exits, "eods": self.eods,
            "avg_loop_ms": self.avg_loop_ms(), "avg_ai_ms": self.avg_ai_ms(),
        }
