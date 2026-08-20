"""Blind-OOS-safe daily loader for box/order-block research."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from src.history.oos_guard import TRACK_SWING, research_load
from src.models import ET, Candle
from src.research.box_orderblock_retest.contract import STRATEGY_ID


def official_symbols(universe_path: str | Path) -> tuple[str, ...]:
    with Path(universe_path).open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    excluded = {"leveraged_etf", "inverse_etf"}
    return tuple(
        entry["symbol"]
        for entry in document["symbols"]
        if entry.get("class") not in excluded
    )


def _et_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=ET)


def load_development_bars(
    db_path: str | Path,
    *,
    symbols: tuple[str, ...],
    start: date,
    end_inclusive: date,
) -> dict[str, list[Candle]]:
    start_ts = _et_midnight(start)
    end_ts = _et_midnight(end_inclusive + timedelta(days=1))
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
        for symbol in symbols
    }
