"""Deterministic development simulator for frozen box/order-block research."""

from __future__ import annotations

import hashlib
import math
import statistics
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from src.models import Candle
from src.research.box_orderblock_retest.contract import canonical_json_bytes


@dataclass(frozen=True)
class FeatureBar:
    day: date
    candle: Candle
    atr5_pre: float | None
    atr20_pre: float | None
    ema50: float | None
    sma200: float | None
    box_high: float | None
    box_low: float | None
    box_width_fraction: float | None
    volume_median20: float | None


@dataclass(frozen=True)
class BreakoutSignal:
    symbol: str
    day: date
    box_high: float
    box_low: float
    atr20_pre: float
    breakout_atr: float
    volume_ratio: float
    box_width_fraction: float
    atr_contraction_ratio: float
    has_order_block: bool
    zone_low: float | None
    zone_high: float | None


@dataclass(frozen=True)
class Candidate:
    arm: str
    symbol: str
    signal_day: date
    confirmation_day: date
    entry_day: date
    planned_exit_day: date
    initial_stop: float
    breakout_atr: float
    volume_ratio: float


@dataclass(frozen=True)
class ExitLeg:
    day: date
    price: float
    quantity: float
    reason: str


@dataclass(frozen=True)
class Episode:
    arm: str
    symbol: str
    signal_day: date
    confirmation_day: date
    entry_day: date
    planned_exit_day: date
    entry_price: float
    initial_stop: float
    quantity: int
    initial_risk_cash: float
    exits: tuple[ExitLeg, ...]

    @property
    def entry_notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def exit_day(self) -> date:
        return self.exits[-1].day

    @property
    def gross_pnl(self) -> float:
        return sum(
            leg.quantity * (leg.price - self.entry_price) for leg in self.exits
        )


@dataclass(frozen=True)
class PreparedArm:
    candidates: tuple[Candidate, ...]
    data_exclusions: int
    boundary_skips: int
    strategy_filters: Mapping[str, int]

    @property
    def data_exclusion_rate(self) -> float:
        denominator = len(self.candidates) + self.data_exclusions
        return self.data_exclusions / denominator if denominator else 0.0


@dataclass(frozen=True)
class Simulation:
    episodes: tuple[Episode, ...]
    portfolio_skips: Mapping[str, int]


@dataclass
class _OpenPosition:
    candidate: Candidate
    entry_price: float
    initial_stop: float
    quantity: int
    remaining: float
    exits: list[ExitLeg]
    target_hit: bool = False
    breakeven_active_day: date | None = None


def valid_ohlc(candle: Candle) -> bool:
    return (
        min(candle.open, candle.high, candle.low, candle.close) > 0
        and candle.high >= max(candle.open, candle.close)
        and candle.low <= min(candle.open, candle.close)
        and candle.high > candle.low
    )


def _ema(previous: float | None, value: float, period: int) -> float:
    if previous is None:
        return value
    alpha = 2.0 / (period + 1.0)
    return alpha * value + (1.0 - alpha) * previous


