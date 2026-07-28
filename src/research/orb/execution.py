"""ORB 5분 결정봉 청산 의미론 — §8.4·§8.5·§8.7·§8.8."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from src.models import Candle
from src.research.orb.costs import CostedPnl, all_costed_pnls
from src.research.orb.portfolio import Position


@dataclass(frozen=True)
class ExitFill:
    symbol: str
    day: date
    exit_bar_ts_utc: datetime
    exit_minute: int
    exit_price: float
    exit_reason: str


@dataclass(frozen=True)
class TradeRecord:
    position: Position
    exit: ExitFill | None
    costs: Mapping[str, CostedPnl]
    operational_reason: str | None = None

    @property
    def closable(self) -> bool:
        return self.exit is not None


def evaluate_exit(position: Position, bar: Candle, *, is_eod_fill: bool) -> ExitFill | None:
    """한 결정봉의 보수적 청산 우선순위."""
    minute = bar.ts_exchange.hour * 60 + bar.ts_exchange.minute
    if is_eod_fill:
        if bar.open <= position.stop:
            return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "stop")
        if bar.low <= position.stop:
            return ExitFill(position.symbol, position.day, bar.ts_utc, minute, position.stop, "stop")
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "eod")
    if bar.open <= position.stop:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "stop")
    if bar.open >= position.target:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "target")
    if bar.low <= position.stop:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, position.stop, "stop")
    if bar.high >= position.target:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, position.target, "target")
    return None


def close_trade(
    contract: Mapping[str, object],
    position: Position,
    exit_fill: ExitFill,
    *,
    operational_reason: str | None = None,
) -> TradeRecord:
    return TradeRecord(
        position=position,
        exit=exit_fill,
        costs=all_costed_pnls(
            contract,
            entry_price=position.entry_price,
            exit_price=exit_fill.exit_price,
            qty=position.qty,
        ),
        operational_reason=operational_reason,
    )


def unclosable_trade(position: Position) -> TradeRecord:
    return TradeRecord(position=position, exit=None, costs={}, operational_reason="unclosable")
