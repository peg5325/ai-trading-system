"""Official one-pass development runner for the frozen gross-edge screen (#124)."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET
from src.research.development_identity import verify_intraday_identity
from src.research.gross_edge.contract import compute_json_sha256, load_contract
from src.research.gross_edge.engine import (
    candidate_specs,
    extract_symbol_day,
    group_regular_by_day,
)
from src.research.gross_edge.evaluation import evaluate_candidate, overall_verdict
from src.research.orb.contract import load_contract as load_orb_contract
from src.research.orb.windows import development_eval_days, folds, previous_trading_day

TRACKED_SNAPSHOT_RECEIPT = (
    "docs/research/P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json"
)


def _et_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=ET)


def canonical_json_bytes(result: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def run_screen(
    *,
    snapshot_path: str,
    raw_root: str,
    manifest_path: str,
    snapshot_receipt_path: str = TRACKED_SNAPSHOT_RECEIPT,
) -> dict[str, Any]:
    contract = load_contract()
    orb_contract = load_orb_contract()
    expected_identity = contract["data_identity"]
    identity = verify_intraday_identity(
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        intraday_manifest_path=manifest_path,
        snapshot_receipt_path=snapshot_receipt_path,
        expected_snapshot_file=str(expected_identity["snapshot_file_sha256"]),
        expected_snapshot_content=str(expected_identity["snapshot_content_sha256"]),
        expected_intraday_manifest=str(expected_identity["manifest_data_sha256"]),
        expected_raw_content=str(expected_identity["raw_content_sha256"]),
    )

    evaluation_days = development_eval_days(orb_contract)
    fold_specs = folds(orb_contract)
    first_load_day = previous_trading_day(evaluation_days[0])
    end_exclusive = evaluation_days[-1] + timedelta(days=1)
    specs = candidate_specs(contract)
    all_trades = []
    excluded_symbol_days = 0
    for symbol in contract["universe"]:
        candles = research_load(
            snapshot_path,
            str(symbol),
            "1m",
            start=_et_midnight(first_load_day),
            end=_et_midnight(end_exclusive),
            purpose="performance",
            track=TRACK_INTRADAY,
            market="US",
        )
        by_day = group_regular_by_day(candles)
        for day in evaluation_days:
            previous = previous_trading_day(day)
            trades = extract_symbol_day(
                symbol=str(symbol),
                day=day,
                current=by_day.get(day, ()),
                previous=by_day.get(previous, ()),
                specs=specs,
            )
            if not trades:
                excluded_symbol_days += 1
            all_trades.extend(trades)

    results = [
        evaluate_candidate(
            candidate_id=spec.candidate_id,
            trades=tuple(
                trade for trade in all_trades if trade.candidate_id == spec.candidate_id
            ),
            folds=fold_specs,
            contract=contract,
        )
        for spec in specs
    ]
    payload: dict[str, Any] = {
        "schema": "p0_us30_gross_edge_screen_result_v1",
        "strategy_id": contract["strategy_id"],
        "verdict": overall_verdict(results),
        "blind_oos_accessed": True,
        "blind_oos_performance_accessed": False,
        "current_execution_blind_oos_ohlcv_accessed": False,
        "prereg_compliance": "DEVIATION_ACCEPTED_BY_APPEND_ONLY_ERRATUM",
        "erratum_path": (
            "docs/research/ERRATUM_P0_US30_GROSS_EDGE_SCREEN_OOS_IDENTITY.md"
        ),
        "development_window": {
            "start": evaluation_days[0].isoformat(),
            "end": evaluation_days[-1].isoformat(),
            "trading_days": len(evaluation_days),
        },
        "universe_count": len(contract["universe"]),
        "candidate_count": len(specs),
        "excluded_symbol_days_without_any_candidate": excluded_symbol_days,
        "prereg_json_sha256": compute_json_sha256(),
        "data_identity": {
            str(check["name"]): {
                "expected": check["expected"],
                "actual": check["actual"],
                "status": check["status"],
            }
            for check in identity
        },
        "execution_audit": {
            "review_phase": "post_result_premerge_review",
            "issue": "full_snapshot_content_hash_decoded_blind_oos_ohlcv",
            "fix": "snapshot_file_sha_plus_tracked_content_receipt",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        },
        "cost_audit": {
            "published_floor_pct_points": 0.204,
            "tracked_provenance_artifact_found": False,
            "decision_impact": (
                "none; every candidate fails the gross-edge gate before cost"
            ),
        },
        "candidates": [result.to_dict() for result in results],
        "shortlisted_candidates": [
            result.candidate_id for result in results if result.economic_pass
        ],
        "gross_edge_only_candidates": [
            result.candidate_id
            for result in results
            if result.gross_edge_pass and not result.economic_pass
        ],
    }
    digest_source = canonical_json_bytes(payload)
    payload["payload_sha256"] = hashlib.sha256(digest_source).hexdigest()
    return payload


def default_manifest_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json",
    )
