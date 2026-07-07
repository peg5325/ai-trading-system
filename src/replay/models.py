"""AI Replay System 모델.

Replay 는 과거를 재생하는 시스템이다. 분석/판단은 하지 않는다(판단은 GPT가).
ReplayContext 는 하나의 cycle 전체 상태를 담은 읽기전용 스냅샷이며 JSON 직렬화 가능하고,
GPT Provider 가 그대로 입력으로 받을 수 있다. (이후 AI Review Engine 이 이 구조를 사용)
"""
from __future__ import annotations

from dataclasses import dataclass, field


class CycleNotFoundError(KeyError):
    """존재하지 않는 cycle_id 를 replay 하려 할 때."""


@dataclass
class ReplayContext:
    cycle_id: str
    cycle_ts: str | None
    session: str | None
    status: str
    universe: list[str]
    market_snapshot: dict                 # cycle_ts, session, is_eod_flatten_window, last_prices
    portfolio: dict | None                # 사이클 시점 포트폴리오(+positions)
    symbols: list[dict] = field(default_factory=list)   # symbol별 복원 데이터
    journal: dict | None = None           # 원본 journal cycle (전체 감사)
    realized_pnl: dict | None = None      # as_of 까지의 실현손익(FIFO 엔진) — Review 활용

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "cycle_ts": self.cycle_ts, "session": self.session,
            "status": self.status, "universe": self.universe,
            "market_snapshot": self.market_snapshot, "portfolio": self.portfolio,
            "symbols": self.symbols, "journal": self.journal,
            "realized_pnl": self.realized_pnl,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReplayContext":
        return cls(
            cycle_id=d["cycle_id"], cycle_ts=d.get("cycle_ts"), session=d.get("session"),
            status=d.get("status", "RAN"), universe=list(d.get("universe", [])),
            market_snapshot=d.get("market_snapshot", {}), portfolio=d.get("portfolio"),
            symbols=list(d.get("symbols", [])), journal=d.get("journal"),
            realized_pnl=d.get("realized_pnl"))

    def to_prompt_dict(self, max_candles: int = 20) -> dict:
        """GPT 입력용 요약 뷰. 캔들 배열은 최근 max_candles 개로 절단(프롬프트 크기 보호).
        원본은 변경하지 않는다."""
        symbols = []
        for s in self.symbols:
            item = dict(s)
            candles = item.get("candles") or []
            if len(candles) > max_candles:
                item = dict(item, candles=candles[-max_candles:], candles_truncated=True)
            symbols.append(item)
        return {
            "cycle_id": self.cycle_id, "cycle_ts": self.cycle_ts, "session": self.session,
            "status": self.status, "universe": self.universe,
            "market_snapshot": self.market_snapshot, "portfolio": self.portfolio,
            "symbols": symbols, "realized_pnl": self.realized_pnl,
        }