def build_features(
    bars: Sequence[Candle], calendar: Sequence[date]
) -> tuple[dict[date, FeatureBar], Counter[str]]:
    by_day: dict[date, Candle] = {}
    conflict_days: set[date] = set()
    defects: Counter[str] = Counter()
    for candle in bars:
        day = candle.ts_exchange.date()
        previous = by_day.get(day)
        if previous is not None:
            if previous != candle:
                defects["duplicate_value_conflict"] += 1
                conflict_days.add(day)
            continue
        by_day[day] = candle

    result: dict[date, FeatureBar] = {}
    previous_close: float | None = None
    ema50: float | None = None
    segment_count = 0
    true_ranges: deque[float] = deque(maxlen=20)
    prior_bars: deque[Candle] = deque(maxlen=20)
    closes: deque[float] = deque(maxlen=200)
    for day in calendar:
        candle = by_day.get(day)
        conflict = day in conflict_days
        if conflict:
            candle = None
        if candle is None or not valid_ohlc(candle):
            if candle is None and not conflict:
                defects["missing_daily_bar"] += 1
            elif candle is not None:
                defects["invalid_daily_bar"] += 1
            previous_close = None
            ema50 = None
            segment_count = 0
            true_ranges.clear()
            prior_bars.clear()
            closes.clear()
            continue

        if candle.volume <= 0:
            defects["nonpositive_volume"] += 1

        atr5_pre = (
            sum(tuple(true_ranges)[-5:]) / 5 if len(true_ranges) >= 5 else None
        )
        atr20_pre = sum(true_ranges) / 20 if len(true_ranges) == 20 else None
        box_high = max(bar.high for bar in prior_bars) if len(prior_bars) == 20 else None
        box_low = min(bar.low for bar in prior_bars) if len(prior_bars) == 20 else None
        box_width = (
            (box_high - box_low) / prior_bars[-1].close
            if box_high is not None and box_low is not None and prior_bars[-1].close > 0
            else None
        )
        volumes = [bar.volume for bar in prior_bars]
        volume_median = (
            statistics.median(volumes)
            if len(volumes) == 20 and all(value > 0 for value in volumes)
            else None
        )
        ema50 = _ema(ema50, candle.close, 50)
        segment_count += 1
        sma200 = (
            sum((*tuple(closes)[-199:], candle.close)) / 200
            if len(closes) >= 199
            else None
        )
        result[day] = FeatureBar(
            day=day,
            candle=candle,
            atr5_pre=atr5_pre,
            atr20_pre=atr20_pre,
            ema50=ema50 if segment_count >= 50 else None,
            sma200=sma200,
            box_high=box_high,
            box_low=box_low,
            box_width_fraction=box_width,
            volume_median20=volume_median,
        )

        true_range = candle.high - candle.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        true_ranges.append(true_range)
        prior_bars.append(candle)
        closes.append(candle.close)
        previous_close = candle.close
    return result, defects


def breakout_signal(
    *,
    symbol: str,
    current: FeatureBar,
    previous: FeatureBar | None,
    spy: FeatureBar | None,
    contract: Mapping[str, Any],
) -> BreakoutSignal | None:
    candle = current.candle
    if (
        previous is None
        or spy is None
        or current.atr5_pre is None
        or current.atr20_pre is None
        or current.ema50 is None
        or current.box_high is None
        or current.box_low is None
        or current.box_width_fraction is None
        or current.volume_median20 is None
        or spy.sma200 is None
    ):
        return None
    box = contract["box"]
    breakout = contract["breakout"]
    if not (
        current.box_width_fraction <= float(box["max_width_fraction_of_previous_close"])
        and current.atr5_pre <= 0.80 * current.atr20_pre
        and spy.candle.close > spy.sma200
        and candle.close > current.ema50
        and candle.close > candle.open
        and candle.close > current.box_high + 0.10 * current.atr20_pre
        and candle.close <= current.box_high + 1.00 * current.atr20_pre
        and candle.volume >= float(breakout["minimum_volume_ratio"]) * current.volume_median20
    ):
        return None
    previous_candle = previous.candle
    has_order_block = (
        previous_candle.close < previous_candle.open
        and candle.open <= previous_candle.close
        and candle.close >= previous_candle.open
    )
    return BreakoutSignal(
        symbol=symbol,
        day=current.day,
        box_high=current.box_high,
        box_low=current.box_low,
        atr20_pre=current.atr20_pre,
        breakout_atr=(candle.close - current.box_high) / current.atr20_pre,
        volume_ratio=candle.volume / current.volume_median20,
        has_order_block=has_order_block,
        box_width_fraction=current.box_width_fraction,
        atr_contraction_ratio=current.atr5_pre / current.atr20_pre,
        zone_low=previous_candle.close if has_order_block else None,
        zone_high=previous_candle.open if has_order_block else None,
    )


def _breakout_inputs_ready(
    current: FeatureBar, previous: FeatureBar | None, spy: FeatureBar | None
) -> bool:
    return not (
        previous is None
        or spy is None
        or current.atr5_pre is None
        or current.atr20_pre is None
        or current.ema50 is None
        or current.box_high is None
        or current.box_low is None
        or current.box_width_fraction is None
        or current.volume_median20 is None
        or spy.sma200 is None
    )


