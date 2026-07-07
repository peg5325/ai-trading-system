"""Paper Trading 실시간 루프 모델 + 인터페이스 + Fake provider.

원칙: 실돈 주문 금지, Paper 체결만, 5분봉 마감 기준, 중복 실행 방지, 장애 시 생존 우선.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.models import Candle

# ---- 사이클 상태 ----
ST_RAN = "RAN"
ST_SKIPPED = "SKIPPED"
ST_ERROR = "ERROR"
ST_STOPPED = "STOPPED"

# ---- skip/err 사유 ----
R_OK = "OK"
R_PAUSED = "PAUSED"
R_STOPPED = "STOPPED"
R_DATA_ERROR = "DATA_ERROR"
R_NO_CLOSED_5M = "NO_CLOSED_5M"
R_NO_NEW_BAR = "NO_NEW_BAR"
R_RATE_LIMITED = "RATE_LIMITED"
R_PIPELINE_ERROR = "PIPELINE_ERROR"


class MarketDataProvider(Protocol):
    def recent(self, symbol: str, interval: str, count: int) -> list[Candle]: ...


@dataclass(frozen=True)
class LoopConfig:
    universe: tuple[str, ...] = ("SOXL",)
    account_value: float = 100_000.0
    recent_1m_count: int = 200      # Toss candles API 최대 count
    polling_interval_s: float = 20.0
    fetch_spacing_s: float = 0.0    # 종목간 시세 호출 간격(429 완화, 0=끔)
    max_calls_per_session: int | None = None
    eod_flatten_before_min: int = 10
    extended_close_et_min: int = 20 * 60      # 20:00 ET (애프터마켓 마감)


@dataclass
class CycleReport:
    cycle_id: str | None
    status: str                 # RAN / SKIPPED / ERROR / STOPPED
    reason: str
    session: str | None = None
    pipeline_result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "status": self.status, "reason": self.reason,
            "session": self.session, "pipeline_result": self.pipeline_result,
            "error": self.error,
        }


class InMemoryResultLog:
    """사이클 결과 저장 (요구사항 10). 실제로는 DB/파일 로거로 교체 가능."""
    def __init__(self) -> None:
        self.reports: list[CycleReport] = []

    def save(self, report: CycleReport) -> None:
        self.reports.append(report)


class FakeMarketDataProvider:
    """테스트/수동용. 실제 Toss 호출 없이 스크립트된 1분봉을 반환.
    fail_symbols 에 든 종목은 recent 시 예외 → 종목별 실패 격리 테스트용."""
    def __init__(self, data: dict | None = None, raise_exc: Exception | None = None,
                 fail_symbols: set | None = None):
        self.data: dict[str, list[Candle]] = data or {}
        self.raise_exc = raise_exc
        self.fail_symbols = fail_symbols or set()
        self.calls = 0

    def recent(self, symbol: str, interval: str, count: int) -> list[Candle]:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        if symbol in self.fail_symbols:
            raise ConnectionError(f"fake data error for {symbol}")
        return list(self.data.get(symbol, []))[-count:]
