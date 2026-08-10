"""Gap+RVOL no-external-source 종료 결정의 증거·경계 회귀 테스트."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "research"
DECISION_PATH = DOCS / "DECISION_P0_US30_GAP_RVOL_NO_EXTERNAL_SOURCE.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_decision_evidence_hashes_and_counts_match_tracked_sources() -> None:
    decision = _load(DECISION_PATH)
    evidence = decision["evidence"]

    for item in evidence.values():
        path = ROOT / item["path"]
        assert path.is_file()
        assert _sha256(path) == item["sha256"]

    result = _load(ROOT / evidence["development_result"]["path"])
    assert result["payload"]["verdict"] == {
        "failed": ["D3", "D4", "D5", "D6", "D7", "D9"],
        "next_stage": None,
        "value": "INCONCLUSIVE_DATA",
    }

    audit = _load(ROOT / evidence["d9_audit"]["path"])
    assert audit["corrected_d9"]["data_exclusion_ratio"] == 1296 / 20100
    assert audit["corrected_d9"]["verdict"] == "INCONCLUSIVE_DATA"

    plan = _load(ROOT / evidence["dependency_plan"]["path"])
    assert plan["target_count"] == 458
    assert plan["role_counts"] == {
        "prior_regular_close": 193,
        "rvol_15m_lookback": 448,
    }

    probe = _load(ROOT / evidence["dependency_probe"]["path"])
    assert probe["available_count"] == 265
    assert probe["unavailable_count"] == 193
    assert probe["unavailable_role_counts"]["prior_regular_close"] == 193


def test_decision_is_fail_closed_without_reclassifying_strategy() -> None:
    decision = _load(DECISION_PATH)

    assert decision["governance"]["decision"] == "DO_NOT_USE_EXTERNAL_PROVIDER"
    assert decision["governance"]["scientific_gate_changed"] is False
    assert decision["governance"]["strategy_contract_changed"] is False
    assert decision["research_disposition"] == {
        "next_candidate": None,
        "status": "TERMINAL_INCONCLUSIVE_DATA",
        "strategy_verdict": "INCONCLUSIVE_DATA",
    }
    assert decision["scope"]["additional_external_provider_called"] is False
    assert all(value is False for value in decision["scope"].values())
    assert any("NO_SELECTION" in claim for claim in decision["not_claimed"])


def test_plan_records_post_result_terminal_state_without_unlocking_next_candidate() -> None:
    plan = (DOCS / "PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md").read_text(
        encoding="utf-8"
    )
    decision = (DOCS / "DECISION_P0_US30_GAP_RVOL_NO_EXTERNAL_SOURCE.md").read_text(
        encoding="utf-8"
    )

    assert "| r4 | 2026-08-10 | #109 |" in plan
    assert "이번 계획의 종료 상태는 `INCONCLUSIVE_DATA`" in plan
    assert "동일 결손 데이터에 의존하는 후보 3 VWAP/ATR도 시작하지 않는다" in plan
    assert "#78 unlock" in decision
    assert "artifact도 만들지 않는다" in decision
    assert "`NO_SELECTION`이 아니다" in decision
