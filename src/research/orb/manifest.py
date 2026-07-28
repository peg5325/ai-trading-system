"""ORB 결정론적 development 결과 envelope — 실제 실행은 4-C."""
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

from src.research.orb.contract import (
    PREREG_JSON_PATH,
    PREREG_JSON_SHA256,
    PREREG_MD_PATH,
    PREREG_MD_SHA256,
    SpecParams,
    _require,
    all_specs,
    neighbor_specs,
    universe,
)
from src.research.orb.costs import cost_levels
from src.research.orb.gates import (
    SnapshotDefectEvidence,
    build_defect_metrics,
    evaluate_development,
)
from src.research.orb.identity import IdentityResult
from src.research.orb.simulator import DaySimulationResult
from src.research.orb.stats import PerformanceStats, compute_stats
from src.research.orb.windows import development_eval_days, folds

SCHEMA = "orb-development-result/v1"
PREREG_MERGE_COMMIT = "e42dafcf4a5f803c1af9a3535a980492c93e9c96"
LOADER_PROVENANCE = {
    "function": "src.history.oos_guard.research_load",
    "purpose": "performance",
    "track": "intraday",
}


@dataclass(frozen=True)
class SnapshotEvidence:
    """P3 identity 결과에서만 만들 수 있는 결과 manifest용 스냅샷 증거."""

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
            raise ValueError("P3 identity가 전부 match가 아니면 결과를 만들 수 없다")
        actual = {check.name: check.actual for check in identity.checks}
        if set(actual) != {
            "snapshot_file", "snapshot_content", "manifest_data", "raw_content"
        } or any(value is None for value in actual.values()):
            raise ValueError("P3 identity 4개 증거가 완결되지 않았다")
        data_identity = _require(contract, "data_identity")
        expected = {
            "snapshot_file": str(_require(
                data_identity, "snapshot_file_sha256")),
            "snapshot_content": str(_require(
                data_identity, "snapshot_content_sha256")),
            "manifest_data": str(_require(
                data_identity, "manifest_data_sha256")),
            "raw_content": str(_require(
                data_identity, "raw_content_sha256")),
        }
        if actual != expected:
            raise ValueError("P3 identity actual 값이 동결 계약 hash와 일치하지 않는다")
        return cls(
            basename=str(_require(data_identity, "snapshot_basename")),
            snapshot_file_sha256=str(actual["snapshot_file"]),
            snapshot_content_sha256=str(actual["snapshot_content"]),
            manifest_data_sha256=str(actual["manifest_data"]),
            raw_content_sha256=str(actual["raw_content"]),
            as_of=str(_require(data_identity, "as_of")),
            collection_run_id=str(_require(data_identity, "collection_run_id")),
        )


