"""사전등록 v1.0의 장 마감 과잉압력 반전 시뮬레이션."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from src.models import Candle
from src.research.close_pressure_reversal.contract import canonical_json_bytes


@dataclass(frozen=True)
class FeatureBar:
    day: date
    candle: Candle
    atr14: float | None
    day_return: float
    close_location: float | None
    pressure_score: float | None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signal_day: date
    entry_day: date
    planned_exit_day: date
    atr14: float
    day_return: float
    pressure_score: float


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


def select_verdict(
    *,
    data_gates: dict[str, bool],
    economic_gates: dict[str, bool],
    historical_oos_identity_touch: bool,
) -> str:
    """Apply scientific-governance caps after ordinary data/economic gates."""
    if not all(data_gates.values()):
        return "INCONCLUSIVE_DATA"
    if not all(economic_gates.values()):
        return "NO_SELECTION"
    if historical_oos_identity_touch:
        return "REQUIRES_NEW_PREREG"
    return "DEVELOPMENT_GO"


def build_features(bars: Iterable[Candle], atr_period: int = 14) -> dict[date, FeatureBar]:
    ordered = sorted(bars, key=lambda candle: candle.ts_utc)
    true_ranges: deque[float] = deque(maxlen=atr_period)
    previous_close: float | None = None
    result: dict[date, FeatureBar] = {}
    for candle in ordered:
        day = candle.ts_exchange.date()
        tr = candle.high - candle.low
        if previous_close is not None:
            tr = max(
                tr,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        true_ranges.append(tr)
        atr14 = (
            sum(true_ranges) / atr_period
            if len(true_ranges) == atr_period
            else None
        )
        day_return = candle.close / candle.open - 1.0 if candle.open > 0 else math.nan
        candle_range = candle.high - candle.low
        close_location = (
            (candle.close - candle.low) / candle_range
            if candle_range > 0
            else None
        )
        pressure_score = (
            (candle.high - candle.close) / atr14
            if atr14 is not None and atr14 > 0
            else None
        )
        result[day] = FeatureBar(
            day=day,
            candle=candle,
            atr14=atr14,
            day_return=day_return,
            close_location=close_location,
            pressure_score=pressure_score,
        )
        previous_close = candle.close
    return result


def is_signal(feature: FeatureBar, contract: dict[str, Any]) -> bool:
    candle = feature.candle
    valid_ohlc = (
        min(candle.open, candle.high, candle.low, candle.close) > 0
        and candle.high >= max(candle.open, candle.close)
        and candle.low <= min(candle.open, candle.close)
        and candle.high > candle.low
    )
    signal = contract["signal"]
    return bool(
        valid_ohlc
        and feature.atr14 is not None
        and feature.atr14 > 0
        and feature.close_location is not None
        and feature.day_return <= signal["day_return_lte"]
        and feature.close_location <= signal["close_location_lte"]
    )


def prepare_candidates(
    features: dict[str, dict[date, FeatureBar]],
    calendar: tuple[date, ...],
    *,
    holding_days: int,
    contract: dict[str, Any],
) -> PreparedCandidates:
    index = {day: idx for idx, day in enumerate(calendar)}
    candidates: list[Candidate] = []
    qualifying = boundary = excluded = 0
    reasons: defaultdict[str, int] = defaultdict(int)

    for symbol in sorted(features):
        symbol_features = features[symbol]
        for signal_day in calendar:
            feature = symbol_features.get(signal_day)
            if feature is None or not is_signal(feature, contract):
                continue
            qualifying += 1
            signal_idx = index[signal_day]
            exit_idx = signal_idx + holding_days
            if exit_idx >= len(calendar):
                boundary += 1
                continue
            entry_day = calendar[signal_idx + 1]
            planned_exit_day = calendar[exit_idx]
            required_days = calendar[signal_idx + 1 : exit_idx + 1]
            missing = [day for day in required_days if day not in symbol_features]
            if missing:
                excluded += 1
                reasons["missing_holding_bar"] += 1
                continue
            invalid = [
                day
                for day in required_days
                if not _valid_execution_bar(symbol_features[day].candle)
            ]
            if invalid:
                excluded += 1
                reasons["invalid_execution_bar"] += 1
                continue
            assert feature.atr14 is not None and feature.pressure_score is not None
            candidates.append(
                Candidate(
                    symbol=symbol,
                    signal_day=signal_day,
                    entry_day=entry_day,
                    planned_exit_day=planned_exit_day,
                    atr14=feature.atr14,
                    day_return=feature.day_return,
                    pressure_score=feature.pressure_score,
                )
            )

    candidates.sort(
        key=lambda candidate: (
            candidate.entry_day,
            -candidate.pressure_score,
            candidate.day_return,
            candidate.symbol,
        )
    )
    return PreparedCandidates(
        candidates=tuple(candidates),
        qualifying_signals=qualifying,
        boundary_skips=boundary,
        data_exclusions=excluded,
        data_exclusion_reasons=dict(sorted(reasons.items())),
    )


def _valid_execution_bar(candle: Candle) -> bool:
    return (
        min(candle.open, candle.high, candle.low, candle.close) > 0
        and candle.high >= max(candle.open, candle.close)
        and candle.low <= min(candle.open, candle.close)
        and candle.high > candle.low
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
                -candidate.pressure_score,
                candidate.day_return,
                candidate.symbol,
            )
        )

    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    capacity = duplicate = quantity_skip = 0
    calendar_index = {day: idx for idx, day in enumerate(calendar)}

    def close_position(symbol: str, day: date, price: float, reason: str) -> None:
        position = positions.pop(symbol)
        holding_days = calendar_index[day] - calendar_index[position.candidate.entry_day] + 1
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
        # 1) 밤사이 stop gap만 시가에 먼저 처리해 실제 빈 슬롯을 확정한다.
        for symbol in sorted(positions):
            position = positions[symbol]
            candle = features[symbol][day].candle
            if candle.open <= position.stop_price:
                close_position(symbol, day, candle.open, "gap_stop")

        # 2) 전일 종가 이후 확정된 후보를 당일 시가에 진입한다.
        for candidate in by_entry_day.get(day, []):
            if len(positions) >= max_positions:
                capacity += 1
                continue
            if candidate.symbol in positions:
                duplicate += 1
                continue
            entry_price = features[candidate.symbol][day].candle.open
            risk_per_share = stop_multiple * candidate.atr14
            qty = math.floor(
                min(max_notional / entry_price, risk_budget / risk_per_share)
            )
            if qty < 1:
                quantity_skip += 1
                continue
            positions[candidate.symbol] = Position(
                candidate=candidate,
                entry_price=entry_price,
                stop_price=entry_price - risk_per_share,
                quantity=qty,
                entry_notional=entry_price * qty,
            )

        # 3) 시가 이후 일중 stop, 그 뒤 time exit 순서로 처리한다.
        for symbol in sorted(positions):
            position = positions[symbol]
            candle = features[symbol][day].candle
            if candle.low <= position.stop_price:
                close_position(symbol, day, position.stop_price, "intraday_stop")
        for symbol in sorted(positions):
            position = positions[symbol]
            if day == position.candidate.planned_exit_day:
                candle = features[symbol][day].candle
                close_position(symbol, day, candle.close, "time_exit")

    if positions:
        raise AssertionError(f"development 경계에서 미청산 포지션: {sorted(positions)}")
    trades.sort(key=lambda trade: (trade.exit_day, trade.entry_day, trade.symbol))
    return Simulation(
        trades=tuple(trades),
        skipped_capacity=capacity,
        skipped_duplicate_symbol=duplicate,
        skipped_quantity=quantity_skip,
    )


def chronological_blocks(calendar: tuple[date, ...], count: int = 10) -> tuple[tuple[date, ...], ...]:
    if count <= 0 or len(calendar) < count:
        raise ValueError("calendar는 fold 수 이상이어야 한다")
    base, remainder = divmod(len(calendar), count)
    blocks: list[tuple[date, ...]] = []
    cursor = 0
    for idx in range(count):
        width = base + (1 if idx < remainder else 0)
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
    mdd = 0.0
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
        equity = initial_capital + cumulative_realized_gross - cumulative_cost + unrealized
        peak = max(peak, equity)
        if peak > 0:
            mdd = min(mdd, (equity - peak) / peak)
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
        "max_drawdown_pct": mdd * 100.0,
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
    for idx, block in enumerate(chronological_blocks(calendar, 10), start=1):
        allowed = set(block)
        selected = tuple(
            candidate
            for candidate in candidates
            if candidate.entry_day in allowed and candidate.planned_exit_day in allowed
        )
        simulation = simulate(features, block, selected, contract=contract)
        net = sum(costed_pnl(trade, cost_pct_points) for trade in simulation.trades)
        folds.append(
            {
                "fold": idx,
                "start": block[0].isoformat(),
                "end": block[-1].isoformat(),
                "trades": len(simulation.trades),
                "net_pnl": net,
                "positive": net > 0,
            }
        )
    return folds


def run_development(
    bars_by_symbol: dict[str, list[Candle]],
    *,
    contract: dict[str, Any],
    data_identity: list[dict[str, str]],
) -> dict[str, Any]:
    features = {
        symbol: build_features(bars, int(contract["signal"]["atr_period"]))
        for symbol, bars in sorted(bars_by_symbol.items())
    }
    calendar = tuple(sorted(features[contract["data"]["calendar_symbol"]]))
    specs: dict[str, dict[str, Any]] = {}
    prepared_by_spec: dict[str, PreparedCandidates] = {}

    for spec_name, holding_days in (
        ("primary_3d", int(contract["portfolio"]["primary_holding_days"])),
        ("sensitivity_1d", int(contract["portfolio"]["sensitivity_holding_days"])),
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
            (json.dumps(trade_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        holding_values = sorted(trade.holding_days for trade in simulation.trades)
        specs[spec_name] = {
            "holding_days": holding_days,
            "candidate_audit": {
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
    historical_oos_identity_touch = True
    dq = {
        "DQ1_data_exclusion_rate_lte_0_05": exclusion_rate <= 0.05,
        "DQ2_identity_and_boundaries_match": all(
            check["status"] == "match" for check in data_identity
        ),
        "DQ3_current_execution_blind_oos_closed": True,
    }
    primary = specs["primary_3d"]
    sensitivity = specs["sensitivity_1d"]
    official = primary["cost_levels"]["official"]
    stress = primary["cost_levels"]["stress"]
    gates_cfg = contract["evaluation"]["primary_gates"]
    pf = official["profit_factor"]
    economic = {
        "D1_min_trades": official["trade_count"] >= gates_cfg["min_trades"],
        "D2_official_expectancy_gt_0": official["net_expectancy"] > 0,
        "D3_official_pf_gte_1_20": (
            (pf is None and official["total_net_pnl"] > 0)
            or (pf is not None and pf >= gates_cfg["official_profit_factor_gte"])
        ),
        "D4_stress_total_net_pnl_gte_0": stress["total_net_pnl"] >= 0,
        "D5_positive_folds_gte_7": (
            primary["positive_folds_official"]
            >= gates_cfg["official_positive_folds_gte"]
        ),
        "D6_mdd_abs_lte_10pct": (
            abs(official["max_drawdown_pct"])
            <= gates_cfg["official_max_drawdown_abs_pct_lte"]
        ),
        "D7_concentration": bool(
            official["total_net_pnl"] > 0
            and official["max_symbol_contribution"] is not None
            and official["max_entry_day_contribution"] is not None
            and official["max_symbol_contribution"]
            <= gates_cfg["max_symbol_contribution_lte"]
            and official["max_entry_day_contribution"]
            <= gates_cfg["max_entry_day_contribution_lte"]
        ),
        "D8_sensitivity_expectancy_gt_0": (
            sensitivity["cost_levels"]["official"]["net_expectancy"]
            > gates_cfg["sensitivity_official_expectancy_gt"]
        ),
    }
    verdict = select_verdict(
        data_gates=dq,
        economic_gates=economic,
        historical_oos_identity_touch=historical_oos_identity_touch,
    )

    result: dict[str, Any] = {
        "schema": "p0_us30_close_pressure_reversal_development/v1",
        "strategy_id": contract["strategy_id"],
        "contract_path": contract["_contract_path"],
        "contract_sha256": contract["_contract_sha256"],
        "data_content_sha256": contract["data"]["data_content_sha256"],
        "development_window": {
            "start": contract["data"]["development_start"],
            "end_inclusive": contract["data"]["development_end_inclusive"],
            "calendar_days": len(calendar),
        },
        "blind_oos_opened": historical_oos_identity_touch,
        "blind_oos_performance_accessed": False,
        "current_execution_blind_oos_ohlcv_accessed": False,
        "prereg_compliance": "DEVIATION_ACCEPTED_BY_APPEND_ONLY_ERRATUM",
        "erratum_path": (
            "docs/research/ERRATUM_P0_US30_CLOSE_PRESSURE_OOS_IDENTITY.md"
        ),
        "identity": data_identity,
        "execution_audit": {
            "review_phase": "post_result_premerge_review",
            "issue": "daily_identity_hash_scanned_blind_oos_ohlcv",
            "fix": "tracked_full_receipt_plus_development_bounded_live_hash",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        },
        "governance_gates": {
            "G0_positive_promotion_requires_pristine_prereg": (
                not historical_oos_identity_touch
            )
        },
        "symbol_count": len(features),
        "symbols": sorted(features),
        "specs": specs,
        "data_gates": dq,
        "economic_gates": economic,
        "verdict": verdict,
    }
    result["payload_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result
