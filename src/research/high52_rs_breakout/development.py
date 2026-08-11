"""사전등록 v1.0의 52주 고점·상대강도 돌파 시뮬레이션."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

from src.models import Candle


@dataclass(frozen=True)
class FeatureBar:
    day: date
    candle: Candle
    atr14: float | None
    sma200: float | None
    high252: float | None
    high52_proximity: float | None
    return63: float | None
    prior20_high: float | None
    breakout_strength: float | None
    first_breakout: bool


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signal_day: date
    entry_day: date
    planned_exit_day: date
    atr14: float
    return63: float
    breakout_strength: float
    high52_proximity: float


@dataclass(frozen=True)
class Position:
    candidate: Candidate
    entry_price: float
    stop_price: float
    quantity: int
    entry_notional: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_day: str
    entry_day: str
    exit_day: str
    entry_price: float
    exit_price: float
    stop_price: float
    quantity: int
    entry_notional: float
    gross_pnl: float
    exit_reason: str
    holding_days: int


@dataclass(frozen=True)
class PreparedCandidates:
    candidates: tuple[Candidate, ...]
    cross_section_days: int
    insufficient_cross_section_days: int
    qualifying_signals: int
    boundary_skips: int
    data_exclusions: int
    data_exclusion_reasons: dict[str, int]

    @property
    def data_exclusion_rate(self) -> float:
        denominator = len(self.candidates) + self.data_exclusions
        return self.data_exclusions / denominator if denominator else 0.0


@dataclass(frozen=True)
class Simulation:
    trades: tuple[Trade, ...]
    skipped_capacity: int
    skipped_duplicate_symbol: int
    skipped_quantity: int


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
    atr_period: int = 14,
    sma_period: int = 200,
    high52_period: int = 252,
    relative_strength_lookback: int = 63,
    breakout_lookback: int = 20,
) -> dict[date, FeatureBar]:
    ordered = sorted(bars, key=lambda candle: candle.ts_utc)
    true_ranges: deque[float] = deque(maxlen=atr_period)
    closes: list[float] = []
    highs: list[float] = []
    result: dict[date, FeatureBar] = {}

    for index, candle in enumerate(ordered):
        day = candle.ts_exchange.date()
        if day in result:
            raise ValueError(f"중복 일봉: {candle.symbol} {day}")
        previous_close = closes[-1] if closes else None
        true_range = candle.high - candle.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        true_ranges.append(true_range)
        closes.append(candle.close)
        highs.append(candle.high)

        atr14 = (
            sum(true_ranges) / atr_period
            if len(true_ranges) == atr_period
            else None
        )
        sma200 = (
            sum(closes[-sma_period:]) / sma_period
            if len(closes) >= sma_period
            else None
        )
        high252 = max(highs[-high52_period:]) if len(highs) >= high52_period else None
        proximity = candle.close / high252 if high252 is not None and high252 > 0 else None
        return63 = (
            candle.close / closes[index - relative_strength_lookback] - 1.0
            if index >= relative_strength_lookback
            and closes[index - relative_strength_lookback] > 0
            else None
        )
        prior20 = (
            max(highs[index - breakout_lookback : index])
            if index >= breakout_lookback
            else None
        )
        previous_prior20 = (
            max(highs[index - breakout_lookback - 1 : index - 1])
            if index >= breakout_lookback + 1
            else None
        )
        first_breakout = bool(
            prior20 is not None
            and previous_prior20 is not None
            and previous_close is not None
            and candle.close > prior20
            and previous_close <= previous_prior20
        )
        breakout_strength = (
            candle.close / prior20 - 1.0
            if prior20 is not None and prior20 > 0
            else None
        )
        result[day] = FeatureBar(
            day=day,
            candle=candle,
            atr14=atr14,
            sma200=sma200,
            high252=high252,
            high52_proximity=proximity,
            return63=return63,
            prior20_high=prior20,
            breakout_strength=breakout_strength,
            first_breakout=first_breakout,
        )
    return result


def is_signal(feature: FeatureBar, contract: dict[str, Any]) -> bool:
    signal = contract["signal"]
    return bool(
        valid_ohlc(feature.candle)
        and feature.atr14 is not None
        and feature.atr14 > 0
        and feature.sma200 is not None
        and feature.sma200 > 0
        and feature.high252 is not None
        and feature.high252 > 0
        and feature.high52_proximity is not None
        and feature.high52_proximity >= signal["high52_proximity_gte"]
        and feature.candle.close > feature.sma200
        and feature.return63 is not None
        and feature.breakout_strength is not None
        and feature.first_breakout
    )


def prepare_candidates(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    *,
    holding_days: int,
    contract: dict[str, Any],
) -> PreparedCandidates:
    signal = contract["signal"]
    calendar_index = {day: index for index, day in enumerate(calendar)}
    candidates: list[Candidate] = []
    cross_section_days = insufficient_days = qualifying = boundary = excluded = 0
    reasons: defaultdict[str, int] = defaultdict(int)

    for signal_day in calendar:
        population = [
            (symbol, symbol_features[signal_day])
            for symbol, symbol_features in sorted(features.items())
            if signal_day in symbol_features
            and valid_ohlc(symbol_features[signal_day].candle)
            and symbol_features[signal_day].return63 is not None
        ]
        if len(population) < int(signal["minimum_cross_section"]):
            insufficient_days += 1
            continue
        cross_section_days += 1
        population.sort(
            key=lambda item: (-float(item[1].return63), item[0])
        )
        top_count = math.ceil(
            len(population) * float(signal["relative_strength_top_fraction"])
        )
        for symbol, feature in population[:top_count]:
            if not is_signal(feature, contract):
                continue
            qualifying += 1
            signal_index = calendar_index[signal_day]
            exit_index = signal_index + holding_days
            if exit_index >= len(calendar):
                boundary += 1
                continue
            entry_day = calendar[signal_index + 1]
            planned_exit_day = calendar[exit_index]
            required_days = calendar[signal_index + 1 : exit_index + 1]
            symbol_features = features[symbol]
            if any(day not in symbol_features for day in required_days):
                excluded += 1
                reasons["missing_holding_bar"] += 1
                continue
            if any(not valid_ohlc(symbol_features[day].candle) for day in required_days):
                excluded += 1
                reasons["invalid_execution_bar"] += 1
                continue
            assert feature.atr14 is not None
            assert feature.return63 is not None
            assert feature.breakout_strength is not None
            assert feature.high52_proximity is not None
            candidates.append(
                Candidate(
                    symbol=symbol,
                    signal_day=signal_day,
                    entry_day=entry_day,
                    planned_exit_day=planned_exit_day,
                    atr14=feature.atr14,
                    return63=feature.return63,
                    breakout_strength=feature.breakout_strength,
                    high52_proximity=feature.high52_proximity,
                )
            )

    candidates.sort(
        key=lambda candidate: (
            candidate.entry_day,
            -candidate.return63,
            -candidate.breakout_strength,
            -candidate.high52_proximity,
            candidate.symbol,
        )
    )
    return PreparedCandidates(
        candidates=tuple(candidates),
        cross_section_days=cross_section_days,
        insufficient_cross_section_days=insufficient_days,
        qualifying_signals=qualifying,
        boundary_skips=boundary,
        data_exclusions=excluded,
        data_exclusion_reasons=dict(sorted(reasons.items())),
    )


def simulate(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    candidates: Iterable[Candidate],
    *,
    contract: dict[str, Any],
) -> Simulation:
    portfolio = contract["portfolio"]
    max_positions = int(portfolio["max_positions"])
    max_notional = float(portfolio["max_entry_notional_usd"])
    risk_budget = float(portfolio["risk_budget_usd"])
    stop_multiple = float(portfolio["stop_atr_multiple"])

    by_entry_day: defaultdict[date, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_entry_day[candidate.entry_day].append(candidate)
    for day in by_entry_day:
        by_entry_day[day].sort(
            key=lambda candidate: (
                -candidate.return63,
                -candidate.breakout_strength,
                -candidate.high52_proximity,
                candidate.symbol,
            )
        )

    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    capacity = duplicate = quantity_skip = 0
    calendar_index = {day: index for index, day in enumerate(calendar)}

    def close_position(symbol: str, day: date, price: float, reason: str) -> None:
        position = positions.pop(symbol)
        holding_days = (
            calendar_index[day] - calendar_index[position.candidate.entry_day] + 1
        )
        trades.append(
            Trade(
                symbol=symbol,
                signal_day=position.candidate.signal_day.isoformat(),
                entry_day=position.candidate.entry_day.isoformat(),
                exit_day=day.isoformat(),
                entry_price=position.entry_price,
                exit_price=price,
                stop_price=position.stop_price,
                quantity=position.quantity,
                entry_notional=position.entry_notional,
                gross_pnl=position.quantity * (price - position.entry_price),
                exit_reason=reason,
                holding_days=holding_days,
            )
        )

    for day in calendar:
        for symbol in sorted(tuple(positions)):
            position = positions[symbol]
            candle = features[symbol][day].candle
            if candle.open <= position.stop_price:
                close_position(symbol, day, candle.open, "gap_stop")

        for candidate in by_entry_day.get(day, []):
            if len(positions) >= max_positions:
                capacity += 1
                continue
            if candidate.symbol in positions:
                duplicate += 1
                continue
            entry_price = features[candidate.symbol][day].candle.open
            risk_per_share = stop_multiple * candidate.atr14
            quantity = math.floor(
                min(max_notional / entry_price, risk_budget / risk_per_share)
            )
            if quantity < 1:
                quantity_skip += 1
                continue
            positions[candidate.symbol] = Position(
                candidate=candidate,
                entry_price=entry_price,
                stop_price=entry_price - risk_per_share,
                quantity=quantity,
                entry_notional=entry_price * quantity,
            )

        for symbol in sorted(tuple(positions)):
            position = positions[symbol]
            candle = features[symbol][day].candle
            if candle.low <= position.stop_price:
                close_position(symbol, day, position.stop_price, "intraday_stop")
        for symbol in sorted(tuple(positions)):
            position = positions[symbol]
            if day == position.candidate.planned_exit_day:
                close_position(symbol, day, features[symbol][day].candle.close, "time_exit")

    if positions:
        raise AssertionError(f"development 경계에서 미청산 포지션: {sorted(positions)}")
    trades.sort(key=lambda trade: (trade.exit_day, trade.entry_day, trade.symbol))
    return Simulation(
        trades=tuple(trades),
        skipped_capacity=capacity,
        skipped_duplicate_symbol=duplicate,
        skipped_quantity=quantity_skip,
    )


def chronological_blocks(
    calendar: tuple[date, ...], count: int = 10
) -> tuple[tuple[date, ...], ...]:
    if count <= 0 or len(calendar) < count:
        raise ValueError("calendar는 fold 수 이상이어야 한다")
    base, remainder = divmod(len(calendar), count)
    blocks: list[tuple[date, ...]] = []
    cursor = 0
    for index in range(count):
        width = base + (1 if index < remainder else 0)
        blocks.append(calendar[cursor : cursor + width])
        cursor += width
    return tuple(blocks)


def costed_pnl(trade: Trade, cost_pct_points: float) -> float:
    return trade.gross_pnl - trade.entry_notional * cost_pct_points / 100.0


def summarize(
    trades: tuple[Trade, ...],
    *,
    calendar: tuple[date, ...],
    features: dict[str, dict[date, FeatureBar]],
    cost_pct_points: float,
    initial_capital: float,
) -> dict[str, Any]:
    pnls = [costed_pnl(trade, cost_pct_points) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    total = sum(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    realized_gross_by_day: defaultdict[date, float] = defaultdict(float)
    entry_cost_by_day: defaultdict[date, float] = defaultdict(float)
    by_symbol: defaultdict[str, float] = defaultdict(float)
    by_entry_day: defaultdict[str, float] = defaultdict(float)
    for trade, pnl in zip(trades, pnls):
        realized_gross_by_day[date.fromisoformat(trade.exit_day)] += trade.gross_pnl
        entry_cost_by_day[date.fromisoformat(trade.entry_day)] += (
            trade.entry_notional * cost_pct_points / 100.0
        )
        by_symbol[trade.symbol] += pnl
        by_entry_day[trade.entry_day] += pnl

    cumulative_realized_gross = 0.0
    cumulative_cost = 0.0
    peak = initial_capital
    max_drawdown = 0.0
    equity_curve: list[dict[str, Any]] = []
    for day in calendar:
        cumulative_realized_gross += realized_gross_by_day.get(day, 0.0)
        cumulative_cost += entry_cost_by_day.get(day, 0.0)
        unrealized = 0.0
        for trade in trades:
            entry_day = date.fromisoformat(trade.entry_day)
            exit_day = date.fromisoformat(trade.exit_day)
            if entry_day <= day < exit_day:
                mark = features[trade.symbol][day].candle.close
                unrealized += trade.quantity * (mark - trade.entry_price)
        equity = (
            initial_capital
            + cumulative_realized_gross
            - cumulative_cost
            + unrealized
        )
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity - peak) / peak)
        equity_curve.append({"day": day.isoformat(), "equity": equity})

    max_consecutive_losses = 0
    current_losses = 0
    for pnl in pnls:
        if pnl < 0:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0

    if total > 0:
        max_symbol_share = max(by_symbol.values(), default=0.0) / total
        max_entry_day_share = max(by_entry_day.values(), default=0.0) / total
    else:
        max_symbol_share = None
        max_entry_day_share = None

    return {
        "trade_count": len(trades),
        "total_net_pnl": total,
        "net_expectancy": total / len(trades) if trades else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "max_drawdown_pct": max_drawdown * 100.0,
        "max_consecutive_losses": max_consecutive_losses,
        "max_symbol_contribution": max_symbol_share,
        "max_entry_day_contribution": max_entry_day_share,
        "net_pnl_by_symbol": dict(sorted(by_symbol.items())),
        "net_pnl_by_entry_day": dict(sorted(by_entry_day.items())),
        "daily_equity": equity_curve,
        "final_equity": initial_capital + total,
    }


def _fold_pnls(
    *,
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    candidates: tuple[Candidate, ...],
    contract: dict[str, Any],
    cost_pct_points: float,
) -> list[dict[str, Any]]:
    folds: list[dict[str, Any]] = []
    for index, block in enumerate(chronological_blocks(calendar, 10), start=1):
        allowed = set(block)
        selected = tuple(
            candidate
            for candidate in candidates
            if candidate.entry_day in allowed and candidate.planned_exit_day in allowed
        )
        simulation = simulate(features, block, selected, contract=contract)
        net_pnl = sum(
            costed_pnl(trade, cost_pct_points) for trade in simulation.trades
        )
        folds.append(
            {
                "fold": index,
                "start": block[0].isoformat(),
                "end": block[-1].isoformat(),
                "trades": len(simulation.trades),
                "net_pnl": net_pnl,
                "positive": net_pnl > 0,
            }
        )
    return folds


def run_development(
    bars_by_symbol: dict[str, list[Candle]],
    *,
    contract: dict[str, Any],
    data_identity_verified: bool,
) -> dict[str, Any]:
    signal = contract["signal"]
    features = {
        symbol: build_features(
            bars,
            atr_period=int(signal["atr_period"]),
            sma_period=int(signal["sma_period"]),
            high52_period=int(signal["high52_period"]),
            relative_strength_lookback=int(signal["relative_strength_lookback"]),
            breakout_lookback=int(signal["breakout_lookback"]),
        )
        for symbol, bars in sorted(bars_by_symbol.items())
    }
    calendar = tuple(sorted(features[contract["data"]["calendar_symbol"]]))
    specs: dict[str, dict[str, Any]] = {}
    prepared_by_spec: dict[str, PreparedCandidates] = {}

    for spec_name, holding_days in (
        ("primary_5d", int(contract["portfolio"]["primary_holding_days"])),
        ("sensitivity_3d", int(contract["portfolio"]["sensitivity_holding_days"])),
    ):
        prepared = prepare_candidates(
            features,
            calendar,
            holding_days=holding_days,
            contract=contract,
        )
        prepared_by_spec[spec_name] = prepared
        simulation = simulate(
            features,
            calendar,
            prepared.candidates,
            contract=contract,
        )
        metrics = {
            level: summarize(
                simulation.trades,
                calendar=calendar,
                features=features,
                cost_pct_points=float(cost),
                initial_capital=float(contract["portfolio"]["initial_capital_usd"]),
            )
            for level, cost in contract["costs_round_trip_pct_points"].items()
        }
        gross = summarize(
            simulation.trades,
            calendar=calendar,
            features=features,
            cost_pct_points=0.0,
            initial_capital=float(contract["portfolio"]["initial_capital_usd"]),
        )
        official_cost = float(contract["costs_round_trip_pct_points"]["official"])
        folds = _fold_pnls(
            features=features,
            calendar=calendar,
            candidates=prepared.candidates,
            contract=contract,
            cost_pct_points=official_cost,
        )
        exit_reasons: defaultdict[str, int] = defaultdict(int)
        for trade in simulation.trades:
            exit_reasons[trade.exit_reason] += 1
        trade_payload = [asdict(trade) for trade in simulation.trades]
        trade_digest = hashlib.sha256(
            (
                json.dumps(trade_payload, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        holding_values = sorted(trade.holding_days for trade in simulation.trades)
        specs[spec_name] = {
            "holding_days": holding_days,
            "candidate_audit": {
                "cross_section_days": prepared.cross_section_days,
                "insufficient_cross_section_days": prepared.insufficient_cross_section_days,
                "qualifying_signals": prepared.qualifying_signals,
                "boundary_skips": prepared.boundary_skips,
                "prepared_candidates": len(prepared.candidates),
                "data_exclusions": prepared.data_exclusions,
                "data_exclusion_reasons": prepared.data_exclusion_reasons,
                "data_exclusion_rate": prepared.data_exclusion_rate,
            },
            "portfolio_skips": {
                "capacity": simulation.skipped_capacity,
                "duplicate_symbol": simulation.skipped_duplicate_symbol,
                "quantity_lt_one": simulation.skipped_quantity,
            },
            "gross": gross,
            "cost_levels": metrics,
            "folds_official": folds,
            "positive_folds_official": sum(fold["positive"] for fold in folds),
            "exit_reasons": dict(sorted(exit_reasons.items())),
            "holding_days_mean": (
                sum(holding_values) / len(holding_values) if holding_values else 0.0
            ),
            "holding_days_median": (
                holding_values[len(holding_values) // 2] if holding_values else 0
            ),
            "trades_sha256": trade_digest,
        }

    exclusion_rate = max(
        prepared.data_exclusion_rate for prepared in prepared_by_spec.values()
    )
    data_gates = {
        "DQ1_data_exclusion_rate_lte_0_05": exclusion_rate <= 0.05,
        "DQ2_identity_and_boundaries_match": data_identity_verified,
        "DQ3_blind_oos_closed": contract["discipline"]["blind_oos_opened"] is False,
    }
    primary = specs["primary_5d"]
    sensitivity = specs["sensitivity_3d"]
    official = primary["cost_levels"]["official"]
    stress = primary["cost_levels"]["stress"]
    gates = contract["evaluation"]["primary_gates"]
    profit_factor = official["profit_factor"]
    economic_gates = {
        "D1_min_trades": official["trade_count"] >= gates["min_trades"],
        "D2_official_expectancy_gt_0": official["net_expectancy"] > 0,
        "D3_official_pf_gte_1_20": (
            (profit_factor is None and official["total_net_pnl"] > 0)
            or (
                profit_factor is not None
                and profit_factor >= gates["official_profit_factor_gte"]
            )
        ),
        "D4_stress_total_net_pnl_gte_0": stress["total_net_pnl"] >= 0,
        "D5_positive_folds_gte_7": (
            primary["positive_folds_official"]
            >= gates["official_positive_folds_gte"]
        ),
        "D6_mdd_abs_lte_10pct": (
            abs(official["max_drawdown_pct"])
            <= gates["official_max_drawdown_abs_pct_lte"]
        ),
        "D7_concentration": bool(
            official["total_net_pnl"] > 0
            and official["max_symbol_contribution"] is not None
            and official["max_entry_day_contribution"] is not None
            and official["max_symbol_contribution"]
            <= gates["max_symbol_contribution_lte"]
            and official["max_entry_day_contribution"]
            <= gates["max_entry_day_contribution_lte"]
        ),
        "D8_sensitivity_expectancy_gt_0": (
            sensitivity["cost_levels"]["official"]["net_expectancy"]
            > gates["sensitivity_official_expectancy_gt"]
        ),
    }
    if not all(data_gates.values()):
        verdict = "INCONCLUSIVE_DATA"
    elif not all(economic_gates.values()):
        verdict = "NO_SELECTION"
    else:
        verdict = "DEVELOPMENT_GO"

    return {
        "schema": "p0_us30_high52_rs_breakout_development/v1",
        "strategy_id": contract["strategy_id"],
        "contract_path": contract["_contract_path"],
        "contract_sha256": contract["_contract_sha256"],
        "data_content_sha256": contract["data"]["data_content_sha256"],
        "development_window": {
            "start": contract["data"]["development_start"],
            "end_inclusive": contract["data"]["development_end_inclusive"],
            "calendar_days": len(calendar),
        },
        "blind_oos_opened": False,
        "symbol_count": len(features),
        "symbols": sorted(features),
        "specs": specs,
        "data_gates": data_gates,
        "economic_gates": economic_gates,
        "verdict": verdict,
    }
