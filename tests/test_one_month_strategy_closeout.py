"""1개월 전략 선정 연구 종료 산출물의 증거·경계 회귀 테스트."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "research"
CLOSEOUT_PATH = DOCS / "RESULT_ONE_MONTH_STRATEGY_SELECTION_CLOSEOUT.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_closeout_evidence_hashes_match_tracked_sources() -> None:
    closeout = _load(CLOSEOUT_PATH)
    evidence = closeout["evidence"]

    for group in evidence.values():
        for item in group.values():
            path = ROOT / item["path"]
            assert path.is_file()
            assert _sha256(path) == item["sha256"]


def test_closeout_data_foundation_matches_manifests_and_replica() -> None:
    closeout = _load(CLOSEOUT_PATH)
    data = closeout["evidence"]["data_foundation"]

    daily = _load(ROOT / data["daily_manifest"]["path"])
    assert daily["summary"]["total_rows"] == data["daily_manifest"]["total_rows"]
    assert daily["summary"]["verdict_counts"] == {
        "OK": data["daily_manifest"]["symbols_ok"]
    }

    intraday = _load(ROOT / data["intraday_manifest"]["path"])
    assert (
        intraday["summary"]["total_rows"]
        == data["intraday_manifest"]["total_rows"]
    )
    assert (
        intraday["summary"]["meets_3y_count"]
        == data["intraday_manifest"]["symbols_meeting_3y"]
    )

    snapshot = _load(ROOT / data["research_snapshot"]["path"])
    assert (
        snapshot["summary"]["total_rows"]
        == data["research_snapshot"]["total_rows"]
    )
    assert snapshot["summary"]["verdict_counts"] == {
        "OK": data["research_snapshot"]["symbols_ok"]
    }

    replica = _load(ROOT / data["snapshot_replica"]["path"])
    assert replica["status"] == data["snapshot_replica"]["status"]
    assert (
        replica["restore_smoke"]["status"]
        == data["snapshot_replica"]["restore_smoke"]
    )
    assert replica["restore_smoke"]["semantic_match"] is True


def test_closeout_strategy_results_preserve_inconclusive_distinction() -> None:
    closeout = _load(CLOSEOUT_PATH)
    research = closeout["evidence"]["strategy_research"]

    h4 = (ROOT / research["h4_ranking"]["path"]).read_text(encoding="utf-8")
    assert f"H4 = {research['h4_ranking']['verdict']}" in h4
    assert f"전부 충족한 셀 = {research['h4_ranking']['go_cells']}개" in h4

    for candidate in ("orb", "gap_rvol"):
        result = _load(ROOT / research[candidate]["path"])
        assert result["payload"]["verdict"] == {
            "failed": research[candidate]["failed_gates"],
            "next_stage": None,
            "value": research[candidate]["verdict"],
        }

    decision = _load(ROOT / research["source_boundary_decision"]["path"])
    assert (
        decision["governance"]["decision"]
        == research["source_boundary_decision"]["decision"]
    )
    assert decision["research_disposition"] == {
        "next_candidate": None,
        "status": research["source_boundary_decision"]["status"],
        "strategy_verdict": "INCONCLUSIVE_DATA",
    }


def test_closeout_is_fail_closed_without_claiming_strategy_failure() -> None:
    closeout = _load(CLOSEOUT_PATH)

    assert closeout["final_state"] == "INCONCLUSIVE_DATA"
    assert closeout["period"]["terminal_state_reached_early"] is True
    assert closeout["scope"]["selected_strategy"] is None
    assert all(
        value is False
        for key, value in closeout["scope"].items()
        if key != "selected_strategy"
    )
    assert any("NO_SELECTION" in claim for claim in closeout["not_claimed"])
    assert len(closeout["resume_requires"]) == 4


def test_plan_records_r5_closeout_without_marking_skipped_stages_complete() -> None:
    plan = (DOCS / "PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md").read_text(
        encoding="utf-8"
    )
    report = (DOCS / "RESULT_ONE_MONTH_STRATEGY_SELECTION_CLOSEOUT.md").read_text(
        encoding="utf-8"
    )

    assert "| r5 | 2026-08-10 | #111 |" in plan
    assert "### r5 종료 적용성 감사" in plan
    assert "blind OOS·walk-forward | 해당 없음" in plan
    assert "Paper·AI shadow | 해당 없음" in plan
    assert "terminal 상태 때문에 실행하지 않은 항목" in plan
    assert "최종 상태: **`INCONCLUSIVE_DATA`**" in report
    assert "`NO_SELECTION` 판정이 아니다" in report
    assert "#78 unlock, VWAP/ATR, Paper, AI shadow" in report
