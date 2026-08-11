"""Swing OOS를 열지 않는 P0 섹터 ETF 일봉 로더."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from src.history.oos_guard import TRACK_SWING, research_load
from src.models import ET, Candle

from .contract import STRATEGY_ID


def sector_symbols(universe_path: str | Path) -> tuple[str, ...]:
    with Path(universe_path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return tuple(sorted(
        entry["symbol"]
        for entry in document["symbols"]
        if entry.get("class") == "sector_etf"
    ))


def _et_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=ET)


def load_development_bars(
    db_path: str | Path,
    *,
    symbols: tuple[str, ...],
    calendar_symbol: str,
    start: date,
    end_inclusive: date,
) -> dict[str, list[Candle]]:
    """고정 development 반개구간만 performance 권한으로 읽는다."""
    start_ts = _et_midnight(start)
    end_ts = _et_midnight(end_inclusive + timedelta(days=1))
    requested = tuple(sorted(set(symbols) | {calendar_symbol}))
    return {
        symbol: research_load(
            str(db_path),
            symbol,
            "1d",
            start=start_ts,
            end=end_ts,
            purpose="performance",
            track=TRACK_SWING,
            strategy_id=STRATEGY_ID,
            market="US",
        )
        for symbol in requested
    }
