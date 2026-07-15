"""Trading Journal 모델 + 인터페이스.

이 모듈은 매매 판단을 하지 않는다. 오직 사이클별 의사결정 과정을 기록/추적한다.
나중에 AI 회고, 전략 개선, 사용자 행동 분석의 기반 데이터가 된다.

- cycle_id 로 한 사이클 전체를 묶는다.
- 주문이 없었던 사이클도 반드시 저장한다.
- JSON 직렬화 가능한 구조.
- JournalStore 인터페이스로 SQLite(MVP) → PostgreSQL/TimescaleDB 교체 가능.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


def ts_from_cycle_id(cycle_id: str | None) -> str | None:
    """'20260706T144500Z' → '2026-07-06T14:45:00Z' (파싱 실패 시 None)."""
    if not cycle_id:
        return None
    try:
        dt = datetime.strptime(cycle_id, "%Y%m%dT%H%M%SZ")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


@dataclass
class JournalCycle:
    cycle_id: str
    cycle_ts: str | None
    session: str | None
    universe: list[str]
    status: str                 # RAN / SKIPPED / ERROR
    reason: str | None
    error: str | None
    outcomes: list[dict] = field(default_factory=list)   # per-symbol outcome dict
    order_count: int = 0
    portfolio: dict | None = None       # 사이클 시점 포트폴리오 스냅샷 (replay 복원용)
    market: dict | None = None          # 사이클 시점 market context (세션/EOD, AI 용)
    benchmark_context: dict | None = None   # 벤치마크 국면 관측(v2.1 PR 2-B, 매매 무관)
    shadow: dict | None = None          # Online Shadow Decision(v2.1 PR 4-A, hypothetical·매매 무관)

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "cycle_ts": self.cycle_ts, "session": self.session,
            "universe": self.universe, "status": self.status, "reason": self.reason,
            "error": self.error, "outcomes": self.outcomes, "order_count": self.order_count,
            "portfolio": self.portfolio, "market": self.market,
            "benchmark_context": self.benchmark_context, "shadow": self.shadow,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "JournalCycle":
        return cls(
            cycle_id=d["cycle_id"], cycle_ts=d.get("cycle_ts"), session=d.get("session"),
            universe=list(d.get("universe", [])), status=d.get("status", "RAN"),
            reason=d.get("reason"), error=d.get("error"),
            outcomes=list(d.get("outcomes", [])), order_count=d.get("order_count", 0),
            portfolio=d.get("portfolio"), market=d.get("market"),
            benchmark_context=d.get("benchmark_context"), shadow=d.get("shadow"))

    @classmethod
    def from_pipeline(cls, cycle_id, session, universe, result, *,
                      cycle_ts=None, status="RAN", error=None,
                      portfolio=None, market=None, benchmark_context=None,
                      shadow=None) -> "JournalCycle":
        outs = [o.to_dict() for o in result.outcomes]
        return cls(cycle_id, cycle_ts or ts_from_cycle_id(cycle_id), session,
                   list(universe), status, None, error, outs, len(result.orders()),
                   portfolio=_as_dict(portfolio), market=_as_dict(market),
                   benchmark_context=benchmark_context, shadow=shadow)

    @classmethod
    def error_cycle(cls, cycle_id, universe, error, *, session=None,
                    cycle_ts=None, reason="PIPELINE_ERROR",
                    portfolio=None, market=None) -> "JournalCycle":
        return cls(cycle_id, cycle_ts or ts_from_cycle_id(cycle_id), session,
                   list(universe), "ERROR", reason, error, [], 0,
                   portfolio=_as_dict(portfolio), market=_as_dict(market))


def _as_dict(obj):
    if obj is None or isinstance(obj, dict):
        return obj
    return obj.to_dict() if hasattr(obj, "to_dict") else None


class JournalStore(Protocol):
    """PaperTradingLoop / TradingPipeline 공용. 구현체 교체 가능(SQLite→PG/Timescale)."""
    def save_cycle(self, cycle: JournalCycle) -> None: ...
    def get_cycle(self, cycle_id: str) -> JournalCycle | None: ...
    def get_by_symbol(self, symbol: str, start: datetime | None = None,
                      end: datetime | None = None) -> list[dict]: ...
    def get_by_time_range(self, start: datetime | None = None,
                          end: datetime | None = None) -> list[JournalCycle]: ...


class InMemoryJournalStore:
    """가벼운 인메모리 구현 (테스트/개발용). 인터페이스 동일."""
    def __init__(self) -> None:
        self._cycles: dict[str, JournalCycle] = {}

    def save_cycle(self, cycle: JournalCycle) -> None:
        self._cycles[cycle.cycle_id] = cycle

    def get_cycle(self, cycle_id: str) -> JournalCycle | None:
        return self._cycles.get(cycle_id)

    def get_by_symbol(self, symbol, start=None, end=None) -> list[dict]:
        out = []
        for c in self._cycles.values():
            if not _in_range(c.cycle_ts, start, end):
                continue
            for o in c.outcomes:
                if o.get("symbol") == symbol:
                    out.append({"cycle_id": c.cycle_id, "cycle_ts": c.cycle_ts, **o})
        return sorted(out, key=lambda r: r.get("cycle_ts") or "")

    def get_by_time_range(self, start=None, end=None) -> list[JournalCycle]:
        cs = [c for c in self._cycles.values() if _in_range(c.cycle_ts, start, end)]
        return sorted(cs, key=lambda c: c.cycle_ts or "")


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_range(cycle_ts: str | None, start: datetime | None, end: datetime | None) -> bool:
    if start is None and end is None:
        return True
    if cycle_ts is None:
        return False
    lo, hi = iso_z(start), iso_z(end)
    if lo is not None and cycle_ts < lo:
        return False
    if hi is not None and cycle_ts > hi:
        return False
    return True
