"""Deterministic Gap+RVOL development result envelope."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.research.gap_rvol.contract import (
    PREREG_JSON_PATH,
    PREREG_JSON_SHA256,
    PREREG_MD_PATH,
    PREREG_MD_SHA256,
    all_specs,
    neighbor_specs,
    repo_root,
    require,
)
from src.research.gap_rvol.gates import build_defect_metrics, evaluate_development
from src.research.gap_rvol.identity import IdentityResult
from src.research.gap_rvol.simulator import DaySimulationResult
from src.research.gap_rvol.stats import PerformanceStats, compute_stats
from src.research.gap_rvol.windows import (
    development_data_days,
    development_eval_days,
    folds,
)
from src.research.orb.costs import cost_levels

SCHEMA = "gap-rvol-development-result/v1"
PREREG_MERGE_COMMIT = "d16c6692beca828e6afba32b57fad8a0282089c6"
IMPLEMENTATION_MERGE_COMMIT = "caa507415c2d5e34a25279fa83a356d6b19a54e6"
IMPLEMENTATION_REVIEW_EVIDENCE_URL = (
    "https://github.com/peg5325/ai-trading-system/pull/101"
    "#issuecomment-5126919041"
)
TRACKED_SNAPSHOT_MANIFEST_PATH = (
    "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"
)
TRACKED_SNAPSHOT_MANIFEST_SHA256 = (
    "0b77433c58dbc15c933d2077fbf3cf64b65d194078c8bb6c498a1a78866fc978"
)
TRACKED_CONFLICT_BASELINE_PATH = "docs/research/PREREG_P0_US30_ORB_V1.json"
TRACKED_CONFLICT_BASELINE_SHA256 = (
    "4f69e8539a1140a94a9e14ceb10249352f130e3bc4bb98f518a2492089847559"
)
TRACKED_CONFLICT_BASELINE_COMMIT = (
    "17681a7c1b893f035a7c4f645e08d98734e00fec"
)
LOADER_PROVENANCE = {
    "function": "src.history.oos_guard.research_load",
    "purpose": "performance",
    "track": "intraday",
}


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


@dataclass(frozen=True)
class SnapshotEvidence:
    basename: str
    snapshot_file_sha256: str
    snapshot_content_sha256: str
    manifest_data_sha256: str
    raw_content_sha256: str
    as_of: str
    collection_run_id: str

    @classmethod
    def from_identity(
        cls,
        *,
        contract: Mapping[str, object],
        identity: IdentityResult,
    ) -> SnapshotEvidence:
        if not identity.ok:
            raise ValueError("all four Gap+RVOL identity checks must match")
        actual = {check.name: check.actual for check in identity.checks}
        required_names = {
            "snapshot_file",
            "snapshot_content",
            "manifest_data",
            "raw_content",
        }
        if set(actual) != required_names or any(value is None for value in actual.values()):
            raise ValueError("Gap+RVOL identity evidence must include four hashes")
        data_identity = require(contract, "data_identity")
        expected = {
            "snapshot_file": str(require(data_identity, "snapshot_file_sha256")),
            "snapshot_content": str(require(data_identity, "snapshot_content_sha256")),
            "manifest_data": str(require(data_identity, "manifest_data_sha256")),
            "raw_content": str(require(data_identity, "raw_content_sha256")),
        }
        if actual != expected:
            raise ValueError("Gap+RVOL identity actuals differ from frozen contract")
        return cls(
            basename=str(require(data_identity, "snapshot_basename")),
            snapshot_file_sha256=str(actual["snapshot_file"]),
            snapshot_content_sha256=str(actual["snapshot_content"]),
            manifest_data_sha256=str(actual["manifest_data"]),
            raw_content_sha256=str(actual["raw_content"]),
            as_of=str(require(data_identity, "as_of")),
            collection_run_id=str(require(data_identity, "collection_run_id")),
        )


@dataclass(frozen=True)
class SnapshotDefectEvidence:
    conflict_total: int
    identity_failures: tuple[str, ...]
    conflict_baseline_total: int = 0
    new_conflict_total: int = 0
    conflict_baseline_source: str = ""
    conflict_baseline_by_symbol: tuple[tuple[str, int], ...] = ()
    snapshot_report_conflict_candles: int = 0

    @staticmethod
    def _tracked_conflict_baseline() -> tuple[dict[str, int], int, int]:
        path = os.path.join(repo_root(), TRACKED_CONFLICT_BASELINE_PATH)
        with open(path, "rb") as handle:
            content = handle.read()
        if hashlib.sha256(content).hexdigest() != TRACKED_CONFLICT_BASELINE_SHA256:
            raise ValueError("tracked conflict baseline SHA-256 is not frozen")
        document = json.loads(content)
        baseline = document["defect_policy"]["duplicate_ts_conflict_baseline"]
        by_symbol = baseline["manifest_by_symbol"]
        if not isinstance(by_symbol, Mapping):
            raise TypeError("tracked conflict baseline by-symbol value must be an object")
        normalized = {str(symbol): int(value) for symbol, value in by_symbol.items()}
        total = int(baseline["manifest_conflict_total"])
        if sum(normalized.values()) != total:
            raise ValueError("tracked conflict baseline total does not match by-symbol sum")
        if baseline["defect_is"] != "new conflicts beyond this baseline":
            raise ValueError("tracked conflict baseline semantics are not frozen")
        snapshot_report_conflicts = int(baseline["snapshot_report_conflict_candles"])
        return normalized, total, snapshot_report_conflicts

    @classmethod
    def from_sources(
        cls,
        *,
        manifest: Mapping[str, object],
        identity: IdentityResult,
        contract: Mapping[str, object],
    ) -> SnapshotDefectEvidence:
        symbols = manifest.get("symbols")
        if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes)):
            raise TypeError("snapshot manifest symbols must be an array")
        conflict_total = 0
        conflict_by_symbol: dict[str, int] = {}
        manifest_symbols: set[str] = set()
        for item in symbols:
            if (
                not isinstance(item, Mapping)
                or "symbol" not in item
                or "conflict" not in item
            ):
                raise ValueError("snapshot manifest symbol conflict evidence missing")
            symbol = str(item["symbol"])
            if symbol in manifest_symbols:
                raise ValueError(f"duplicate snapshot manifest symbol: {symbol}")
            manifest_symbols.add(symbol)
            conflict = item["conflict"]
            if not isinstance(conflict, int) or isinstance(conflict, bool) or conflict < 0:
                raise ValueError("snapshot manifest conflict must be nonnegative int")
            conflict_total += conflict
            conflict_by_symbol[symbol] = conflict
        if manifest_symbols != set(universe_symbols(contract)):
            raise ValueError("snapshot manifest symbols differ from frozen universe")
        expected = {
            "snapshot_file": str(require(
                contract, "data_identity", "snapshot_file_sha256")),
            "snapshot_content": str(require(
                contract, "data_identity", "snapshot_content_sha256")),
            "manifest_data": str(require(
                contract, "data_identity", "manifest_data_sha256")),
            "raw_content": str(require(
                contract, "data_identity", "raw_content_sha256")),
        }
        identity_failures = tuple(
            f"{check.name}:{check.status}"
            for check in identity.checks
            if check.status != "match" or check.actual != expected.get(check.name)
        )
        (
            baseline_by_symbol,
            baseline_total,
            snapshot_report_conflicts,
        ) = cls._tracked_conflict_baseline()
        if any(
            conflict_by_symbol.get(symbol, 0) < baseline
            for symbol, baseline in baseline_by_symbol.items()
        ):
            raise ValueError("snapshot conflict count fell below frozen baseline")
        new_conflict_total = sum(
            max(0, conflict - baseline_by_symbol.get(symbol, 0))
            for symbol, conflict in conflict_by_symbol.items()
        )
        return cls(
            conflict_total=conflict_total,
            identity_failures=identity_failures,
            conflict_baseline_total=baseline_total,
            new_conflict_total=new_conflict_total,
            conflict_baseline_source=(
                f"{TRACKED_CONFLICT_BASELINE_PATH}@"
                f"{TRACKED_CONFLICT_BASELINE_COMMIT}"
            ),
            conflict_baseline_by_symbol=tuple(sorted(baseline_by_symbol.items())),
            snapshot_report_conflict_candles=snapshot_report_conflicts,
        )


@dataclass(frozen=True)
class PreconditionEvidence:
    implementation_merge_commit: str
    implementation_review_evidence_url: str
    implementation_map_status: str
    runner_reviewed_commit: str
    runner_code_review_status: str
    runner_architect_status: str
    runner_review_evidence_url: str
    loader_boundary_status: str
    loader_boundary_test: str

    def validate(self, *, producer_commit: str) -> None:
        if self.implementation_merge_commit != IMPLEMENTATION_MERGE_COMMIT:
            raise ValueError("Gap+RVOL implementation merge commit is not frozen")
        if self.implementation_review_evidence_url != IMPLEMENTATION_REVIEW_EVIDENCE_URL:
            raise ValueError("Gap+RVOL PR #101 implementation evidence is not frozen")
        if self.implementation_map_status != "tracked":
            raise ValueError("Gap+RVOL implementation map must be tracked")
        if self.runner_reviewed_commit != producer_commit:
            raise ValueError("Gap+RVOL runner review must match producer_commit exactly")
        if self.runner_code_review_status != "approved":
            raise ValueError("Gap+RVOL runner code-reviewer approval is required")
        if self.runner_architect_status != "clear":
            raise ValueError("Gap+RVOL runner architect clear status is required")
        if not re.fullmatch(
            r"https://github\.com/peg5325/ai-trading-system/pull/\d+"
            r"#issuecomment-\d+",
            self.runner_review_evidence_url,
        ):
            raise ValueError("Gap+RVOL runner review evidence URL is not stable")
        if self.loader_boundary_status != "passed":
            raise ValueError("Gap+RVOL loader boundary must be passed")
        if self.loader_boundary_test != (
            "tests/test_gap_rvol_loading_and_map.py::"
            "test_performance_package_and_runner_have_no_loader_bypass"
        ):
            raise ValueError("Gap+RVOL loader boundary test is not frozen")


@dataclass(frozen=True)
class DevelopmentResultEvidence:
    producer_commit: str
    preconditions: PreconditionEvidence
    snapshot: SnapshotEvidence
    results_by_spec: Mapping[str, Sequence[DaySimulationResult]]
    snapshot_defects: SnapshotDefectEvidence


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN cannot be written to Gap+RVOL manifest")
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def _stats_payload(stats: PerformanceStats) -> dict[str, Any]:
    return _json_safe({
        "cost_level": stats.cost_level,
        "trade_count": stats.trade_count,
        "entered_trade_count": stats.entered_trade_count,
        "trading_days_with_trade": stats.trading_days_with_trade,
        "cumulative_net_pnl": stats.cumulative_net_pnl,
        "net_expectancy": stats.net_expectancy,
        "net_expectancy_per_r": stats.net_expectancy_per_r,
        "profit_factor": stats.profit_factor,
        "mdd": stats.mdd,
        "equity_curve": stats.equity_curve,
        "fold_pnl": stats.fold_pnl,
        "positive_folds": stats.positive_folds,
        "family_profit_share": stats.family_profit_share,
        "day_profit_share": stats.day_profit_share,
    })


def _spec_payload(
    spec: Any,
    *,
    stats_by_cost: Mapping[str, PerformanceStats],
    expected_costs: set[str],
) -> dict[str, Any]:
    if set(stats_by_cost) != expected_costs:
        raise ValueError(f"{spec.name}: must include all three cost levels")
    for level, stats in stats_by_cost.items():
        if stats.cost_level != level:
            raise ValueError(f"{spec.name}: stats cost level mismatch")
    return {
        "name": spec.name,
        "authority": spec.authority,
        "params": {
            "gap_threshold": spec.gap_threshold,
            "rvol_threshold": spec.rvol_threshold,
            "retention_fraction": spec.retention_fraction,
            "target_r_multiple": spec.target_r_multiple,
        },
        "stats": {
            level: _stats_payload(stats_by_cost[level])
            for level in sorted(stats_by_cost)
        },
    }


def _aggregate_reasons(
    results_by_spec: Mapping[str, Sequence[DaySimulationResult]],
    *,
    attribute: str,
) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for spec_name, results in results_by_spec.items():
        for result in results:
            rows = getattr(result, attribute)
            for row in rows:
                reason = row.reason if attribute == "skipped" else row[1]
                counts[(spec_name, reason)] += 1
    return [
        {"spec": spec_name, "reason": reason, "count": count}
        for (spec_name, reason), count in sorted(counts.items())
    ]


def _snapshot_payload(
    evidence: DevelopmentResultEvidence,
    contract: Mapping[str, object],
) -> dict[str, Any]:
    data_identity = require(contract, "data_identity")
    expected = {
        "basename": str(require(data_identity, "snapshot_basename")),
        "snapshot_file_sha256": str(require(data_identity, "snapshot_file_sha256")),
        "snapshot_content_sha256": str(require(data_identity, "snapshot_content_sha256")),
        "manifest_data_sha256": str(require(data_identity, "manifest_data_sha256")),
        "raw_content_sha256": str(require(data_identity, "raw_content_sha256")),
        "as_of": str(require(data_identity, "as_of")),
        "collection_run_id": str(require(data_identity, "collection_run_id")),
    }
    actual = {field: getattr(evidence.snapshot, field) for field in expected}
    if actual != expected:
        raise ValueError("Gap+RVOL snapshot evidence differs from frozen contract")
    return actual


def _gate_criterion(name: str, contract: Mapping[str, object]) -> object:
    gate = require(contract, "development_gate")
    if name == "D1":
        return f">={int(require(gate, 'D1_min_trades'))}"
    if name == "D2":
        return f">={int(require(gate, 'D2_min_trading_days_with_trade'))}"
    if name == "D3":
        return ">0"
    if name == "D4":
        return f">={float(require(gate, 'D4_profit_factor_gte'))}"
    if name == "D5":
        return f"<={float(require(gate, 'D5_abs_mdd_lte'))}"
    if name == "D6":
        return f">={int(require(gate, 'D6_min_positive_folds'))}"
    if name == "D7":
        return f">={float(require(gate, 'D7_stress_cumulative_pnl_gte'))}"
    if name == "D8":
        return "<=1 opposite-sign neighbors"
    if name == "D9":
        return "defect policy"
    raise ValueError(f"unknown Gap+RVOL gate name: {name}")


def _development_gate_value(verdict: str | None, failed: Sequence[str]) -> str:
    if verdict == "NOT_RUN":
        return "NOT_RUN"
    return "FAIL" if failed else "PASS"


def build_payload(
    evidence: DevelopmentResultEvidence,
    *,
    contract: Mapping[str, object],
) -> dict[str, Any]:
    specs = all_specs(contract)
    if len(specs) != 9:
        raise ValueError("Gap+RVOL result must publish exactly 9 specs")
    expected_specs = {spec.name for spec in specs}
    if set(evidence.results_by_spec) != expected_specs:
        raise ValueError("Gap+RVOL spec result set does not match contract")
    expected_days = tuple(development_eval_days(contract))
    data_days = tuple(development_data_days(contract))
    if len(data_days) != 690 or len(expected_days) != 670:
        raise ValueError("Gap+RVOL development days must be 690 data / 670 eval")
    for spec_name, results in evidence.results_by_spec.items():
        if any(result.spec_name != spec_name for result in results):
            raise ValueError(f"{spec_name}: DaySimulationResult spec mismatch")
        if tuple(result.day for result in results) != expected_days:
            raise ValueError(f"{spec_name}: eval day result series is incomplete")
    if not re.fullmatch(r"[0-9a-f]{40}", evidence.producer_commit):
        raise ValueError("producer_commit must be a 40-char lowercase git SHA")
    evidence.preconditions.validate(producer_commit=evidence.producer_commit)

    evaluation_folds = folds(contract)
    expected_costs = set(cost_levels(contract))
    if expected_costs != {"low", "official", "stress"}:
        raise ValueError("Gap+RVOL must use exactly low/official/stress costs")
    stats_by_spec = {
        spec.name: {
            level: compute_stats(
                trades=tuple(
                    trade
                    for result in evidence.results_by_spec[spec.name]
                    for trade in result.trades
                ),
                evaluation_days=expected_days,
                folds=evaluation_folds,
                cost_level=level,
                contract=contract,
            )
            for level in sorted(expected_costs)
        }
        for spec in specs
    }
    primary_results = evidence.results_by_spec["primary"]
    defects = build_defect_metrics(
        day_results=primary_results,
        total_symbol_days=len(expected_days) * len(universe_symbols(contract)),
        identity_failure_count=len(evidence.snapshot_defects.identity_failures),
        new_manifest_conflict_count=evidence.snapshot_defects.new_conflict_total,
    )
    primary_stats = stats_by_spec["primary"]
    decision = evaluate_development(
        primary_official=primary_stats["official"],
        primary_stress=primary_stats["stress"],
        neighbor_official=[
            stats_by_spec[spec.name]["official"]
            for spec in neighbor_specs(contract)
        ],
        defects=defects,
        contract=contract,
    )
    payload = {
        "prereg": {
            "document": PREREG_MD_PATH,
            "contract": PREREG_JSON_PATH,
            "md_sha256": PREREG_MD_SHA256,
            "json_sha256": PREREG_JSON_SHA256,
            "prereg_merge_commit": PREREG_MERGE_COMMIT,
        },
        "producer_commit": evidence.producer_commit,
        "preconditions": {
            "P1": {
                "prereg_merge_commit": PREREG_MERGE_COMMIT,
            },
            "P2": {
                "implementation_merge_commit": (
                    evidence.preconditions.implementation_merge_commit
                ),
                "implementation_review_evidence_url": (
                    evidence.preconditions.implementation_review_evidence_url
                ),
                "implementation_map_status": (
                    evidence.preconditions.implementation_map_status
                ),
            },
            "P3": {
                "runner_reviewed_commit": (
                    evidence.preconditions.runner_reviewed_commit
                ),
                "runner_code_review_status": (
                    evidence.preconditions.runner_code_review_status
                ),
                "runner_architect_status": (
                    evidence.preconditions.runner_architect_status
                ),
                "runner_review_evidence_url": (
                    evidence.preconditions.runner_review_evidence_url
                ),
            },
            "P4": {
                "snapshot_identity": "matched_all_four_hashes",
                "tracked_manifest": TRACKED_SNAPSHOT_MANIFEST_PATH,
                "tracked_manifest_file_sha256": TRACKED_SNAPSHOT_MANIFEST_SHA256,
            },
            "P5": {
                "loader_boundary_status": (
                    evidence.preconditions.loader_boundary_status
                ),
                "loader_boundary_test": evidence.preconditions.loader_boundary_test,
            },
        },
        "snapshot": _json_safe(_snapshot_payload(evidence, contract)),
        "config_version": str(require(contract, "version")),
        "schema_version": SCHEMA,
        "window": {
            "data_start": data_days[0].isoformat(),
            "data_end": data_days[-1].isoformat(),
            "data_trading_days": len(data_days),
            "eval_start": expected_days[0].isoformat(),
            "eval_end": expected_days[-1].isoformat(),
            "eval_trading_days": len(expected_days),
            "folds": [
                {
                    "name": fold.name,
                    "start": fold.start.isoformat(),
                    "end": fold.end.isoformat(),
                    "trading_days": fold.trading_days,
                }
                for fold in evaluation_folds
            ],
            "no_oos_overlap": True,
        },
        "specs": [
            _spec_payload(
                spec,
                stats_by_cost=stats_by_spec[spec.name],
                expected_costs=expected_costs,
            )
            for spec in specs
        ],
        "stats": {
            level: _stats_payload(stats)
            for level, stats in sorted(primary_stats.items())
        },
        "excluded": _aggregate_reasons(
            evidence.results_by_spec, attribute="excluded"
        ),
        "filtered": _aggregate_reasons(
            evidence.results_by_spec, attribute="filtered"
        ),
        "skipped": _aggregate_reasons(
            evidence.results_by_spec, attribute="skipped"
        ),
        "defect_ratios": {
            "data": {
                "numerator": defects.data_excluded_symbol_days,
                "denominator": defects.total_symbol_days,
                "ratio": defects.data_ratio,
            },
            "operational": {
                "numerator": defects.operational_failure_trades,
                "denominator": defects.entered_trades,
                "ratio": defects.operational_ratio,
            },
            "major_defect_count": defects.major_defect_count,
            "conflict_total": evidence.snapshot_defects.conflict_total,
            "conflict_baseline_total": (
                evidence.snapshot_defects.conflict_baseline_total
            ),
            "new_conflict_total": evidence.snapshot_defects.new_conflict_total,
            "conflict_baseline_source": (
                evidence.snapshot_defects.conflict_baseline_source
            ),
            "identity_failures": list(evidence.snapshot_defects.identity_failures),
        },
        "dev_gate": {
            "items": [
                {
                    "name": item.name,
                    "criterion": _gate_criterion(item.name, contract),
                    "actual": _json_safe(item.actual),
                    "passed": item.passed,
                }
                for item in decision.items
            ],
            "development_gate": _development_gate_value(
                decision.verdict, decision.failed
            ),
        },
        "verdict": {
            "value": decision.verdict,
            "next_stage": decision.next_stage,
            "failed": list(decision.failed),
        },
        "loader": dict(LOADER_PROVENANCE),
        "single_pass": {
            "oos_opened": False,
            "spec_count": len(specs),
            "cost_count": len(expected_costs),
            "manual_performance_metrics_allowed": False,
            "all_parameterizations_published": True,
        },
    }
    return _json_safe(payload)


def build_result(
    evidence: DevelopmentResultEvidence,
    *,
    contract: Mapping[str, object],
    generated_at_utc: str,
) -> dict[str, Any]:
    payload = build_payload(evidence, contract=contract)
    return {
        "generated_at_utc": generated_at_utc,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema": SCHEMA,
    }
