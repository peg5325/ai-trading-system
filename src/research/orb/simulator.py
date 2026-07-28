"""ORB 독립 event-time 당일 시뮬레이터 — 4-B.

실데이터를 스스로 여는 runner가 아니다. 4-A의 guarded loader/feature pass가
만든 종목-일 입력을 받아 하루를 결정론적으로 계산한다. Development/OOS
실행은 4-C/5에서 별도 승인한다.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from src.models import (
    SESSION_POST,
    SESSION_PRE,
    SESSION_REGULAR,
    Candle,
)
from src.research.orb.contract import (
    SpecParams,
    _require,
    all_specs,
    decision_bar_spec,
    rvol_lookback,
    universe,
)
from src.research.orb.execution import (
    ExitFill,
    TradeRecord,
    close_trade,
    evaluate_exit,
    unclosable_trade,
)
from src.research.orb.features import (
    RvolWindow,
    SymbolDayFeatures,
    compute_symbol_day_features,
)
from src.research.orb.loading import decision_bars, load_symbol_day, regular_1m
from src.research.orb.portfolio import Position, SkippedEntry, allocate_entries
from src.research.orb.signals import OrbSignal, detect_breakout
from src.research.orb.windows import (
    development_data_days,
    development_eval_days,
    minute_of_day,
    previous_trading_day,
    regular_close_minute_checked,
)


@dataclass(frozen=True)
class DaySimulationResult:
    day: date
    spec_name: str
    trades: tuple[TradeRecord, ...]
    skipped: tuple[SkippedEntry, ...]
    excluded: tuple[tuple[str, str], ...]
    open_positions_at_close: int
    fill_order: tuple[str, str] = ("exits", "entries")


@dataclass(frozen=True)
class FeaturePass:
    days: tuple[date, ...]
    symbols: tuple[str, ...]
    by_opening_range_minutes: Mapping[
        int, Mapping[date, Mapping[str, SymbolDayFeatures]]
    ]
    day_candles_by_day: Mapping[date, Mapping[str, tuple[Candle, ...]]]
    regular_1m_by_day: Mapping[date, Mapping[str, tuple[Candle, ...]]]
    defects_by_day: Mapping[date, Mapping[str, tuple[str, ...]]]


def _bar_key(bar: Candle) -> tuple[object, ...]:
    return (
        bar.symbol, bar.ts_utc, bar.open, bar.high, bar.low, bar.close,
        bar.volume, bar.session, bar.interval,
    )


def canonical_decision_bars(
    bars: Sequence[Candle], *, symbol: str, day: date
) -> tuple[Candle, ...]:
    """입력 순서와 동일 중복에 불변, 값 충돌 중복은 fail-closed."""
    by_minute: dict[int, Candle] = {}
    close_minute = regular_close_minute_checked(day)
    for bar in bars:
        if bar.symbol != symbol or bar.ts_exchange.date() != day:
            raise ValueError("결정봉 symbol/day가 호출 범위와 다르다")
        minute = minute_of_day(bar)
        if bar.session != SESSION_REGULAR or minute >= close_minute:
            raise ValueError(
                "시뮬레이터 결정봉은 해당 거래일의 REGULAR 마감 전 봉이어야 한다")
        old = by_minute.get(minute)
        if old is not None and _bar_key(old) != _bar_key(bar):
            raise ValueError(f"{symbol} {day} {minute}:00 결정봉 값 충돌")
        by_minute[minute] = bar
    return tuple(by_minute[m] for m in sorted(by_minute))


def _ohlc_valid(bar: Candle) -> bool:
    return (
        bar.low <= min(bar.open, bar.close)
        <= max(bar.open, bar.close) <= bar.high
    )


def detect_data_defects(candles: Sequence[Candle]) -> tuple[str, ...]:
    """§11.2에서 원시 봉으로 검출 가능한 중대 결함 3종."""
    reasons: set[str] = set()
    if any(not _ohlc_valid(bar) for bar in candles):
        reasons.add("ohlc_invariant_violation")
    allowed_sessions = frozenset({SESSION_PRE, SESSION_REGULAR, SESSION_POST})
    if any(bar.session not in allowed_sessions for bar in candles):
        reasons.add("session_label_violation")
    regular = sorted(
        (bar for bar in candles if bar.session == "REGULAR"),
        key=lambda bar: bar.ts_utc,
    )
    for previous, current in pairwise(regular):
        if (
            previous.ts_exchange.date() == current.ts_exchange.date()
            and previous.close != 0
            and abs(current.close / previous.close - 1) > .5
        ):
            reasons.add("unexplained_1m_price_jump_gt_50pct")
    return tuple(sorted(reasons))


def _exit_at_open(position: Position, bar: Candle, reason: str) -> ExitFill:
    return ExitFill(
        symbol=position.symbol,
        day=position.day,
        exit_bar_ts_utc=bar.ts_utc,
        exit_minute=minute_of_day(bar),
        exit_price=bar.open,
        exit_reason=reason,
    )


def simulate_day(
    *,
    day: date,
    spec: SpecParams,
    decision_bars_by_symbol: Mapping[str, Sequence[Candle]],
    features_by_symbol: Mapping[str, SymbolDayFeatures],
    contract: Mapping[str, object],
) -> DaySimulationResult:
    """한 거래일·한 variant의 독립 포트폴리오를 §7 순서로 계산한다."""
    close_minute = regular_close_minute_checked(day)
    decision_minutes = int(_require(contract, "spec", "decision_bar_minutes"))
    eod_before = int(_require(contract, "risk", "eod_flatten_minutes_before_close"))
    eod_decision_minute = close_minute - eod_before
    eod_fill_minute = eod_decision_minute + decision_minutes
    stale_limit = int(_require(contract, "risk", "stale_bars_to_force_exit"))

    bars: dict[str, tuple[Candle, ...]] = {}
    excluded: list[tuple[str, str]] = []
    for symbol in sorted(features_by_symbol):
        feature = features_by_symbol[symbol]
        excluded.extend((symbol, reason) for reason in feature.excluded_reasons)
        canonical = canonical_decision_bars(
            decision_bars_by_symbol.get(symbol, ()), symbol=symbol, day=day)
        if not canonical:
            excluded.append((symbol, "no_regular_data"))
            bars[symbol] = ()
            continue
        if any(not _ohlc_valid(bar) for bar in canonical):
            excluded.append((symbol, "ohlc_invariant_violation"))
            bars[symbol] = ()
            continue
        bars[symbol] = canonical

    by_symbol_minute = {
        symbol: {minute_of_day(bar): bar for bar in symbol_bars}
        for symbol, symbol_bars in bars.items()
    }
    event_minutes = sorted({
        minute for symbol_bars in by_symbol_minute.values() for minute in symbol_bars
    })

    positions: dict[str, Position] = {}
    last_seen_minute: dict[str, int] = {}
    pending: dict[str, OrbSignal] = {}
    traded_symbols: set[str] = set()
    terminal_symbols: set[str] = {symbol for symbol, _ in excluded}
    trades: list[TradeRecord] = []
    skipped: list[SkippedEntry] = []

    for minute in event_minutes:
        bars_now = {
            symbol: symbol_bars[minute]
            for symbol, symbol_bars in by_symbol_minute.items()
            if minute in symbol_bars
        }

        # 1) 기존 포지션 청산. stale는 다음 실제 봉 open에서 최우선이다.
        for symbol in sorted(positions):
            bar = bars_now.get(symbol)
            if bar is None:
                continue
            position = positions[symbol]
            previous = last_seen_minute[symbol]
            missing = max(0, (minute - previous) // decision_minutes - 1)
            if missing >= stale_limit:
                fill = _exit_at_open(position, bar, "stale_exit")
                trades.append(close_trade(
                    contract, position, fill, operational_reason="stale_exit"))
                del positions[symbol]
                continue
            fill = evaluate_exit(
                position, bar, is_eod_fill=(minute == eod_fill_minute))
            if fill is not None:
                trades.append(close_trade(contract, position, fill))
                del positions[symbol]
            else:
                last_seen_minute[symbol] = minute

        # 신호의 바로 다음 예정봉이 없으면 나중 봉으로 늦춰 채우지 않는다.
        for symbol in sorted(pending):
            if pending[symbol].fill_minute < minute:
                signal = pending.pop(symbol)
                skipped.append(SkippedEntry(
                    symbol, day, signal.signal_minute, signal.fill_minute,
                    "signal_expired"))
                traded_symbols.add(symbol)
                terminal_symbols.add(symbol)

        # 2) 현재 분에 정확히 도착한 진입 체결.
        ready = [signal for signal in pending.values() if signal.fill_minute == minute]
        accepted, entry_skips = allocate_entries(
            signals=ready,
            bars_by_symbol=bars_now,
            positions=positions,
            traded_symbols=traded_symbols,
            contract=contract,
            fill_minute=minute,
            close_minute=close_minute,
        )
        skipped.extend(entry_skips)
        for signal in ready:
            pending.pop(signal.symbol, None)
        for skip in entry_skips:
            terminal_symbols.add(skip.symbol)
        for position in accepted:
            positions[position.symbol] = position
            last_seen_minute[position.symbol] = minute

        # 3) 진입 봉도 stop/target 스캔.
        for position in accepted:
            bar = bars_now[position.symbol]
            fill = evaluate_exit(position, bar, is_eod_fill=(minute == eod_fill_minute))
            if fill is not None:
                trades.append(close_trade(contract, position, fill))
                del positions[position.symbol]

        # 4) 완료봉 close의 돌파를 다음 예정봉 대기로 보낸다.
        for symbol in sorted(bars_now):
            if (
                symbol in positions or symbol in pending
                or symbol in traded_symbols or symbol in terminal_symbols
            ):
                continue
            signal = detect_breakout(
                features_by_symbol[symbol],
                bars_now[symbol],
                spec,
                decision_bar_minutes=decision_minutes,
            )
            if signal is not None:
                pending[symbol] = signal

    # 더 이상 실제 봉이 없으므로 남은 pending은 전부 만료.
    for signal in pending.values():
        skipped.append(SkippedEntry(
            signal.symbol, day, signal.signal_minute, signal.fill_minute,
            "signal_expired"))

    # 정상 EOD 체결봉 부재 시 마지막 실제 REGULAR 결정봉 close fallback.
    for symbol in sorted(positions):
        position = positions.pop(symbol)
        after_entry = [
            bar for bar in bars[symbol]
            if position.entry_bar_ts_utc < bar.ts_utc and minute_of_day(bar) < close_minute
        ]
        if not after_entry:
            trades.append(unclosable_trade(position))
            continue
        last = after_entry[-1]
        fill = ExitFill(
            symbol=symbol,
            day=day,
            exit_bar_ts_utc=last.ts_utc,
            exit_minute=minute_of_day(last),
            exit_price=last.close,
            exit_reason="eod_fallback",
        )
        trades.append(close_trade(
            contract, position, fill, operational_reason="eod_fallback"))

    if positions:
        raise AssertionError("정규장 마감 뒤 open position이 남았다")
    return DaySimulationResult(
        day=day,
        spec_name=spec.name,
        trades=tuple(trades),
        skipped=tuple(skipped),
        excluded=tuple(sorted(excluded)),
        open_positions_at_close=0,
    )


def build_development_feature_pass(
    *,
    contract: Mapping[str, object],
    db_path: str,
    loader: Callable[[str, str, date], tuple[Candle, ...]] = load_symbol_day,
) -> FeaturePass:
    """690 거래일 × 30종목을 빠짐없이 순회하는 guarded feature pass.

    4-B 테스트는 spy loader로 호출 집합과 OOS 미접근을 검증한다. 실 DB
    호출은 4-C의 P1~P3 승인 뒤에만 한다.
    """
    days = development_data_days(contract)
    symbols = tuple(sorted(universe(contract).all_symbols))
    lookback = rvol_lookback(contract)
    opening_minutes = tuple(sorted({
        spec.opening_range_minutes for spec in all_specs(contract)
    }))
    windows = {
        minutes: {
            symbol: RvolWindow(
                lookback_days=lookback.lookback_trading_days,
                min_valid_days=lookback.min_valid_lookback_days,
                minutes=minutes,
            )
            for symbol in symbols
        }
        for minutes in opening_minutes
    }
    features: dict[int, dict[date, dict[str, SymbolDayFeatures]]] = {
        minutes: {} for minutes in opening_minutes
    }
    day_candles_cache: dict[date, dict[str, tuple[Candle, ...]]] = {}
    regular_cache: dict[date, dict[str, tuple[Candle, ...]]] = {}
    defects: dict[date, dict[str, tuple[str, ...]]] = {}

    first_prev = previous_trading_day(days[0])
    previous_regular = {
        symbol: regular_1m(loader(db_path, symbol, first_prev))
        for symbol in symbols
    }
    for day in days:
        day_candles_cache[day] = {}
        regular_cache[day] = {}
        defects[day] = {}
        for minutes in opening_minutes:
            features[minutes][day] = {}
        for symbol in symbols:
            day_candles = loader(db_path, symbol, day)
            day_regular = regular_1m(day_candles)
            day_candles_cache[day][symbol] = day_candles
            regular_cache[day][symbol] = day_regular
            defects[day][symbol] = detect_data_defects(day_candles)
            prev_day = previous_trading_day(day)
            for minutes in opening_minutes:
                features[minutes][day][symbol] = compute_symbol_day_features(
                    symbol=symbol,
                    day=day,
                    today_day_candles=day_candles,
                    today_regular_1m=day_regular,
                    prev_day=prev_day,
                    prev_day_regular_1m=previous_regular[symbol],
                    opening_range_minutes=minutes,
                    rvol_window=windows[minutes][symbol],
                )
            previous_regular[symbol] = day_regular
    return FeaturePass(
        days=days,
        symbols=symbols,
        by_opening_range_minutes=features,
        day_candles_by_day=day_candles_cache,
        regular_1m_by_day=regular_cache,
        defects_by_day=defects,
    )


def simulate_all_specs(
    *,
    contract: Mapping[str, object],
    feature_pass: FeaturePass,
) -> Mapping[str, tuple[DaySimulationResult, ...]]:
    """Development 평가일을 8개 독립 포트폴리오로 계산하는 순수 진입점.

    이 함수를 호출하는 실제 runner는 4-C에서만 작성·실행한다.
    """
    bar_spec = decision_bar_spec(contract)
    eval_days = development_eval_days(contract)
    output: dict[str, tuple[DaySimulationResult, ...]] = {}
    for spec in all_specs(contract):
        per_day: list[DaySimulationResult] = []
        for day in eval_days:
            bars_by_symbol = {
                symbol: decision_bars(
                    feature_pass.day_candles_by_day[day][symbol],
                    day,
                    spec=bar_spec,
                )
                for symbol in feature_pass.symbols
            }
            per_day.append(simulate_day(
                day=day,
                spec=spec,
                decision_bars_by_symbol=bars_by_symbol,
                features_by_symbol=(
                    feature_pass.by_opening_range_minutes[
                        spec.opening_range_minutes][day]
                ),
                contract=contract,
            ))
        output[spec.name] = tuple(per_day)
    return output