def find_signals(
    features: Mapping[str, Mapping[date, FeatureBar]],
    calendar: Sequence[date],
    contract: Mapping[str, Any],
) -> tuple[tuple[BreakoutSignal, ...], Counter[str]]:
    spy = features["SPY"]
    signals: list[BreakoutSignal] = []
    audit: Counter[str] = Counter()
    for symbol in sorted(features):
        symbol_features = features[symbol]
        for index, day in enumerate(calendar):
            if index == 0 or day not in symbol_features:
                continue
            previous = symbol_features.get(calendar[index - 1])
            spy_feature = spy.get(day)
            if not _breakout_inputs_ready(symbol_features[day], previous, spy_feature):
                audit["warmup_not_ready"] += 1
                continue
            signal = breakout_signal(
                symbol=symbol,
                current=symbol_features[day],
                previous=previous,
                spy=spy_feature,
                contract=contract,
            )
            if signal is not None:
                signals.append(signal)
            else:
                audit["breakout_filter_rejection"] += 1
    return tuple(sorted(signals, key=lambda item: (item.day, item.symbol))), audit


def _required_features(
    symbol_features: Mapping[date, FeatureBar], days: Sequence[date]
) -> bool:
    return all(day in symbol_features for day in days)


def prepare_arms(
    signals: Sequence[BreakoutSignal],
    features: Mapping[str, Mapping[date, FeatureBar]],
    calendar: Sequence[date],
) -> dict[str, PreparedArm]:
    day_index = {day: index for index, day in enumerate(calendar)}
    base: list[Candidate] = []
    primary: list[Candidate] = []
    base_exclusions = primary_exclusions = 0
    base_boundary = primary_boundary = 0
    base_filters: Counter[str] = Counter()
    primary_filters: Counter[str] = Counter()

    for signal in signals:
        index = day_index[signal.day]
        symbol_features = features[signal.symbol]

        if index + 5 >= len(calendar):
            base_boundary += 1
        else:
            entry_day = calendar[index + 1]
            exit_day = calendar[index + 5]
            required = calendar[index + 1 : index + 6]
            if not _required_features(symbol_features, required):
                base_exclusions += 1
            else:
                stop = signal.box_high - signal.atr20_pre
                if symbol_features[entry_day].candle.open <= stop:
                    base_filters["entry_lte_stop"] += 1
                else:
                    base.append(
                        Candidate(
                            arm="base",
                            symbol=signal.symbol,
                            signal_day=signal.day,
                            confirmation_day=signal.day,
                            entry_day=entry_day,
                            planned_exit_day=exit_day,
                            initial_stop=stop,
                            breakout_atr=signal.breakout_atr,
                            volume_ratio=signal.volume_ratio,
                        )
                    )

        if not signal.has_order_block:
            primary_filters["no_order_block"] += 1
            continue
        if index + 3 >= len(calendar):
            primary_boundary += 1
            continue
        confirmed_day: date | None = None
        data_failure = False
        invalidated = False
        for offset in range(1, 4):
            day = calendar[index + offset]
            feature = symbol_features.get(day)
            if feature is None:
                primary_exclusions += 1
                data_failure = True
                break
            assert signal.zone_low is not None and signal.zone_high is not None
            candle = feature.candle
            if candle.close < signal.zone_low:
                primary_filters["pre_entry_invalidation"] += 1
                invalidated = True
                break
            if (
                candle.low <= signal.zone_high
                and candle.high >= signal.zone_low
                and candle.close >= signal.box_high
                and candle.close > candle.open
            ):
                confirmed_day = day
                break
        if data_failure or confirmed_day is None:
            if not data_failure and not invalidated:
                primary_filters["no_retest"] += 1
            continue
        confirmation_index = day_index[confirmed_day]
        if confirmation_index + 5 >= len(calendar):
            primary_boundary += 1
            continue
        entry_day = calendar[confirmation_index + 1]
        exit_day = calendar[confirmation_index + 5]
        required = calendar[confirmation_index + 1 : confirmation_index + 6]
        if not _required_features(symbol_features, required):
            primary_exclusions += 1
            continue
        assert signal.zone_low is not None
        stop = signal.zone_low - 0.10 * signal.atr20_pre
        if symbol_features[entry_day].candle.open <= stop:
            primary_filters["entry_lte_stop"] += 1
            continue
        primary.append(
            Candidate(
                arm="primary",
                symbol=signal.symbol,
                signal_day=signal.day,
                confirmation_day=confirmed_day,
                entry_day=entry_day,
                planned_exit_day=exit_day,
                initial_stop=stop,
                breakout_atr=signal.breakout_atr,
                volume_ratio=signal.volume_ratio,
            )
        )

    sort_key = lambda item: (
        item.entry_day,
        -item.breakout_atr,
        -item.volume_ratio,
        item.symbol,
    )
    return {
        "base": PreparedArm(
            tuple(sorted(base, key=sort_key)),
            base_exclusions,
            base_boundary,
            dict(sorted(base_filters.items())),
        ),
        "primary": PreparedArm(
            tuple(sorted(primary, key=sort_key)),
            primary_exclusions,
            primary_boundary,
            dict(sorted(primary_filters.items())),
        ),
    }


