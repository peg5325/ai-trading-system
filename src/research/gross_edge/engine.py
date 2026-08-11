"""Pure candidate extraction for the frozen gross-edge screen (#124)."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from src.history import exchange_calendar as cal
from src.models import SESSION_REGULAR, Candle
from src.research.orb.windows import previous_trading_day, regular_close_minute_checked


class DataDefectError(RuntimeError):
    """Ambiguous canonical input that must not be silently resolved."""


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    signal_close_minute: int | None
    entry_minute: int
    direction_multiplier: int
    uses_previous_close: bool


@dataclass(frozen=True)
class GrossTrade:
    candidate_id: str
    symbol: str
    day: date
    gross_return_pct_points: float


def candidate_specs(contract: Mapping[str, object]) -> tuple[CandidateSpec, ...]:
    minute_by_time = {"09:35": 575, "10:00": 600, "10:30": 630, "12:00": 720}
    close_by_signal = {
        "open_0930_minus_previous_regular_close": None,
        "close_0959_minus_open_0930": 599,
        "close_1029_minus_open_0930": 629,
        "close_1159_minus_open_0930": 719,
    }
    out: list[CandidateSpec] = []
    for item in contract["candidates"]:  # type: ignore[index]
        signal = str(item["signal"])
        entry = str(item["entry_et"])
        if signal not in close_by_signal or entry not in minute_by_time:
            raise ValueError(f"unsupported frozen candidate timing: {item!r}")
        out.append(
            CandidateSpec(
                candidate_id=str(item["id"]),
                signal_close_minute=close_by_signal[signal],
                entry_minute=minute_by_time[entry],
                direction_multiplier=1 if item["direction"] == "same" else -1,
                uses_previous_close=signal == "open_0930_minus_previous_regular_close",
            )
        )
    return tuple(out)


def group_regular_by_day(candles: Sequence[Candle]) -> dict[date, tuple[Candle, ...]]:
    grouped: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        if candle.session == SESSION_REGULAR:
            grouped[candle.ts_exchange.date()].append(candle)
    return {
        day: tuple(sorted(items, key=lambda candle: candle.ts_utc))
        for day, items in grouped.items()
    }


def _minute_map(candles: Sequence[Candle], expected_day: date) -> dict[int, Candle]:
    by_minute: dict[int, Candle] = {}
    for candle in candles:
        if candle.ts_exchange.date() != expected_day:
            raise DataDefectError("cross-day candle passed to one-day extractor")
        minute = candle.ts_exchange.hour * 60 + candle.ts_exchange.minute
        if minute in by_minute:
            raise DataDefectError(
                f"duplicate regular minute {expected_day} {minute} for {candle.symbol}"
            )
        by_minute[minute] = candle
    return by_minute


def extract_symbol_day(
    *,
    symbol: str,
    day: date,
    current: Sequence[Candle],
    previous: Sequence[Candle],
    specs: Sequence[CandidateSpec],
) -> tuple[GrossTrade, ...]:
    """Extract all fixed candidates from one symbol-day without future signal use."""
    if cal.is_early_close(day):
        return ()
    expected_previous = previous_trading_day(day)
    current_by_minute = _minute_map(current, day)
    previous_by_minute = _minute_map(previous, expected_previous)
    open_candle = current_by_minute.get(570)
    exit_candle = current_by_minute.get(955)
    previous_close = previous_by_minute.get(
        regular_close_minute_checked(expected_previous) - 1
    )
    if open_candle is None or exit_candle is None or previous_close is None:
        return ()

    out: list[GrossTrade] = []
    for spec in specs:
        entry = current_by_minute.get(spec.entry_minute)
        if entry is None or entry.open <= 0:
            continue
        if spec.uses_previous_close:
            signal = open_candle.open - previous_close.close
        else:
            signal_candle = current_by_minute.get(spec.signal_close_minute or -1)
            if signal_candle is None:
                continue
            signal = signal_candle.close - open_candle.open
        if signal == 0:
            continue
        direction = (1 if signal > 0 else -1) * spec.direction_multiplier
        gross = direction * (exit_candle.open / entry.open - 1.0) * 100.0
        out.append(
            GrossTrade(
                candidate_id=spec.candidate_id,
                symbol=symbol,
                day=day,
                gross_return_pct_points=gross,
            )
        )
    return tuple(out)
