"""Gap+RVOL development-only pre-execution assembler.

This module opens only the frozen development window. Snapshot identity,
preconditions, specs, costs, and gates are derived from the preregistration
contract. There is no OOS unlock argument.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.models import Candle
from src.research.gap_rvol.contract import (
    all_specs,
    decision_bar_spec,
    load_contract,
    repo_root,
    require,
    rvol_spec,
    signal_window_spec,
)
from src.research.gap_rvol.features import GapRvolDayFeatures, compute_day_features
from src.research.gap_rvol.identity import require_identity, verify_inputs
from src.research.gap_rvol.loading import decision_bars, load_symbol_day, regular_1m
from src.research.gap_rvol.manifest import (
    TRACKED_SNAPSHOT_MANIFEST_PATH,
    TRACKED_SNAPSHOT_MANIFEST_SHA256,
    DevelopmentResultEvidence,
    PreconditionEvidence,
    SnapshotDefectEvidence,
    SnapshotEvidence,
    build_result,
)
from src.research.gap_rvol.simulator import DaySimulationResult, detect_data_defects
from src.research.gap_rvol.simulator import simulate_day as simulate_gap_rvol_day
from src.research.gap_rvol.windows import (
    blind_oos_days,
    development_data_days,
    development_eval_days,
    previous_trading_day,
)
from src.research.orb.features import RvolWindow


@dataclass(frozen=True)
class FeaturePass:
    data_days: tuple[date, ...]
    eval_days: tuple[date, ...]
    symbols: tuple[str, ...]
    features_by_spec: Mapping[
        str, Mapping[date, Mapping[str, GapRvolDayFeatures]]
    ]
    decision_bars_by_day: Mapping[date, Mapping[str, tuple[Candle, ...]]]
    defects_by_day: Mapping[date, Mapping[str, tuple[str, ...]]]


@dataclass(frozen=True)
class DevelopmentPaths:
    snapshot_path: str
    raw_root: str
    manifest_path: str


@dataclass(frozen=True)
class DevelopmentRun:
    result: Mapping[str, Any]
    evidence: DevelopmentResultEvidence


class NotRunError(RuntimeError):
    """A preregistered P1-P5 failure that blocks all performance calculation."""

    verdict = "NOT_RUN"

    def __init__(self, precondition: str, detail: str) -> None:
        self.precondition = precondition
        self.detail = detail
        super().__init__(f"NOT_RUN {precondition}: {detail}")


def universe_symbols(contract: Mapping[str, object]) -> tuple[str, ...]:
    universe = require(contract, "universe")
    symbols = (
        tuple(require(universe, "market_etf"))
        + tuple(require(universe, "sector_etf"))
        + tuple(require(universe, "large_cap"))
        + tuple(require(universe, "leveraged_inverse"))
    )
    if len(symbols) != len(set(symbols)):
        raise ValueError("Gap+RVOL universe contains duplicate symbols")
    return tuple(sorted(str(symbol) for symbol in symbols))


def tracked_manifest_path() -> str:
    return os.path.join(repo_root(), TRACKED_SNAPSHOT_MANIFEST_PATH)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_tracked_manifest(path: str) -> str:
    expected = os.path.realpath(tracked_manifest_path())
    actual = os.path.realpath(os.path.abspath(path))
    if actual != expected:
        raise ValueError("Gap+RVOL snapshot manifest must be the tracked path")
    actual_sha = _file_sha256(actual)
    if actual_sha != TRACKED_SNAPSHOT_MANIFEST_SHA256:
        raise ValueError("tracked snapshot manifest SHA-256 is not frozen value")
    return actual


def _load_manifest(path: str) -> Mapping[str, object]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise TypeError("snapshot manifest top level must be an object")
    return value


def _validate_generated_at(value: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("generated_at_utc must be second-precision UTC Z")


def build_development_feature_pass(
    *,
    contract: Mapping[str, object],
    db_path: str,
    loader: Callable[[str, str, date], tuple[Candle, ...]] = load_symbol_day,
) -> FeaturePass:
    data_days = development_data_days(contract)
    eval_days = development_eval_days(contract)
    if len(data_days) != int(require(contract, "windows", "development_data_trading_days")):
        raise ValueError("Gap+RVOL development data window count mismatch")
    if len(eval_days) != int(require(contract, "windows", "development_eval_trading_days")):
        raise ValueError("Gap+RVOL development eval window count mismatch")
    if set(eval_days) & set(blind_oos_days(contract)):
        raise ValueError("development eval window overlaps blind OOS")

    symbols = universe_symbols(contract)
    specs = all_specs(contract)
    if len(specs) != 9:
        raise ValueError("Gap+RVOL development must publish exactly 9 specs")
    rvol = rvol_spec(contract)
    bar_spec = decision_bar_spec(contract)
    window = signal_window_spec(contract)
    windows = {
        spec.name: {
            symbol: RvolWindow(
                lookback_days=rvol.lookback_days,
                min_valid_days=rvol.min_valid_days,
                minutes=rvol.minutes,
            )
            for symbol in symbols
        }
        for spec in specs
    }
    features: dict[str, dict[date, dict[str, GapRvolDayFeatures]]] = {
        spec.name: {} for spec in specs
    }
    decision_cache: dict[date, dict[str, tuple[Candle, ...]]] = {}
    defects: dict[date, dict[str, tuple[str, ...]]] = {}
    previous_day = previous_trading_day(data_days[0])
    previous_regular = {
        symbol: regular_1m(loader(db_path, symbol, previous_day))
        for symbol in symbols
    }

    for day in data_days:
        decision_cache[day] = {}
        defects[day] = {}
        for spec in specs:
            features[spec.name][day] = {}
        for symbol in symbols:
            day_candles = loader(db_path, symbol, day)
            today_regular = regular_1m(day_candles)
            decision_cache[day][symbol] = decision_bars(
                day_candles, day, spec=bar_spec
            )
            defects[day][symbol] = detect_data_defects(day_candles)
            prior_day = previous_trading_day(day)
            for spec in specs:
                features[spec.name][day][symbol] = compute_day_features(
                    symbol=symbol,
                    day=day,
                    previous_day=prior_day,
                    previous_regular_1m=previous_regular[symbol],
                    today_regular_1m=today_regular,
                    decision_bars=decision_cache[day][symbol],
                    rvol_window=windows[spec.name][symbol],
                    spec=spec,
                    rvol_minutes=rvol.minutes,
                    decision_minutes=bar_spec.minutes,
                    required_through_minute=window.latest_signal_minute,
                )
            previous_regular[symbol] = today_regular

    return FeaturePass(
        data_days=data_days,
        eval_days=eval_days,
        symbols=symbols,
        features_by_spec=features,
        decision_bars_by_day=decision_cache,
        defects_by_day=defects,
    )


def simulate_all_specs(
    *,
    contract: Mapping[str, object],
    feature_pass: FeaturePass,
) -> Mapping[str, tuple[DaySimulationResult, ...]]:
    expected_eval = development_eval_days(contract)
    if feature_pass.eval_days != expected_eval:
        raise ValueError("feature pass eval days do not match frozen contract")
    output: dict[str, tuple[DaySimulationResult, ...]] = {}
    for spec in all_specs(contract):
        per_day: list[DaySimulationResult] = []
        for day in expected_eval:
            per_day.append(simulate_gap_rvol_day(
                day=day,
                spec=spec,
                decision_bars_by_symbol=feature_pass.decision_bars_by_day[day],
                features_by_symbol=feature_pass.features_by_spec[spec.name][day],
                raw_defects_by_symbol=feature_pass.defects_by_day[day],
                contract=contract,
            ))
        output[spec.name] = tuple(per_day)
    return output


def run_development(
    *,
    paths: DevelopmentPaths,
    producer_commit: str,
    generated_at_utc: str,
    preconditions: PreconditionEvidence,
) -> DevelopmentRun:
    _validate_generated_at(generated_at_utc)
    try:
        preconditions.validate(producer_commit=producer_commit)
    except (TypeError, ValueError) as exc:
        raise NotRunError("P1-P3/P5", str(exc)) from exc
    try:
        manifest_path = _require_tracked_manifest(paths.manifest_path)
    except (OSError, TypeError, ValueError) as exc:
        raise NotRunError("P4", str(exc)) from exc
    contract = load_contract()
    try:
        identity = verify_inputs(
            contract=contract,
            snapshot_path=paths.snapshot_path,
            raw_root=paths.raw_root,
            manifest_path=manifest_path,
        )
        require_identity(identity)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NotRunError("P4", str(exc)) from exc
    snapshot = SnapshotEvidence.from_identity(
        contract=contract,
        identity=identity,
    )
    tracked_manifest = _load_manifest(manifest_path)
    snapshot_defects = SnapshotDefectEvidence.from_sources(
        manifest=tracked_manifest,
        identity=identity,
        contract=contract,
    )
    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path=paths.snapshot_path,
    )
    results_by_spec = simulate_all_specs(
        contract=contract,
        feature_pass=feature_pass,
    )
    evidence = DevelopmentResultEvidence(
        producer_commit=producer_commit,
        preconditions=preconditions,
        snapshot=snapshot,
        results_by_spec=results_by_spec,
        snapshot_defects=snapshot_defects,
    )
    return DevelopmentRun(
        result=build_result(
            evidence,
            contract=contract,
            generated_at_utc=generated_at_utc,
        ),
        evidence=evidence,
    )