@dataclass(frozen=True)
class DevelopmentResultEvidence:
    """4-C가 임의 중첩 dict 대신 넘겨야 하는 typed 결과 증거 묶음."""

    producer_commit: str
    snapshot: SnapshotEvidence
    results_by_spec: Mapping[str, Sequence[DaySimulationResult]]
    snapshot_defects: SnapshotDefectEvidence


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN은 ORB 결과 manifest에 기록할 수 없다")
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
    spec: SpecParams,
    *,
    stats_by_cost: Mapping[str, PerformanceStats],
    expected_costs: set[str],
) -> dict[str, Any]:
    if set(stats_by_cost) != expected_costs:
        raise ValueError(
            f"{spec.name}: 비용 통계는 {sorted(expected_costs)} 전부여야 한다")
    for level, stats in stats_by_cost.items():
        if stats.cost_level != level:
            raise ValueError(f"{spec.name}: 통계 cost_level 불일치({level})")
    return {
        "name": spec.name,
        "authority": spec.authority,
        "params": {
            "opening_range_minutes": spec.opening_range_minutes,
            "gap_threshold_abs": spec.gap_threshold_abs,
            "rvol_threshold": spec.rvol_threshold,
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


def build_payload(
    evidence: DevelopmentResultEvidence,
    *,
    contract: Mapping[str, object],
) -> dict[str, Any]:
    """실제 결과 객체에서 완전한 payload를 파생하고 교차 일관성을 검증한다."""
    specs = all_specs(contract)
    expected_specs = {spec.name for spec in specs}
    if set(evidence.results_by_spec) != expected_specs:
        raise ValueError("8개 spec 결과 집합이 계약과 정확히 일치하지 않는다")
    for spec_name, results in evidence.results_by_spec.items():
        if any(result.spec_name != spec_name for result in results):
            raise ValueError(f"{spec_name}: DaySimulationResult spec_name 불일치")

    evaluation_days = development_eval_days(contract)
    expected_days = tuple(evaluation_days)
    for spec_name, results in evidence.results_by_spec.items():
        if tuple(result.day for result in results) != expected_days:
            raise ValueError(f"{spec_name}: development 거래일 결과가 완결되지 않았다")

    data_identity = _require(contract, "data_identity")
    expected_snapshot = {
        "basename": str(_require(data_identity, "snapshot_basename")),
        "snapshot_file_sha256": str(_require(
            data_identity, "snapshot_file_sha256")),
        "snapshot_content_sha256": str(_require(
            data_identity, "snapshot_content_sha256")),
        "manifest_data_sha256": str(_require(
            data_identity, "manifest_data_sha256")),
        "raw_content_sha256": str(_require(
            data_identity, "raw_content_sha256")),
        "as_of": str(_require(data_identity, "as_of")),
        "collection_run_id": str(_require(data_identity, "collection_run_id")),
    }
    actual_snapshot = {
        field: getattr(evidence.snapshot, field)
        for field in expected_snapshot
    }
    if actual_snapshot != expected_snapshot:
        raise ValueError("결과 snapshot 증거가 동결 계약과 일치하지 않는다")
    if not re.fullmatch(r"[0-9a-f]{40}", evidence.producer_commit):
        raise ValueError("producer_commit은 40자리 lowercase git SHA여야 한다")

    evaluation_folds = folds(contract)
    expected_costs = set(cost_levels(contract))
    stats_by_spec = {
        spec.name: {
            level: compute_stats(
                trades=tuple(
                    trade
                    for result in evidence.results_by_spec[spec.name]
                    for trade in result.trades
                ),
                evaluation_days=evaluation_days,
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
        total_symbol_days=len(evaluation_days) * len(universe(contract).all_symbols),
        snapshot_evidence=evidence.snapshot_defects,
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
    fold_rows = [
        {
            "name": fold.name,
            "start": fold.start.isoformat(),
            "end": fold.end.isoformat(),
            "trading_days": fold.trading_days,
        }
        for fold in evaluation_folds
    ]
    gate_rows = [
        {
            "name": item.name,
            "criterion": _json_safe(item.criterion),
            "actual": _json_safe(item.actual),
            "passed": item.passed,
        }
        for item in decision.items
    ]
    payload = {
        "prereg": {
            "document": PREREG_MD_PATH,
            "contract": PREREG_JSON_PATH,
            "md_sha256": PREREG_MD_SHA256,
            "json_sha256": PREREG_JSON_SHA256,
            "prereg_merge_commit": PREREG_MERGE_COMMIT,
        },
        "producer_commit": evidence.producer_commit,
        "snapshot": _json_safe({
            "basename": evidence.snapshot.basename,
            "snapshot_file_sha256": evidence.snapshot.snapshot_file_sha256,
            "snapshot_content_sha256": evidence.snapshot.snapshot_content_sha256,
            "manifest_data_sha256": evidence.snapshot.manifest_data_sha256,
            "raw_content_sha256": evidence.snapshot.raw_content_sha256,
            "as_of": evidence.snapshot.as_of,
            "collection_run_id": evidence.snapshot.collection_run_id,
        }),
        "config_version": str(_require(contract, "version")),
        "schema_version": SCHEMA,
        "window": {
            "start": expected_days[0].isoformat(),
            "end": expected_days[-1].isoformat(),
            "trading_days": len(expected_days),
            "folds": fold_rows,
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
            evidence.results_by_spec, attribute="excluded"),
        "skipped": _aggregate_reasons(
            evidence.results_by_spec, attribute="skipped"),
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
            "conflict_total": defects.conflict_total,
        },
        "dev_gate": {
            "items": gate_rows,
            "development_gate": decision.development_gate,
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


def write_result(
    path: str,
    evidence: DevelopmentResultEvidence,
    *,
    contract: Mapping[str, object],
    generated_at_utc: str,
) -> None:
    """완성본만 atomic replace. generated_at은 payload 해시 밖이다."""
    result = build_result(
        evidence, contract=contract, generated_at_utc=generated_at_utc)
    text = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    partial = f"{path}.partial"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
