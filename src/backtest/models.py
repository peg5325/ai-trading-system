"""Backtest 데이터 모델: Trade / BacktestConfig / BacktestReport / EntryStrategy.

결정론적: 현재 시각에 의존하지 않고 입력 캔들과 설정만 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import pandas as pd

from src.backtest.execution import ExecutionModel
from src.risk.risk_manager import RiskConfig

SAMPLE_OK = "OK"
SAMPLE_INSUFFICIENT = "INSUFFICIENT_SAMPLE"


def iso_z(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    entry_reason: list[str]
    exit_reason: list[str]
    risk_decision: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "entry_time": iso_z(self.entry_time),
            "exit_time": iso_z(self.exit_time),
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "quantity": round(self.quantity, 6),
            "pnl": round(self.pnl, 6),
            "pnl_pct": round(self.pnl_pct, 4),
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "risk_decision": self.risk_decision,
        }


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0
    entry_notional_pct: float = 0.05        # 진입 시 계좌 명목 노출 비율
    stop_loss_pct: float = 0.03             # -3% 손절
    take_profit_pct: float | None = None    # 익절(옵션)
    time_exit_minutes: float | None = None  # N분 보유 후 청산(기본 off — 백테스트 동작 무변경)
    time_exit_bars: int | None = None       # N봉 보유 후 청산(기본 off)
    exit_on_trend_break: bool = True        # close < EMA20 → 청산
    leverage_eod_flatten: bool = True       # 레버리지 ETF 는 거래소 로컬 거래일 마감 청산
    allow_overnight_non_leverage: bool = True   # 일반주 오버나잇 허용(레버리지 차등)
    min_trades: int = 100                   # 표본 충분성 기준
    execution: ExecutionModel = field(default_factory=ExecutionModel)
    risk: RiskConfig = field(default_factory=RiskConfig)


@dataclass
class BacktestReport:
    symbol: str
    start: datetime | None
    end: datetime | None
    summary: dict
    trades: list[Trade]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "period": {"start": iso_z(self.start), "end": iso_z(self.end)},
            "summary": self.summary,
            "trades": [t.to_dict() for t in self.trades],
        }


class EntryStrategy(Protocol):
    """진입 신호 전략. 반환: 진입 이유 리스트(진입) 또는 None(진입 안함).

    look-ahead 방지를 위해 indicators 의 t 행까지만 사용해야 한다.
    하위호환을 위해 market 인자는 추가하지 않는다. 시장 컨텍스트가 필요한 사용자 전략은
    생성 시 market을 주입해야 하며, 기본 ScreenerEntryStrategy는 엔진이 이를 자동 주입한다.
    """
    def signal(self, symbol: str, indicators: pd.DataFrame, t: int,
               leverage_factor: int) -> list[str] | None: ...


def split_in_out(candles: list, ratio: float = 0.7) -> tuple[list, list]:
    """in-sample / out-of-sample 분리 (시간 순서 유지)."""
    if not 0.0 < ratio < 1.0:
        raise ValueError("ratio must be in (0,1)")
    k = int(len(candles) * ratio)
    return candles[:k], candles[k:]