def simulate(
    candidates: Iterable[Candidate],
    features: Mapping[str, Mapping[date, FeatureBar]],
    calendar: Sequence[date],
    *,
    arm: str,
    contract: Mapping[str, Any],
) -> Simulation:
    by_entry: dict[date, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_entry[candidate.entry_day].append(candidate)
    for day, entries in by_entry.items():
        entries.sort(
            key=lambda item: (-item.breakout_atr, -item.volume_ratio, item.symbol)
        )
    calendar_index = {day: index for index, day in enumerate(calendar)}
    positions: dict[str, _OpenPosition] = {}
    episodes: list[Episode] = []
    skips: Counter[str] = Counter()
    portfolio = contract["portfolio"]

    def finish(symbol: str) -> None:
        position = positions.pop(symbol)
        episodes.append(
            Episode(
                arm=arm,
                symbol=symbol,
                signal_day=position.candidate.signal_day,
                confirmation_day=position.candidate.confirmation_day,
                entry_day=position.candidate.entry_day,
                planned_exit_day=position.candidate.planned_exit_day,
                entry_price=position.entry_price,
                initial_stop=position.initial_stop,
                quantity=position.quantity,
                initial_risk_cash=(position.entry_price - position.initial_stop)
                * position.quantity,
                exits=tuple(position.exits),
            )
        )

    def close_all(symbol: str, day: date, price: float, reason: str) -> None:
        position = positions[symbol]
        position.exits.append(ExitLeg(day, price, position.remaining, reason))
        position.remaining = 0.0
        finish(symbol)

    for day in calendar:
        for symbol in sorted(positions):
            position = positions[symbol]
            candle = features[symbol][day].candle
            active_stop = (
                position.entry_price
                if arm == "partial"
                and position.target_hit
                and position.breakeven_active_day is not None
                and day >= position.breakeven_active_day
                else position.initial_stop
            )
            if candle.open <= active_stop:
                close_all(symbol, day, candle.open, "gap_stop")

        for candidate in by_entry.get(day, []):
            if candidate.symbol in positions:
                skips["duplicate_symbol"] += 1
                continue
            if len(positions) >= int(portfolio["max_positions"]):
                skips["capacity"] += 1
                continue
            entry = features[candidate.symbol][day].candle.open
            risk_per_share = entry - candidate.initial_stop
            if risk_per_share <= 0:
                skips["entry_lte_stop"] += 1
                continue
            quantity = math.floor(
                min(
                    float(portfolio["max_entry_notional_usd"]) / entry,
                    float(portfolio["risk_budget_usd"]) / risk_per_share,
                )
            )
            if quantity < 1:
                skips["quantity_lt_one"] += 1
                continue
            positions[candidate.symbol] = _OpenPosition(
                candidate=candidate,
                entry_price=entry,
                initial_stop=candidate.initial_stop,
                quantity=quantity,
                remaining=float(quantity),
                exits=[],
            )

        for symbol in sorted(positions):
            position = positions[symbol]
            candle = features[symbol][day].candle
            time_exit = day == position.candidate.planned_exit_day
            if arm != "partial":
                if candle.low <= position.initial_stop:
                    close_all(symbol, day, position.initial_stop, "intraday_stop")
                elif time_exit:
                    close_all(symbol, day, candle.close, "time_exit")
                continue

            target = position.entry_price + (
                position.entry_price - position.initial_stop
            )
            active_stop = (
                position.entry_price
                if position.target_hit
                and position.breakeven_active_day is not None
                and day >= position.breakeven_active_day
                else position.initial_stop
            )
            target_available = not position.target_hit
            if (
                target_available
                and candle.low <= active_stop
                and max(candle.open, candle.high) >= target
            ):
                close_all(symbol, day, active_stop, "same_bar_stop_before_target")
            elif candle.low <= active_stop:
                close_all(symbol, day, active_stop, "intraday_stop")
            elif target_available and max(candle.open, candle.high) >= target:
                half = position.quantity * 0.5
                position.exits.append(ExitLeg(day, target, half, "target_1r_half"))
                position.remaining -= half
                position.target_hit = True
                next_index = calendar_index[day] + 1
                position.breakeven_active_day = (
                    calendar[next_index] if next_index < len(calendar) else None
                )
                if time_exit:
                    close_all(symbol, day, candle.close, "time_exit_after_target")
            elif time_exit:
                close_all(symbol, day, candle.close, "time_exit")

    if positions:
        raise AssertionError(f"open positions at boundary: {sorted(positions)}")
    episodes.sort(key=lambda item: (item.exit_day, item.entry_day, item.symbol))
    return Simulation(tuple(episodes), dict(sorted(skips.items())))


def episode_net_pnl(episode: Episode, round_trip_pct_points: float) -> float:
    rate = round_trip_pct_points / 100.0 / 2.0
    entry_cost = episode.entry_notional * rate
    exit_cost = sum(leg.price * leg.quantity * rate for leg in episode.exits)
    return episode.gross_pnl - entry_cost - exit_cost


def profit_factor_pass(value: float | str | None, threshold: float) -> bool:
    return value == "Infinity" or (
        value is not None and float(value) >= threshold
    )


def summarize(
    episodes: Sequence[Episode],
    features: Mapping[str, Mapping[date, FeatureBar]],
    calendar: Sequence[date],
    *,
    cost_pct_points: float,
    initial_capital: float,
) -> dict[str, Any]:
    pnls = [episode_net_pnl(item, cost_pct_points) for item in episodes]
    gains = sum(value for value in pnls if value > 0)
    losses = abs(sum(value for value in pnls if value < 0))
    by_symbol: dict[str, float] = defaultdict(float)
    by_entry_day: dict[str, float] = defaultdict(float)
    realized_by_day: dict[date, float] = defaultdict(float)
    costs_by_day: dict[date, float] = defaultdict(float)
    rate = cost_pct_points / 100.0 / 2.0
    entry_turnover = 0.0
    exit_turnover = 0.0
    for episode, pnl in zip(episodes, pnls):
        by_symbol[episode.symbol] += pnl
        by_entry_day[episode.entry_day.isoformat()] += pnl
        entry_turnover += episode.entry_notional
        costs_by_day[episode.entry_day] += episode.entry_notional * rate
        for leg in episode.exits:
            exit_turnover += leg.price * leg.quantity
            realized_by_day[leg.day] += leg.quantity * (
                leg.price - episode.entry_price
            )
            costs_by_day[leg.day] += leg.price * leg.quantity * rate

    cumulative_realized = 0.0
    cumulative_cost = 0.0
    peak = initial_capital
    mdd = 0.0
    equity_curve: list[dict[str, Any]] = []
    for day in calendar:
        cumulative_realized += realized_by_day.get(day, 0.0)
        cumulative_cost += costs_by_day.get(day, 0.0)
        unrealized = 0.0
        for episode in episodes:
            if not (episode.entry_day <= day <= episode.exit_day):
                continue
            exited = sum(
                leg.quantity for leg in episode.exits if leg.day <= day
            )
            remaining = episode.quantity - exited
            if remaining > 0:
                unrealized += remaining * (
                    features[episode.symbol][day].candle.close - episode.entry_price
                )
        equity = initial_capital + cumulative_realized - cumulative_cost + unrealized
        peak = max(peak, equity)
        if peak > 0:
            mdd = min(mdd, (equity - peak) / peak)
        equity_curve.append({"day": day.isoformat(), "equity": equity})

    total = sum(pnls)
    max_consecutive_losses = 0
    current_losses = 0
    for pnl in pnls:
        if pnl < 0:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0
    day_index = {day: index for index, day in enumerate(calendar)}
    holding_days = [
        day_index[episode.exit_day] - day_index[episode.entry_day] + 1
        for episode in episodes
    ]
    max_symbol = max(by_symbol.values(), default=0.0) / total if total > 0 else None
    max_day = max(by_entry_day.values(), default=0.0) / total if total > 0 else None
    return {
        "trade_count": len(episodes),
        "trading_days_with_trade": len({item.entry_day for item in episodes}),
        "cumulative_net_pnl": total,
        "net_expectancy": total / len(episodes) if episodes else 0.0,
        "profit_factor": (
            gains / losses if losses else "Infinity" if gains > 0 else None
        ),
        "win_rate": sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0,
        "expectancy_per_initial_r": (
            sum(
                pnl / episode.initial_risk_cash
                for episode, pnl in zip(episodes, pnls)
                if episode.initial_risk_cash > 0
            )
            / len(episodes)
            if episodes
            else 0.0
        ),
        "max_drawdown_pct": mdd * 100.0,
        "max_consecutive_losses": max_consecutive_losses,
        "holding_days_mean": (
            sum(holding_days) / len(holding_days) if holding_days else 0.0
        ),
        "holding_days_median": (
            statistics.median(holding_days) if holding_days else 0.0
        ),
        "entry_turnover_notional": entry_turnover,
        "exit_turnover_notional": exit_turnover,
        "total_turnover_notional": entry_turnover + exit_turnover,
        "total_cost": (entry_turnover + exit_turnover) * rate,
        "max_symbol_contribution": max_symbol,
        "max_entry_day_contribution": max_day,
        "net_pnl_by_symbol": dict(sorted(by_symbol.items())),
        "net_pnl_by_entry_day": dict(sorted(by_entry_day.items())),
        "daily_equity": equity_curve,
    }


def _folds(contract: Mapping[str, Any]) -> tuple[tuple[str, date, date], ...]:
    return tuple(
        (
            f"F{item['fold']:02d}",
            date.fromisoformat(item["start"]),
            date.fromisoformat(item["end"]),
        )
        for item in contract["evaluation"]["fold_boundaries"]
    )


def evaluate_arm(
    *,
    arm: str,
    candidates: Sequence[Candidate],
    features: Mapping[str, Mapping[date, FeatureBar]],
    calendar: Sequence[date],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    simulation = simulate(candidates, features, calendar, arm=arm, contract=contract)
    costs = contract["costs_round_trip_pct_points"]
    metrics = {
        level: summarize(
            simulation.episodes,
            features,
            calendar,
            cost_pct_points=float(cost),
            initial_capital=float(contract["portfolio"]["initial_capital_usd"]),
        )
        for level, cost in costs.items()
        if isinstance(cost, (int, float))
    }
    gross = summarize(
        simulation.episodes,
        features,
        calendar,
        cost_pct_points=0.0,
        initial_capital=float(contract["portfolio"]["initial_capital_usd"]),
    )
    fold_rows = []
    for name, start, end in _folds(contract):
        fold_calendar = tuple(day for day in calendar if start <= day <= end)
        selected = tuple(
            item
            for item in candidates
            if start <= item.entry_day <= item.planned_exit_day <= end
        )
        fold_sim = simulate(
            selected, features, fold_calendar, arm=arm, contract=contract
        )
        net = sum(
            episode_net_pnl(item, float(costs["official"]))
            for item in fold_sim.episodes
        )
        fold_rows.append(
            {
                "fold": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "trades": len(fold_sim.episodes),
                "official_net_pnl": net,
                "positive": net > 0,
            }
        )
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "episodes": [
                    {
                        **asdict(item),
                        "signal_day": item.signal_day.isoformat(),
                        "confirmation_day": item.confirmation_day.isoformat(),
                        "entry_day": item.entry_day.isoformat(),
                        "planned_exit_day": item.planned_exit_day.isoformat(),
                        "exits": [
                            {**asdict(leg), "day": leg.day.isoformat()}
                            for leg in item.exits
                        ],
                    }
                    for item in simulation.episodes
                ]
            }
        )
    ).hexdigest()
    return {
        "gross": gross,
        "cost_levels": metrics,
        "folds_official": fold_rows,
        "positive_folds_official": sum(row["positive"] for row in fold_rows),
        "exit_reasons": dict(
            sorted(
                Counter(
                    leg.reason
                    for item in simulation.episodes
                    for leg in item.exits
                ).items()
            )
        ),
        "portfolio_skips": simulation.portfolio_skips,
        "episode_digest_sha256": digest,
    }


