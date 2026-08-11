"""사전등록 v1.0의 저회전 섹터 ETF 주간 모멘텀 시뮬레이션."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

from src.models import Candle


@dataclass(frozen=True)
class FeatureBar:
    day: date
    candle: Candle
    sma200: float | None
    ret63_skip5: float | None
    ret126_skip5: float | None

    @property
    def primary_score(self) -> float | None:
        if self.ret63_skip5 is None or self.ret126_skip5 is None:
            return None
        return 0.5 * self.ret63_skip5 + 0.5 * self.ret126_skip5

    @property
    def sensitivity_score(self) -> float | None:
        return self.ret126_skip5


@dataclass(frozen=True)
class TargetEvent:
    signal_day: date
    trade_day: date
    targets: tuple[str, ...]
    regime_on: bool


@dataclass(frozen=True)
class PreparedTargets:
    events: tuple[TargetEvent, ...]
    scheduled_weeks: int
    warmup_weeks: int
    valid_weeks: int
    invalid_weeks: int
    boundary_weeks: int
    regime_on_weeks: int
    cash_weeks: int

    @property
    def invalid_week_rate(self) -> float:
        denominator = self.valid_weeks + self.invalid_weeks
        return self.invalid_weeks / denominator if denominator else 0.0


@dataclass(frozen=True)
class Position:
    symbol: str
    signal_day: date
    entry_day: date
    entry_price: float
    shares: float
    entry_notional: float
    entry_cost: float


@dataclass(frozen=True)
class Episode:
    symbol: str
    signal_day: str
    entry_day: str
    exit_day: str
    entry_price: float
    exit_price: float
    shares: float
    entry_notional: float
    exit_notional: float
    entry_cost: float
    exit_cost: float
    gross_pnl: float
    net_pnl: float
    holding_days: int
    exit_reason: str


@dataclass(frozen=True)
class Simulation:
    episodes: tuple[Episode, ...]
    daily_equity: tuple[dict[str, Any], ...]
    buy_count: int
    sell_count: int
    retained_count: int
    turnover_notional: float
    total_cost: float


def valid_ohlc(candle: Candle) -> bool:
    return (
        min(candle.open, candle.high, candle.low, candle.close) > 0
        and candle.high >= max(candle.open, candle.close)
        and candle.low <= min(candle.open, candle.close)
        and candle.high > candle.low
    )


def build_features(
    bars: Iterable[Candle],
    *,
    skip_recent: int = 5,
    short_lookback: int = 63,
    long_lookback: int = 126,
    sma_period: int = 200,
) -> dict[date, FeatureBar]:
    ordered = sorted(bars, key=lambda candle: candle.ts_utc)
    closes: list[float] = []
    result: dict[date, FeatureBar] = {}
    for index, candle in enumerate(ordered):
        day = candle.ts_exchange.date()
        if day in result:
            raise ValueError(f"중복 일봉: {candle.symbol} {day}")
        closes.append(candle.close)
        sma200 = (
            sum(closes[-sma_period:]) / sma_period
            if len(closes) >= sma_period
            else None
        )

        def skipped_return(lookback: int) -> float | None:
            if index < lookback:
                return None
            old = closes[index - lookback]
            recent = closes[index - skip_recent]
            return recent / old - 1.0 if old > 0 else None

        result[day] = FeatureBar(
            day=day,
            candle=candle,
            sma200=sma200,
            ret63_skip5=skipped_return(short_lookback),
            ret126_skip5=skipped_return(long_lookback),
        )
    return result


def weekly_signal_trade_pairs(
    calendar: tuple[date, ...],
) -> tuple[tuple[date, date], ...]:
    if not calendar:
        return ()
    last_by_week: dict[tuple[int, int], date] = {}
    for day in calendar:
        iso = day.isocalendar()
        last_by_week[(iso.year, iso.week)] = day
    index = {day: offset for offset, day in enumerate(calendar)}
    pairs: list[tuple[date, date]] = []
    for signal_day in sorted(last_by_week.values()):
        offset = index[signal_day]
        if offset + 1 < len(calendar):
            pairs.append((signal_day, calendar[offset + 1]))
    return tuple(pairs)


def _score(feature: FeatureBar, mode: str) -> float | None:
    if mode == "primary":
        return feature.primary_score
    if mode == "sensitivity":
        return feature.sensitivity_score
    raise ValueError(f"알 수 없는 score mode: {mode}")


def prepare_targets(
    features: dict[str, dict[date, FeatureBar]],
    spy_features: dict[date, FeatureBar],
    calendar: tuple[date, ...],
    *,
    symbols: tuple[str, ...],
    mode: str,
    contract: dict[str, Any],
) -> PreparedTargets:
    pairs = weekly_signal_trade_pairs(calendar)
    last_week = calendar[-1].isocalendar() if calendar else None
    boundary = int(bool(calendar and last_week))
    events: list[TargetEvent] = []
    warmup = valid = invalid = regime_on = cash = 0
    top_n = int(contract["signal"]["top_n"])

    for signal_day, trade_day in pairs:
        spy = spy_features.get(signal_day)
        sector_points = [features[symbol].get(signal_day) for symbol in symbols]
        scores = [
            _score(point, mode) if point is not None else None
            for point in sector_points
        ]
        if (
            spy is None
            or spy.sma200 is None
            or any(score is None for score in scores)
        ):
            warmup += 1
            continue
        trade_points = [features[symbol].get(trade_day) for symbol in symbols]
        if (
            not valid_ohlc(spy.candle)
            or any(point is None or not valid_ohlc(point.candle) for point in sector_points)
            or any(point is None or not valid_ohlc(point.candle) for point in trade_points)
        ):
            invalid += 1
            continue
        valid += 1
        active = spy.candle.close > spy.sma200
        targets: tuple[str, ...] = ()
        if active:
            regime_on += 1
            ranked = sorted(
                (
                    (symbol, float(score))
                    for symbol, score in zip(symbols, scores)
                    if float(score) > 0
                ),
                key=lambda item: (-item[1], item[0]),
            )
            targets = tuple(symbol for symbol, _ in ranked[:top_n])
            if not targets:
                cash += 1
        else:
            cash += 1
        events.append(TargetEvent(signal_day, trade_day, targets, active))

    return PreparedTargets(
        events=tuple(events),
        scheduled_weeks=len(pairs) + boundary,
        warmup_weeks=warmup,
        valid_weeks=valid,
        invalid_weeks=invalid,
        boundary_weeks=boundary,
        regime_on_weeks=regime_on,
        cash_weeks=cash,
    )


def equal_all_events(
    prepared: PreparedTargets, symbols: tuple[str, ...]
) -> tuple[TargetEvent, ...]:
    return tuple(
        TargetEvent(
            event.signal_day,
            event.trade_day,
            symbols if event.regime_on else (),
            event.regime_on,
        )
        for event in prepared.events
    )


def prepared_audit(prepared: PreparedTargets) -> dict[str, Any]:
    """이벤트 payload를 제외하고 계산 속성까지 명시 직렬화한다."""
    audit = asdict(prepared)
    audit.pop("events")
    audit["invalid_week_rate"] = prepared.invalid_week_rate
    return audit


def simulate(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    events: Iterable[TargetEvent],
    *,
    entry_notional: float,
    round_trip_cost_pct_points: float,
    initial_capital: float,
) -> Simulation:
    if not calendar:
        raise ValueError("빈 거래일 달력")
    one_way_rate = round_trip_cost_pct_points / 200.0
    by_trade_day = {event.trade_day: event for event in events if event.trade_day in set(calendar)}
    positions: dict[str, Position] = {}
    episodes: list[Episode] = []
    realized_gross = total_cost = turnover = 0.0
    buys = sells = retained = 0
    index = {day: offset for offset, day in enumerate(calendar)}
    curve: list[dict[str, Any]] = []

    def close(symbol: str, day: date, price: float, reason: str) -> None:
        nonlocal realized_gross, total_cost, turnover, sells
        position = positions.pop(symbol)
        exit_notional = position.shares * price
        exit_cost = exit_notional * one_way_rate
        gross_pnl = position.shares * (price - position.entry_price)
        realized_gross += gross_pnl
        total_cost += exit_cost
        turnover += exit_notional
        sells += 1
        net_pnl = gross_pnl - position.entry_cost - exit_cost
        episodes.append(Episode(
            symbol=symbol,
            signal_day=position.signal_day.isoformat(),
            entry_day=position.entry_day.isoformat(),
            exit_day=day.isoformat(),
            entry_price=position.entry_price,
            exit_price=price,
            shares=position.shares,
            entry_notional=position.entry_notional,
            exit_notional=exit_notional,
            entry_cost=position.entry_cost,
            exit_cost=exit_cost,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            holding_days=index[day] - index[position.entry_day] + 1,
            exit_reason=reason,
        ))

    for day in calendar:
        event = by_trade_day.get(day)
        if event is not None:
            targets = set(event.targets)
            for symbol in sorted(set(positions) - targets):
                close(symbol, day, features[symbol][day].candle.open, "rebalance")
            retained += len(set(positions) & targets)
            for symbol in event.targets:
                if symbol in positions:
                    continue
                price = features[symbol][day].candle.open
                shares = entry_notional / price
                entry_cost = entry_notional * one_way_rate
                positions[symbol] = Position(
                    symbol=symbol,
                    signal_day=event.signal_day,
                    entry_day=day,
                    entry_price=price,
                    shares=shares,
                    entry_notional=entry_notional,
                    entry_cost=entry_cost,
                )
                total_cost += entry_cost
                turnover += entry_notional
                buys += 1

        if day == calendar[-1]:
            for symbol in sorted(tuple(positions)):
                close(symbol, day, features[symbol][day].candle.close, "boundary")

        unrealized = sum(
            position.shares
            * (features[symbol][day].candle.close - position.entry_price)
            for symbol, position in positions.items()
        )
        curve.append({
            "day": day.isoformat(),
            "equity": initial_capital + realized_gross - total_cost + unrealized,
        })

    episodes.sort(key=lambda item: (item.exit_day, item.entry_day, item.symbol))
    return Simulation(
        episodes=tuple(episodes),
        daily_equity=tuple(curve),
        buy_count=buys,
        sell_count=sells,
        retained_count=retained,
        turnover_notional=turnover,
        total_cost=total_cost,
    )


def summarize(simulation: Simulation, *, initial_capital: float) -> dict[str, Any]:
    pnls = [episode.net_pnl for episode in simulation.episodes]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total = sum(pnls)
    peak = initial_capital
    max_drawdown = 0.0
    for point in simulation.daily_equity:
        equity = float(point["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity - peak) / peak)

    streak = current = 0
    by_symbol: defaultdict[str, float] = defaultdict(float)
    by_entry_week: defaultdict[str, float] = defaultdict(float)
    holding_days: list[int] = []
    for episode in simulation.episodes:
        if episode.net_pnl < 0:
            current += 1
            streak = max(streak, current)
        else:
            current = 0
        by_symbol[episode.symbol] += episode.net_pnl
        day = date.fromisoformat(episode.entry_day)
        iso = day.isocalendar()
        by_entry_week[f"{iso.year}-W{iso.week:02d}"] += episode.net_pnl
        holding_days.append(episode.holding_days)

    max_symbol = (
        max(by_symbol.values(), default=0.0) / total if total > 0 else None
    )
    return {
        "episode_count": len(simulation.episodes),
        "total_net_pnl": total,
        "return_on_initial_pct": total / initial_capital * 100.0,
        "net_expectancy": total / len(pnls) if pnls else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "max_drawdown_pct": max_drawdown * 100.0,
        "max_consecutive_losses": streak,
        "max_symbol_contribution": max_symbol,
        "turnover_notional": simulation.turnover_notional,
        "total_cost": simulation.total_cost,
        "buy_count": simulation.buy_count,
        "sell_count": simulation.sell_count,
        "retained_count": simulation.retained_count,
        "holding_days_mean": statistics.mean(holding_days) if holding_days else 0.0,
        "holding_days_median": statistics.median(holding_days) if holding_days else 0.0,
        "net_pnl_by_symbol": dict(sorted(by_symbol.items())),
        "net_pnl_by_entry_week": dict(sorted(by_entry_week.items())),
        "daily_equity": list(simulation.daily_equity),
        "final_equity": initial_capital + total,
    }


def chronological_blocks(
    calendar: tuple[date, ...], count: int = 10
) -> tuple[tuple[date, ...], ...]:
    if count <= 0 or len(calendar) < count:
        raise ValueError("calendar는 fold 수 이상이어야 한다")
    base, remainder = divmod(len(calendar), count)
    blocks: list[tuple[date, ...]] = []
    cursor = 0
    for offset in range(count):
        width = base + (1 if offset < remainder else 0)
        blocks.append(calendar[cursor : cursor + width])
        cursor += width
    return tuple(blocks)


def _run_levels(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    events: tuple[TargetEvent, ...],
    *,
    entry_notional: float,
    contract: dict[str, Any],
) -> dict[str, Any]:
    initial = float(contract["portfolio"]["initial_capital_usd"])
    simulations: dict[str, Simulation] = {}
    metrics: dict[str, Any] = {}
    for level, cost in {"gross": 0.0, **contract["costs_round_trip_pct_points"]}.items():
        simulation = simulate(
            features,
            calendar,
            events,
            entry_notional=entry_notional,
            round_trip_cost_pct_points=float(cost),
            initial_capital=initial,
        )
        simulations[level] = simulation
        metrics[level] = summarize(simulation, initial_capital=initial)

    gross_episodes = [
        {
            key: value
            for key, value in asdict(episode).items()
            if key not in {"entry_cost", "exit_cost", "net_pnl"}
        }
        for episode in simulations["gross"].episodes
    ]
    digest = hashlib.sha256(
        (json.dumps(gross_episodes, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    official = metrics["official"]
    gross = metrics["gross"]
    cost_drag = gross["total_net_pnl"] - official["total_net_pnl"]
    return {
        "metrics": metrics,
        "episode_digest_sha256": digest,
        "official_cost_drag": cost_drag,
        "official_cost_drag_vs_gross_profit": (
            cost_drag / gross["total_net_pnl"]
            if gross["total_net_pnl"] > 0
            else None
        ),
    }


def _fold_comparison(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    primary_events: tuple[TargetEvent, ...],
    baseline_events: tuple[TargetEvent, ...],
    *,
    contract: dict[str, Any],
) -> list[dict[str, Any]]:
    official = float(contract["costs_round_trip_pct_points"]["official"])
    initial = float(contract["portfolio"]["initial_capital_usd"])
    selected_notional = float(
        contract["portfolio"]["selected_position_entry_notional_usd"]
    )
    baseline_notional = float(
        contract["portfolio"]["equal_all_position_entry_notional_usd"]
    )
    folds: list[dict[str, Any]] = []
    for number, block in enumerate(chronological_blocks(calendar, 10), start=1):
        allowed = set(block)
        selected_events = tuple(
            event for event in primary_events if event.trade_day in allowed
        )
        equal_events = tuple(
            event for event in baseline_events if event.trade_day in allowed
        )
        selected = simulate(
            features, block, selected_events,
            entry_notional=selected_notional,
            round_trip_cost_pct_points=official,
            initial_capital=initial,
        )
        equal = simulate(
            features, block, equal_events,
            entry_notional=baseline_notional,
            round_trip_cost_pct_points=official,
            initial_capital=initial,
        )
        selected_net = summarize(selected, initial_capital=initial)["total_net_pnl"]
        equal_net = summarize(equal, initial_capital=initial)["total_net_pnl"]
        folds.append({
            "fold": number,
            "start": block[0].isoformat(),
            "end": block[-1].isoformat(),
            "primary_net": selected_net,
            "equal_all_net": equal_net,
            "excess": selected_net - equal_net,
            "positive_excess": selected_net > equal_net,
        })
    return folds


def run_development(
    bars_by_symbol: dict[str, list[Candle]],
    *,
    contract: dict[str, Any],
    data_identity_verified: bool,
) -> dict[str, Any]:
    data = contract["data"]
    symbols = tuple(data["expected_symbols"])
    calendar_symbol = data["calendar_symbol"]
    signal = contract["signal"]
    features = {
        symbol: build_features(
            bars,
            skip_recent=int(signal["skip_recent_trading_days"]),
            short_lookback=int(signal["primary_lookbacks"][0]),
            long_lookback=int(signal["primary_lookbacks"][1]),
        )
        for symbol, bars in sorted(bars_by_symbol.items())
    }
    spy_features = features[calendar_symbol]
    calendar = tuple(sorted(spy_features))
    primary_prepared = prepare_targets(
        features, spy_features, calendar,
        symbols=symbols, mode="primary", contract=contract,
    )
    sensitivity_prepared = prepare_targets(
        features, spy_features, calendar,
        symbols=symbols, mode="sensitivity", contract=contract,
    )
    baseline_events = equal_all_events(primary_prepared, symbols)
    selected_notional = float(
        contract["portfolio"]["selected_position_entry_notional_usd"]
    )
    baseline_notional = float(
        contract["portfolio"]["equal_all_position_entry_notional_usd"]
    )
    primary = _run_levels(
        features, calendar, primary_prepared.events,
        entry_notional=selected_notional, contract=contract,
    )
    sensitivity = _run_levels(
        features, calendar, sensitivity_prepared.events,
        entry_notional=selected_notional, contract=contract,
    )
    baseline = _run_levels(
        features, calendar, baseline_events,
        entry_notional=baseline_notional, contract=contract,
    )
    folds = _fold_comparison(
        features, calendar, primary_prepared.events, baseline_events,
        contract=contract,
    )

    first_trade_day = primary_prepared.events[0].trade_day
    spy_entry = spy_features[first_trade_day].candle.open
    spy_exit = spy_features[calendar[-1]].candle.close
    spy_reference_pnl = 50000.0 * (spy_exit / spy_entry - 1.0)

    data_gates = {
        "DQ1_invalid_week_rate_lte_0_05": (
            primary_prepared.invalid_week_rate
            <= float(contract["evaluation"]["invalid_or_skipped_signal_week_rate_lte"])
        ),
        "DQ2_identity_universe_boundaries_match": data_identity_verified,
        "DQ3_blind_oos_closed": contract["discipline"]["blind_oos_opened"] is False,
    }
    official = primary["metrics"]["official"]
    stress = primary["metrics"]["stress"]
    equal_official = baseline["metrics"]["official"]
    sensitivity_official = sensitivity["metrics"]["official"]
    gates = contract["evaluation"]["primary_gates"]
    pf = official["profit_factor"]
    economic_gates = {
        "D1_min_completed_position_episodes": (
            official["episode_count"] >= gates["min_completed_position_episodes"]
        ),
        "D2_official_expectancy_gt_0": official["net_expectancy"] > 0,
        "D3_official_pf_gte_1_20": bool(
            (pf is None and official["total_net_pnl"] > 0)
            or (pf is not None and pf >= gates["official_profit_factor_gte"])
        ),
        "D4_stress_total_net_pnl_gte_0": stress["total_net_pnl"] >= 0,
        "D5_positive_excess_folds_gte_7": (
            sum(fold["positive_excess"] for fold in folds)
            >= gates["official_positive_excess_folds_gte"]
        ),
        "D6_official_mdd_abs_lte_15pct": (
            abs(official["max_drawdown_pct"])
            <= gates["official_max_drawdown_abs_pct_lte"]
        ),
        "D7_official_net_gt_equal_all": (
            official["total_net_pnl"] > equal_official["total_net_pnl"]
        ),
        "D8_symbol_contribution_lte_0_50": bool(
            official["total_net_pnl"] > 0
            and official["max_symbol_contribution"] is not None
            and official["max_symbol_contribution"]
            <= gates["max_symbol_contribution_lte"]
        ),
        "D9_sensitivity_official_net_gt_0": (
            sensitivity_official["total_net_pnl"]
            > gates["sensitivity_official_total_net_pnl_gt"]
        ),
    }
    if not all(data_gates.values()):
        verdict = "INCONCLUSIVE_DATA"
    elif not all(economic_gates.values()):
        verdict = "NO_SELECTION"
    else:
        verdict = "DEVELOPMENT_GO"

    audit = {
        "primary": prepared_audit(primary_prepared),
        "sensitivity": prepared_audit(sensitivity_prepared),
    }
    return {
        "schema": "p0_sector_etf_weekly_momentum_development/v1",
        "strategy_id": contract["strategy_id"],
        "contract_path": contract["_contract_path"],
        "contract_sha256": contract["_contract_sha256"],
        "data_content_sha256": data["data_content_sha256"],
        "development_window": {
            "start": data["development_start"],
            "end_inclusive": data["development_end_inclusive"],
            "calendar_days": len(calendar),
        },
        "blind_oos_opened": False,
        "symbols": list(symbols),
        "signal_week_audit": audit,
        "primary": primary,
        "sensitivity": sensitivity,
        "equal_all": baseline,
        "folds_official": folds,
        "positive_excess_folds_official": sum(
            fold["positive_excess"] for fold in folds
        ),
        "spy_buy_hold_reference_gross_pnl_50k": spy_reference_pnl,
        "data_gates": data_gates,
        "economic_gates": economic_gates,
        "execution_audit": [{
            "phase": "initial_markdown_render",
            "issue": "derived_invalid_week_rate_missing_from_asdict",
            "resolution": "explicit_property_serialization",
            "performance_values_inspected_before_fix": False,
            "strategy_rules_changed": False,
        }],
        "verdict": verdict,
    }
