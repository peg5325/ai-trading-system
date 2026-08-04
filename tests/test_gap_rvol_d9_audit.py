from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.research.gap_rvol.contract import all_specs, load_contract
from src.research.gap_rvol.d9_audit import (
    audit_json_bytes,
    build_d9_audit,
    render_d9_audit,
)
from src.research.gap_rvol.development import FeaturePass
from src.research.gap_rvol.features import GapRvolDayFeatures
from src.research.gap_rvol.manifest import SnapshotDefectEvidence


def _feature(symbol: str, day: date, reasons: tuple[str, ...]) -> GapRvolDayFeatures:
    return GapRvolDayFeatures(
        symbol=symbol,
        day=day,
        previous_close=None,
        today_open=None,
        gap=None,
        rvol=None,
        retention_floor=None,
        vwap_by_minute={},
        excluded_reasons=reasons,
        filter_reasons=(),
    )


def test_d9_audit_separates_pre_result_conflicts_from_new_defects():
    contract = load_contract()
    days = (date(2024, 1, 2), date(2024, 1, 3))
    symbols = ("AAPL", "SPY")
    by_day = {
        days[0]: {
            "AAPL": _feature("AAPL", days[0], ("incomplete_rvol_window",)),
            "SPY": _feature("SPY", days[0], ()),
        },
        days[1]: {
            "AAPL": _feature("AAPL", days[1], ()),
            "SPY": _feature("SPY", days[1], ()),
        },
    }
    feature_pass = FeaturePass(
        data_days=days,
        eval_days=days,
        symbols=symbols,
        features_by_spec={spec.name: by_day for spec in all_specs(contract)},
        decision_bars_by_day={day: {} for day in days},
        defects_by_day={day: {symbol: () for symbol in symbols} for day in days},
    )
    tracked = {
        "payload": {
            "verdict": {"value": "INCONCLUSIVE_DATA", "failed": ["D9"]},
            "defect_ratios": {
                "data": {"numerator": 1, "denominator": 4},
                "major_defect_count": 28,
                "conflict_total": 28,
            },
        }
    }
    audit = build_d9_audit(
        feature_pass=feature_pass,
        tracked_result=tracked,
        snapshot_defects=SnapshotDefectEvidence(
            conflict_total=28,
            identity_failures=(),
            conflict_baseline_total=28,
            new_conflict_total=0,
            conflict_baseline_source="pre-result",
            conflict_baseline_by_symbol=(("SPY", 28),),
            snapshot_report_conflict_candles=1,
        ),
        contract=contract,
        generated_at_utc="2026-08-04T00:00:00Z",
    )
    assert audit["conflict_audit"]["corrected_major_defect_count"] == 0
    assert audit["conflict_audit"]["pre_result_baseline_by_symbol"] == {"SPY": 28}
    assert audit["conflict_audit"]["snapshot_report_conflict_candles"] == 1
    assert audit["exclusion_audit"]["numerator"] == 1
    assert audit["corrected_d9"]["verdict"] == "INCONCLUSIVE_DATA"
    assert audit_json_bytes(audit) == audit_json_bytes(audit)
    assert "blind OOS opened: `False`" in render_d9_audit(audit)


def test_tracked_pre_result_conflict_baseline_matches_frozen_manifest():
    by_symbol, total, snapshot_report_conflicts = (
        SnapshotDefectEvidence._tracked_conflict_baseline()
    )
    assert total == 28
    assert by_symbol == {"NVDA": 5, "QQQ": 7, "SOXL": 12, "SPY": 3, "VST": 1}
    assert snapshot_report_conflicts == 1


def test_tracked_d9_audit_is_self_consistent_and_keeps_oos_closed():
    json_path = Path("docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json")
    md_path = Path("docs/research/AUDIT_P0_US30_GAP_RVOL_D9.md")
    audit = json.loads(json_path.read_text(encoding="utf-8"))
    assert audit_json_bytes(audit) == json_path.read_bytes()
    assert render_d9_audit(audit).encode("utf-8") == md_path.read_bytes()
    assert audit["scope"] == {
        "blind_oos_opened": False,
        "development_only": True,
        "performance_computed": False,
        "strategy_contract_changed": False,
    }
    assert audit["conflict_audit"]["new_conflict_total"] == 0
    assert audit["conflict_audit"]["corrected_major_defect_count"] == 0
    assert audit["conflict_audit"]["snapshot_report_conflict_candles"] == 1
    assert audit["exclusion_audit"]["numerator"] == 1296
    assert audit["exclusion_audit"]["passes"] is False
    assert audit["corrected_d9"]["verdict"] == "INCONCLUSIVE_DATA"