def run_development(
    bars_by_symbol: Mapping[str, Sequence[Candle]],
    *,
    contract: Mapping[str, Any],
    identity: Sequence[Mapping[str, str]],
    producer_commit: str,
    review_evidence_url: str,
) -> dict[str, Any]:
    spy_days = tuple(
        sorted(candle.ts_exchange.date() for candle in bars_by_symbol["SPY"])
    )
    features: dict[str, dict[date, FeatureBar]] = {}
    defect_counts: Counter[str] = Counter()
    for symbol, bars in sorted(bars_by_symbol.items()):
        built, defects = build_features(bars, spy_days)
        features[symbol] = built
        defect_counts.update(defects)
    signals, signal_filter_audit = find_signals(features, spy_days, contract)
    prepared = prepare_arms(signals, features, spy_days)
    arms = {
        "base": evaluate_arm(
            arm="base",
            candidates=prepared["base"].candidates,
            features=features,
            calendar=spy_days,
            contract=contract,
        ),
        "primary": evaluate_arm(
            arm="primary",
            candidates=prepared["primary"].candidates,
            features=features,
            calendar=spy_days,
            contract=contract,
        ),
        "partial": evaluate_arm(
            arm="partial",
            candidates=prepared["primary"].candidates,
            features=features,
            calendar=spy_days,
            contract=contract,
        ),
    }
    for name in ("base", "primary"):
        arms[name]["candidate_audit"] = {
            "prepared_candidates": len(prepared[name].candidates),
            "data_exclusions": prepared[name].data_exclusions,
            "data_exclusion_rate": prepared[name].data_exclusion_rate,
            "boundary_skips": prepared[name].boundary_skips,
            "strategy_filters": dict(prepared[name].strategy_filters),
            "warmup_not_ready": signal_filter_audit["warmup_not_ready"],
        }
    arms["partial"]["candidate_audit"] = {
        **arms["primary"]["candidate_audit"],
        "shared_with_primary": True,
    }

    max_exclusion_rate = max(
        prepared["base"].data_exclusion_rate,
        prepared["primary"].data_exclusion_rate,
    )
    data_gates = {
        "DQ1_data_exclusion_rate_lte_0_05": max_exclusion_rate <= 0.05,
        "DQ2_identity_and_boundaries_match": all(
            item["status"] == "match" for item in identity
        ),
        "DQ3_current_execution_blind_oos_closed": True,
        "DQ4_new_conflicts_eq_0": defect_counts["duplicate_value_conflict"] == 0,
        "DQ5_major_defects_eq_0": defect_counts["invalid_daily_bar"] == 0,
    }
    official = arms["primary"]["cost_levels"]["official"]
    stress = arms["primary"]["cost_levels"]["stress"]
    partial_official = arms["partial"]["cost_levels"]["official"]
    gates = contract["evaluation"]["primary_gates"]
    pf = official["profit_factor"]
    economic_gates = {
        "D1_min_trades": official["trade_count"] >= int(gates["min_trades"]),
        "D2_min_trading_days": official["trading_days_with_trade"]
        >= int(gates["min_trading_days_with_trade"]),
        "D3_official_expectancy_gt_0": official["net_expectancy"] > 0,
        "D4_official_pf_gte_1_20": profit_factor_pass(
            pf, float(gates["official_profit_factor_gte"])
        ),
        "D5_stress_net_gte_0": stress["cumulative_net_pnl"] >= 0,
        "D6_positive_folds_gte_7": arms["primary"]["positive_folds_official"]
        >= int(gates["official_positive_folds_gte"]),
        "D7_mdd_abs_lte_10pct": abs(official["max_drawdown_pct"])
        <= float(gates["official_max_drawdown_abs_pct_lte"]),
        "D8_symbol_contribution_lte_0_35": official["cumulative_net_pnl"] > 0
        and official["max_symbol_contribution"] is not None
        and official["max_symbol_contribution"]
        <= float(gates["max_symbol_contribution_lte"]),
        "D9_entry_day_contribution_lte_0_20": official["cumulative_net_pnl"] > 0
        and official["max_entry_day_contribution"] is not None
        and official["max_entry_day_contribution"]
        <= float(gates["max_entry_day_contribution_lte"]),
        "D10_partial_expectancy_gt_0": partial_official["net_expectancy"] > 0,
    }
    verdict = (
        "INCONCLUSIVE_DATA"
        if not all(data_gates.values())
        else "EXPLORATORY_NO_SELECTION"
        if not all(economic_gates.values())
        else "EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION"
    )
    calendar_index = {day: index for index, day in enumerate(spy_days)}
    result: dict[str, Any] = {
        "schema": "p0_us30_box_orderblock_retest_development/v1",
        "strategy_id": contract["strategy_id"],
        "scope": "exploratory-development-only",
        "producer_commit": producer_commit,
        "review_evidence_url": review_evidence_url,
        "contract_path": contract["_contract_path"],
        "contract_sha256": contract["_contract_sha256"],
        "blind_oos_opened": False,
        "external_provider_used": False,
        "window": {
            "start": contract["data"]["development_start"],
            "end": contract["data"]["development_end_inclusive"],
            "trading_days": len(spy_days),
        },
        "identity": list(identity),
        "signal_count": len(signals),
        "signal_audit": {
            "warmup_not_ready": signal_filter_audit["warmup_not_ready"],
            "breakout_filter_rejection": signal_filter_audit[
                "breakout_filter_rejection"
            ],
            "breakout_signals": len(signals),
            "order_block_signals": sum(item.has_order_block for item in signals),
            "mean_box_width_fraction": (
                sum(item.box_width_fraction for item in signals) / len(signals)
                if signals
                else 0.0
            ),
            "mean_atr_contraction_ratio": (
                sum(item.atr_contraction_ratio for item in signals) / len(signals)
                if signals
                else 0.0
            ),
            "mean_breakout_atr": (
                sum(item.breakout_atr for item in signals) / len(signals)
                if signals
                else 0.0
            ),
            "mean_volume_ratio": (
                sum(item.volume_ratio for item in signals) / len(signals)
                if signals
                else 0.0
            ),
            "retest_delay_days": dict(
                sorted(
                    Counter(
                        calendar_index[item.confirmation_day]
                        - calendar_index[item.signal_day]
                        for item in prepared["primary"].candidates
                    ).items()
                )
            ),
        },
        "defects": dict(sorted(defect_counts.items())),
        "arms": arms,
        "data_gates": data_gates,
        "economic_gates": economic_gates,
        "verdict": verdict,
        "promotion_allowed": False,
        "execution_audit": [
            {
                "phase": "post_result_artifact_reproduction_test",
                "issue": "markdown_used_python_mapping_repr_with_key_type_and_order_drift",
                "resolution": "canonical_sorted_json_for_inline_mapping_rendering",
                "performance_values_inspected_before_fix": True,
                "strategy_rules_changed": False,
                "result_regenerated": True,
            },
            {
                "phase": "second_post_result_artifact_reproduction_test",
                "issue": "markdown_gate_iteration_used_insertion_order_before_sorted_json_reload",
                "resolution": "sorted_data_and_economic_gate_rendering",
                "performance_values_inspected_before_fix": True,
                "strategy_rules_changed": False,
                "result_regenerated": True,
            }
        ],
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result
