"""Conflict-checked D9-only overlay for Toss dependency repair candidates."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date

from src.models import Candle
from src.research.gap_rvol.loading import load_symbol_day


def dependency_overlay_loader(
    *, candidate_db: str, eligible_targets: tuple[tuple[str, str], ...]
) -> Callable[[str, str, date], tuple[Candle, ...]]:
    """Add missing candidate minutes without overwriting frozen candle values."""
    eligible = set(eligible_targets)

    def load(snapshot_db: str, symbol: str, day: date) -> tuple[Candle, ...]:
        frozen = load_symbol_day(snapshot_db, symbol, day)
        if (symbol, day.isoformat()) not in eligible:
            return frozen
        candidate = load_symbol_day(candidate_db, symbol, day)
        if not candidate:
            raise ValueError(f"eligible dependency candidate is empty: {symbol} {day}")
        merged = {candle.ts_utc: candle for candle in frozen}
        for candle in candidate:
            existing = merged.get(candle.ts_utc)
            if existing is not None and existing != candle:
                raise ValueError(
                    f"dependency overlay conflicts with frozen candle: "
                    f"{symbol} {candle.ts_utc.isoformat()}"
                )
            merged[candle.ts_utc] = candle
        return tuple(merged[key] for key in sorted(merged))

    return load
