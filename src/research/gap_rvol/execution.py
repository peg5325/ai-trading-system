"""Conservative Gap+RVOL stop, target, scheduled-time, stale, and EOD exits."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from src.models import Candle
from src.research.gap_rvol.portfolio import Position
from src.research.orb.costs import CostedPnl, all_costed_pnls
from src.research.orb.windows import minute_of_day


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


def evaluate_exit(
    position: Position,
    bar: Candle,
    *,
    stale: bool = False,
    eod: bool = False,
    time_due: bool = False,
) -> ExitFill | None:
    minute = minute_of_day(bar)
    if bar.open <= position.stop:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "stop")
    if bar.open >= position.target:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "target")
    if stale:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "stale_exit")
    if eod:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "eod")
    if time_due:
        return ExitFill(position.symbol, position.day, bar.ts_utc, minute, bar.open, "time")
    if bar.low <= position.stop:
        return ExitFill(
            position.symbol, position.day, bar.ts_utc, minute, position.stop, "stop")
    if bar.high >= position.target:
        return ExitFill(
            position.symbol, position.day, bar.ts_utc, minute, position.target, "target")
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
    return TradeRecord(position, None, {}, "unclosable")
