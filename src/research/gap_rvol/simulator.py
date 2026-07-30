"""Pure event-time Gap+RVOL day simulator.

The simulator receives already guarded, precomputed symbol-day inputs.  It has
no database or snapshot access and therefore cannot open blind OOS itself.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from src.models import SESSION_POST, SESSION_PRE, SESSION_REGULAR, Candle
from src.research.gap_rvol.contract import SpecParams, require
from src.research.gap_rvol.execution import (
    ExitFill,
    TradeRecord,
    close_trade,
    evaluate_exit,
    unclosable_trade,
)
from src.research.gap_rvol.features import GapRvolDayFeatures
from src.research.gap_rvol.portfolio import (
    Position,
    SkippedEntry,
    allocate_entries,
)
from src.research.gap_rvol.signals import GapRvolSignal, detect_signal
from src.research.orb.windows import minute_of_day, regular_close_minute_checked


@dataclass(frozen=True)
class DaySimulationResult:
    day: date
    spec_name: str
    trades: tuple[TradeRecord, ...]
    skipped: tuple[SkippedEntry, ...]
    excluded: tuple[tuple[str, str], ...]
    filtered: tuple[tuple[str, str], ...]
    major_defects: tuple[tuple[str, str], ...]
    open_positions_at_close: int
    fill_order: tuple[str, str] = ("exits", "entries")


def _bar_key(bar: Candle) -> tuple[object, ...]:
    return (
        bar.symbol, bar.ts_utc, bar.open, bar.high, bar.low, bar.close,
        bar.volume, bar.session, bar.interval,
    )


def canonical_decision_bars(
    bars: Sequence[Candle], *, symbol: str, day: date
) -> tuple[Candle, ...]:
    by_minute: dict[int, Candle] = {}
    close_minute = regular_close_minute_checked(day)
    for bar in bars:
        if bar.symbol != symbol or bar.ts_exchange.date() != day:
            raise ValueError("decision bar symbol/day mismatch")
        minute = minute_of_day(bar)
        if bar.session != SESSION_REGULAR or minute >= close_minute:
            raise ValueError("decision bars must be pre-close REGULAR bars")
        previous = by_minute.get(minute)
        if previous is not None and _bar_key(previous) != _bar_key(bar):
            raise ValueError(f"conflicting decision bar at {symbol} {day} {minute}")
        by_minute[minute] = bar
    return tuple(by_minute[minute] for minute in sorted(by_minute))


def detect_data_defects(candles: Sequence[Candle]) -> tuple[str, ...]:
    reasons: set[str] = set()
    if any(
        not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high)
        for bar in candles
    ):
        reasons.add("ohlc_invariant_violation")
    if any(bar.session not in {SESSION_PRE, SESSION_REGULAR, SESSION_POST} for bar in candles):
        reasons.add("session_label_violation")
    regular = sorted(
        (bar for bar in candles if bar.session == SESSION_REGULAR),
        key=lambda bar: bar.ts_utc,
    )
    for previous, current in pairwise(regular):
        if (
            previous.ts_exchange.date() == current.ts_exchange.date()
            and previous.close
            and abs(current.close / previous.close - 1) > 0.5
        ):
            reasons.add("unexplained_1m_price_jump_gt_50pct")
    return tuple(sorted(reasons))


def _open_exit(position: Position, bar: Candle, reason: str) -> ExitFill:
    return ExitFill(
        position.symbol,
        position.day,
        bar.ts_utc,
        minute_of_day(bar),
        bar.open,
        reason,
    )


def simulate_day(
    *,
    day: date,
    spec: SpecParams,
    decision_bars_by_symbol: Mapping[str, Sequence[Candle]],
    features_by_symbol: Mapping[str, GapRvolDayFeatures],
    raw_defects_by_symbol: Mapping[str, Sequence[str]],
    contract: Mapping[str, object],
) -> DaySimulationResult:
    if set(raw_defects_by_symbol) != set(features_by_symbol):
        raise ValueError("raw defect evidence must match the feature symbol set")
    decision_minutes = int(require(contract, "signal", "decision_bar_minutes"))
    observation_start = _minute(require(contract, "signal", "observation_start_et"))
    latest_signal = _minute(require(contract, "signal", "latest_signal_bar_start_et"))
    latest_fill = _minute(require(contract, "signal", "latest_fill_bar_start_et"))
    close_minute = regular_close_minute_checked(day)
    eod_decision = close_minute - int(
        require(contract, "risk", "eod_flatten_minutes_before_close"))
    stale_limit = int(require(contract, "risk", "stale_bars_to_force_exit"))
    time_stop = int(require(contract, "risk", "time_stop_minutes"))

    excluded: list[tuple[str, str]] = []
    filtered: list[tuple[str, str]] = []
    major: list[tuple[str, str]] = []
    bars: dict[str, tuple[Candle, ...]] = {}
    signals: dict[str, GapRvolSignal] = {}
    for symbol in sorted(features_by_symbol):
        feature = features_by_symbol[symbol]
        excluded.extend((symbol, reason) for reason in feature.excluded_reasons)
        defects = tuple(sorted(set(raw_defects_by_symbol[symbol])))
        if defects:
            major.extend((symbol, reason) for reason in defects)
            bars[symbol] = ()
            continue
        canonical = canonical_decision_bars(
            decision_bars_by_symbol.get(symbol, ()), symbol=symbol, day=day)
        bars[symbol] = canonical
        scan = detect_signal(
            features=feature,
            bars=canonical,
            spec=spec,
            decision_minutes=decision_minutes,
            observation_start_minute=observation_start,
            latest_signal_minute=latest_signal,
            latest_fill_minute=latest_fill,
        )
        if scan.signal is not None:
            signals[symbol] = scan.signal
        elif scan.filter_reason is not None:
            filtered.append((symbol, scan.filter_reason))

    by_symbol_minute = {
        symbol: {minute_of_day(bar): bar for bar in symbol_bars}
        for symbol, symbol_bars in bars.items()
    }
    event_minutes = sorted({
        minute
        for symbol_bars in by_symbol_minute.values()
        for minute in symbol_bars
    })
    positions: dict[str, Position] = {}
    last_seen: dict[str, int] = {}
    traded_symbols: set[str] = set()
    trades: list[TradeRecord] = []
    skipped: list[SkippedEntry] = []
    pending = dict(signals)

    for minute in event_minutes:
        bars_now = {
            symbol: symbol_bars[minute]
            for symbol, symbol_bars in by_symbol_minute.items()
            if minute in symbol_bars
        }
        # Exits always release capacity before entries at the same timestamp.
        for symbol in sorted(positions):
            bar = bars_now.get(symbol)
            if bar is None:
                continue
            position = positions[symbol]
            missing = max(0, (minute - last_seen[symbol]) // decision_minutes - 1)
            eod = minute >= eod_decision + decision_minutes
            time_due = minute >= position.entry_minute + time_stop
            fill = evaluate_exit(
                position,
                bar,
                stale=missing >= stale_limit,
                eod=eod,
                time_due=time_due,
            )
            if fill is None:
                last_seen[symbol] = minute
                continue
            operational = fill.exit_reason if fill.exit_reason == "stale_exit" else None
            trades.append(close_trade(
                contract, position, fill, operational_reason=operational))
            del positions[symbol]

        for symbol in sorted(pending):
            if pending[symbol].fill_minute < minute:
                signal = pending.pop(symbol)
                skipped.append(SkippedEntry(
                    symbol, day, signal.signal_minute,
                    signal.fill_minute, "signal_expired"))
                traded_symbols.add(symbol)

        ready = [
            signal for signal in pending.values()
            if signal.fill_minute == minute
        ]
        accepted, entry_skips = allocate_entries(
            signals=ready,
            bars_by_symbol=bars_now,
            positions=positions,
            traded_symbols=traded_symbols,
            contract=contract,
            fill_minute=minute,
        )
        skipped.extend(entry_skips)
        for signal in ready:
            pending.pop(signal.symbol, None)
        for position in accepted:
            positions[position.symbol] = position
            last_seen[position.symbol] = minute
            # Entry bar is scanned immediately for stop/target.
            fill = evaluate_exit(position, bars_now[position.symbol])
            if fill is not None:
                trades.append(close_trade(contract, position, fill))
                del positions[position.symbol]

    for signal in pending.values():
        skipped.append(SkippedEntry(
            signal.symbol, day, signal.signal_minute,
            signal.fill_minute, "signal_expired"))

    # Missing scheduled EOD fill: use the last post-entry close, otherwise
    # preserve an unclosable operational defect.
    for symbol, position in sorted(positions.items()):
        later = [
            bar for bar in bars[symbol]
            if minute_of_day(bar) > position.entry_minute
        ]
        if later:
            last = later[-1]
            fill = ExitFill(
                symbol, day, last.ts_utc, minute_of_day(last),
                last.close, "eod_fallback")
            trades.append(close_trade(
                contract, position, fill, operational_reason="eod_fallback"))
        else:
            trades.append(unclosable_trade(position))

    return DaySimulationResult(
        day=day,
        spec_name=spec.name,
        trades=tuple(trades),
        skipped=tuple(sorted(
            skipped, key=lambda item: (item.fill_minute, item.symbol, item.reason))),
        excluded=tuple(sorted(set(excluded))),
        filtered=tuple(sorted(set(filtered))),
        major_defects=tuple(sorted(set(major))),
        open_positions_at_close=0,
    )


def _minute(value: object) -> int:
    hour, minute = str(value).split(":")
    return int(hour) * 60 + int(minute)
