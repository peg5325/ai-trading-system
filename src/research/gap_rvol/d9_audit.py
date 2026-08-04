"""Deterministic, development-only audit of the Gap+RVOL D9 evidence."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from src.research.gap_rvol.contract import all_specs, require
from src.research.gap_rvol.development import FeaturePass
from src.research.gap_rvol.manifest import SnapshotDefectEvidence

SCHEMA = "gap-rvol-d9-audit/v1"


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def build_d9_audit(
    *,
    feature_pass: FeaturePass,
    tracked_result: Mapping[str, Any],
    snapshot_defects: SnapshotDefectEvidence,
    contract: Mapping[str, object],
    generated_at_utc: str,
) -> dict[str, Any]:
    """Rebuild D9 inputs without computing PnL, returns, or blind OOS data."""
    primary = feature_pass.features_by_spec["primary"]
    symbols = feature_pass.symbols
    eval_days = feature_pass.eval_days
    events: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    day_counts: Counter[str] = Counter()
    raw_major: list[dict[str, str]] = []

    for day in eval_days:
        day_key = day.isoformat()
        for symbol in symbols:
            reasons = primary[day][symbol].excluded_reasons
            if reasons:
                events.append({
                    "day": day_key,
                    "symbol": symbol,
                    "reasons": list(reasons),
                })
                symbol_counts[symbol] += 1
                day_counts[day_key] += 1
                reason_counts.update(reasons)
            raw_major.extend(
                {"day": day_key, "symbol": symbol, "reason": reason}
                for reason in feature_pass.defects_by_day[day][symbol]
            )

    primary_signature = {
        (event["day"], event["symbol"]): tuple(event["reasons"])
        for event in events
    }
    divergent_specs: list[str] = []
    for spec in all_specs(contract):
        signature = {
            (day.isoformat(), symbol): (
                feature_pass.features_by_spec[spec.name][day][symbol].excluded_reasons
            )
            for day in eval_days
            for symbol in symbols
            if feature_pass.features_by_spec[spec.name][day][symbol].excluded_reasons
        }
        if signature != primary_signature:
            divergent_specs.append(spec.name)

    denominator = len(eval_days) * len(symbols)
    ratio = len(events) / denominator if denominator else float("inf")
    tracked_defects = tracked_result["payload"]["defect_ratios"]
    if int(tracked_defects["data"]["numerator"]) != len(events):
        raise ValueError("rebuilt D9 exclusion numerator differs from tracked result")
    if int(tracked_defects["data"]["denominator"]) != denominator:
        raise ValueError("rebuilt D9 exclusion denominator differs from tracked result")
    if divergent_specs:
        raise ValueError(f"D9 exclusions diverge across specs: {divergent_specs}")

    threshold = float(require(contract, "defect_policy", "data_exclusion_ratio_lte"))
    fully_excluded = [
        day for day, count in sorted(day_counts.items()) if count == len(symbols)
    ]
    corrected_major = (
        len(raw_major)
        + len(snapshot_defects.identity_failures)
        + snapshot_defects.new_conflict_total
    )
    return {
        "schema": SCHEMA,
        "generated_at_utc": generated_at_utc,
        "scope": {
            "development_only": True,
            "blind_oos_opened": False,
            "performance_computed": False,
            "strategy_contract_changed": False,
        },
        "tracked_result": {
            "verdict": tracked_result["payload"]["verdict"]["value"],
            "failed": tracked_result["payload"]["verdict"]["failed"],
            "data_exclusion_numerator": int(tracked_defects["data"]["numerator"]),
            "reported_major_defect_count": int(tracked_defects["major_defect_count"]),
            "reported_conflict_total": int(tracked_defects["conflict_total"]),
        },
        "conflict_audit": {
            "frozen_manifest_total": snapshot_defects.conflict_total,
            "pre_result_baseline_total": snapshot_defects.conflict_baseline_total,
            "pre_result_baseline_by_symbol": dict(
                snapshot_defects.conflict_baseline_by_symbol
            ),
            "snapshot_report_conflict_candles": (
                snapshot_defects.snapshot_report_conflict_candles
            ),
            "new_conflict_total": snapshot_defects.new_conflict_total,
            "baseline_source": snapshot_defects.conflict_baseline_source,
            "raw_major_defect_events": raw_major,
            "identity_failures": list(snapshot_defects.identity_failures),
            "corrected_major_defect_count": corrected_major,
            "mapping_defect": (
                "the original runner counted the frozen pre-result conflict baseline "
                "as new invalidation-grade conflicts"
            ),
        },
        "exclusion_audit": {
            "numerator": len(events),
            "denominator": denominator,
            "ratio": ratio,
            "threshold_lte": threshold,
            "passes": ratio <= threshold,
            "reason_counts": _sorted_counts(reason_counts),
            "symbol_counts": _sorted_counts(symbol_counts),
            "day_counts": _sorted_counts(day_counts),
            "fully_excluded_market_days": fully_excluded,
            "all_specs_identical": True,
            "events": events,
        },
        "corrected_d9": {
            "major_defect_count": corrected_major,
            "data_exclusion_ratio": ratio,
            "passes": corrected_major == 0 and ratio <= threshold,
            "verdict": (
                "INCONCLUSIVE_DATA"
                if corrected_major != 0 or ratio > threshold
                else "REQUIRES_FULL_GATE_REEVALUATION"
            ),
        },
    }


def audit_json_bytes(audit: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def render_d9_audit(audit: Mapping[str, Any]) -> str:
    conflict = audit["conflict_audit"]
    exclusion = audit["exclusion_audit"]
    corrected = audit["corrected_d9"]
    lines = [
        "# P0-US30 Gap+RVOL D9 defect-only 감사",
        "",
        "## 판정",
        "",
        f"**{corrected['verdict']}**",
        "",
        "- blind OOS opened: `False`",
        "- performance computed: `False`",
        "- strategy contract changed: `False`",
        "",
        "## 충돌 기준선 감사",
        "",
        f"- frozen manifest conflict total: `{conflict['frozen_manifest_total']}`",
        f"- pre-result baseline total: `{conflict['pre_result_baseline_total']}`",
        (
            "- pre-result baseline by symbol: `"
            f"{json.dumps(conflict['pre_result_baseline_by_symbol'], sort_keys=True)}`"
        ),
        (
            "- snapshot promotion conflict candles: `"
            f"{conflict['snapshot_report_conflict_candles']}` (manifest 28건 중 부분집합)"
        ),
        f"- new conflict total: `{conflict['new_conflict_total']}`",
        f"- raw major defect events: `{len(conflict['raw_major_defect_events'])}`",
        f"- corrected major defect count: `{conflict['corrected_major_defect_count']}`",
        f"- baseline source: `{conflict['baseline_source']}`",
        "",
        (
            "원 실행은 결과 전에 이미 판정된 충돌 기준선 28건을 새 중대 결함으로 다시 "
            "더했다. 감사에서는 새 충돌만 무효화 대상으로 계산한다."
        ),
        "",
        "## 데이터 제외 감사",
        "",
        f"- excluded: `{exclusion['numerator']} / {exclusion['denominator']}`",
        f"- ratio: `{exclusion['ratio']:.8f}` (threshold `<= {exclusion['threshold_lte']:.2f}`)",
        f"- reason counts: `{json.dumps(exclusion['reason_counts'], sort_keys=True)}`",
        f"- fully excluded market days: `{json.dumps(exclusion['fully_excluded_market_days'])}`",
        "",
        (
            "기준선 중복계산을 바로잡아도 데이터 제외율이 5%를 넘으므로 D9는 계속 "
            "실패한다. 고정 스냅샷에서 결측된 과거 1분봉 구간을 별도 검증 데이터로 복구하기 "
            "전에는 OOS나 다음 전략 후보로 진행하지 않는다."
        ),
        "",
    ]
    return "\n".join(lines)
