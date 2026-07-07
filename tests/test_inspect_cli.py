"""조회(read-only) 인스펙션 CLI 테스트.

Store 클래스(SqliteJournalStore/SqliteReviewStore/SqliteMetricsStore/SqliteSystemReviewStore)로
임시 DB 를 만들고(스키마 충실성 보장) close() 한 뒤, src.cli.inspect 의 read-only reader 가
그 파일을 안전하게 읽는지 검증한다. 실 데이터(data/*.sqlite)는 사용하지 않는다.
now() 의존 없음 — reader 는 모두 명시적 start/end 만 받는다.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cli.inspect import (
    DBError,
    _parse_since,
    main,
    read_candidates,
    read_cycles,
    read_errors,
    read_improvements,
    read_latest,
    read_metrics,
    read_orders,
    read_reviews,
    read_summary,
    read_system_review_latest,
    read_system_reviews,
)
from src.journal.models import JournalCycle
from src.journal.sqlite_store import SqliteJournalStore
from src.review.store import ReviewRecord, SqliteReviewStore
from src.runtime.config import RuntimeConfig
from src.runtime.metrics_store import CycleMetric, SqliteMetricsStore
from src.system_review.models import SystemReviewLog
from src.system_review.store import SqliteSystemReviewStore


# ---------------------------------------------------------------------------
# 시드 helper — 실제 Store 클래스로 CREATE + INSERT 후 close (스키마 충실성)
# ---------------------------------------------------------------------------

def _seed_journal(path) -> None:
    store = SqliteJournalStore(path)
    ordered_cycle = JournalCycle(
        cycle_id="20260707T041000Z", cycle_ts="2026-07-07T04:10:00Z", session="REGULAR",
        universe=["SOXL", "QQQ"], status="RAN", reason=None, error=None,
        outcomes=[
            {
                "symbol": "SOXL", "action": "ORDERED", "reason": "ORDERED",
                "ai_decision": {"decision": "APPROVE_ENTRY", "confidence_score": 84},
                "candidate": {"score": 82.5, "rank": 1, "candidate_type": "MOMENTUM", "risk_flags": []},
                "order_record": {"side": "BUY", "kind": "MARKET", "status": "FILLED",
                                 "filled_quantity": 10, "avg_fill_price": 194.8, "fee": 1.2},
                "indicator_snapshot": {"close": 194.8},
            },
            {
                "symbol": "QQQ", "action": "SKIPPED", "reason": "AI_WAIT",
                "ai_decision": {"decision": "WAIT", "confidence_score": 40},
                "candidate": None, "order_record": None, "indicator_snapshot": {"close": 480.0},
            },
        ],
        order_count=1,
    )
    error_cycle = JournalCycle(
        cycle_id="20260707T042000Z", cycle_ts="2026-07-07T04:20:00Z", session="REGULAR",
        universe=["SOXL"], status="ERROR", reason="PIPELINE_ERROR", error="boom",
        outcomes=[], order_count=0,
    )
    store.save_cycle(ordered_cycle)
    store.save_cycle(error_cycle)
    store.close()


def _cm(**kw) -> CycleMetric:
    base = dict(
        created_at="2026-07-07T04:10:00Z", cycle_id="C1", cycle_ts="2026-07-07T04:10:00Z",
        session="REGULAR", status="RAN", reason="OK", loop_ms=1200.0, ai_ms=300.0,
        fetch_latency_ms=None, health_status="OK", api_ok=True, ai_ok=True,
        broker_ok=True, journal_ok=True, replay_ok=True,
    )
    base.update(kw)
    return CycleMetric(**base)


def _seed_metrics(path) -> None:
    store = SqliteMetricsStore(path)
    store.record(_cm(cycles_run=1, orders=1, candidates=2, ai_approve=1))
    store.record(_cm(created_at="2026-07-07T04:20:00Z", cycle_id="C2",
                     cycle_ts="2026-07-07T04:20:00Z", api_errors=1))
    store.record(_cm(created_at="2026-07-07T04:30:00Z", cycle_id="C3",
                     cycle_ts="2026-07-07T04:30:00Z", health_status="DEGRADED", exceptions=1))
    store.close()


def _rr(cid, cts, *, quality="GOOD", rule=None, missed=None, conf=60,
       model="gpt-test", prompt="review_v1", rv="v1") -> ReviewRecord:
    return ReviewRecord(
        cycle_id=cid, cycle_ts=cts, symbol=None, status="REVIEW_OK",
        decision_quality=quality, missed_signals=list(missed or []),
        risk_assessment="r", alternative_action="a", rule_improvement=list(rule or []),
        confidence_reassessment=conf, model_name=model, prompt_version=prompt,
        review_version=rv, review_source="BATCH", created_at="2026-07-07T05:00:00Z", error=None)


def _seed_reviews(path) -> None:
    store = SqliteReviewStore(path)
    store.save(_rr("20260707T041000Z", "2026-07-07T04:10:00Z", quality="GOOD",
                   rule=["raise min score", "avoid overnight"]))
    store.save(_rr("20260707T042000Z", "2026-07-07T04:20:00Z", quality="POOR",
                   rule=["raise  min score!"], missed=["rsi divergence"]))
    store.close()


def _seed_system_review(path) -> None:
    store = SqliteSystemReviewStore(path)
    store.save(SystemReviewLog(
        created_at="2026-07-07T06:00:00Z", window_start="2026-07-07T00:00:00Z",
        window_end="2026-07-08T00:00:00Z", prompt_version="system_review_v1",
        model_name="gpt-5.5", input_hash="a" * 64, latency_ms=120,
        runtime_window={"window": "24h"}, status="SYSTEM_REVIEW_OK", grade="A",
        confidence=90, raw_response="{}",
        parsed_response={"grade": "A", "summary": "Runtime stable.",
                         "major_issues": ["latency spike at 03:00"],
                         "recommendations": ["increase log retention"]}, error=None))
    store.close()


def _seed_all(tmp_path):
    paths = {
        "journal": tmp_path / "journal.sqlite", "review": tmp_path / "reviews.sqlite",
        "metrics": tmp_path / "metrics.sqlite", "system_review": tmp_path / "sysrev.sqlite",
    }
    _seed_journal(paths["journal"])
    _seed_reviews(paths["review"])
    _seed_metrics(paths["metrics"])
    _seed_system_review(paths["system_review"])
    return paths


def _cfg(paths) -> RuntimeConfig:
    return RuntimeConfig(journal_db=str(paths["journal"]), review_db=str(paths["review"]),
                         metrics_db=str(paths["metrics"]), system_review_db=str(paths["system_review"]))


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def test_summary_counts(tmp_path):
    paths = _seed_all(tmp_path)
    data = read_summary(_cfg(paths))

    assert data["journal"]["available"] is True
    assert data["journal"]["cycle_count"] == 2
    assert data["journal"]["status_counts"] == {"RAN": 1, "ERROR": 1}
    assert data["journal"]["time_range"]["start"] == "2026-07-07T04:10:00Z"
    assert data["journal"]["time_range"]["end"] == "2026-07-07T04:20:00Z"

    assert data["reviews"]["review_count"] == 2
    assert data["reviews"]["quality_counts"] == {"GOOD": 1, "POOR": 1}

    assert data["metrics"]["row_count"] == 3
    assert data["metrics"]["sum_cycles_run"] == 1
    assert data["metrics"]["sum_orders"] == 1
    assert data["metrics"]["sum_api_errors"] == 1
    assert data["metrics"]["degraded_count"] == 1

    assert data["system_reviews"]["review_count"] == 1
    assert data["system_reviews"]["latest_grade"] == "A"


# ---------------------------------------------------------------------------
# latest
# ---------------------------------------------------------------------------

def test_latest_returns_newest_first_and_respects_limit(tmp_path):
    paths = _seed_all(tmp_path)
    rows = read_latest(str(paths["journal"]), limit=1)
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "20260707T042000Z"       # 최신(04:20)이 먼저

    rows_all = read_latest(str(paths["journal"]), limit=10)
    assert [r["cycle_id"] for r in rows_all] == ["20260707T042000Z", "20260707T041000Z"]


def test_cycles_oldest_first(tmp_path):
    paths = _seed_all(tmp_path)
    rows = read_cycles(str(paths["journal"]), limit=10)
    assert [r["cycle_id"] for r in rows] == ["20260707T041000Z", "20260707T042000Z"]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def test_errors_finds_error_cycle_and_metrics_row(tmp_path):
    paths = _seed_all(tmp_path)
    data = read_errors(str(paths["journal"]), str(paths["metrics"]))
    assert len(data["journal_errors"]) == 1
    assert data["journal_errors"][0]["error"] == "boom"
    assert data["journal_errors"][0]["reason"] == "PIPELINE_ERROR"

    # api_errors=1 행과 exceptions=1(DEGRADED) 행 모두 매칭
    assert len(data["metrics_errors"]) == 2
    by_created = {r["created_at"]: r for r in data["metrics_errors"]}
    assert by_created["2026-07-07T04:20:00Z"]["api_errors"] == 1
    assert by_created["2026-07-07T04:30:00Z"]["exceptions"] == 1


# ---------------------------------------------------------------------------
# candidates / orders
# ---------------------------------------------------------------------------

def test_candidates_and_orders(tmp_path):
    paths = _seed_all(tmp_path)
    cands = read_candidates(str(paths["journal"]))
    assert len(cands) == 1
    assert cands[0]["symbol"] == "SOXL"
    assert cands[0]["score"] == 82.5
    assert cands[0]["candidate_type"] == "MOMENTUM"
    assert cands[0]["ai_decision"] == "APPROVE_ENTRY"

    orders = read_orders(str(paths["journal"]))
    assert len(orders) == 1
    assert orders[0]["symbol"] == "SOXL"
    assert orders[0]["filled_quantity"] == 10
    assert orders[0]["avg_fill_price"] == 194.8

    # symbol 필터
    assert read_candidates(str(paths["journal"]), symbol="QQQ") == []
    assert read_orders(str(paths["journal"]), symbol="QQQ") == []


# ---------------------------------------------------------------------------
# reviews
# ---------------------------------------------------------------------------

def test_reviews_lists_seeded(tmp_path):
    paths = _seed_all(tmp_path)
    rows = read_reviews(str(paths["review"]))
    assert len(rows) == 2
    by_quality = {r["decision_quality"]: r for r in rows}
    assert by_quality["GOOD"]["rule_improvement_count"] == 2
    assert by_quality["POOR"]["rule_improvement_count"] == 1


# ---------------------------------------------------------------------------
# improvements (RuleImprovementAggregator 재사용)
# ---------------------------------------------------------------------------

def test_improvements_clusters_seeded_rule_improvement(tmp_path):
    paths = _seed_all(tmp_path)
    report = read_improvements(str(paths["review"]), top_n=10)
    assert report["ok_reviews"] == 2
    top = report["rule_improvement_clusters"][0]
    # "raise min score" / "raise  min score!" 는 정규화 후 동일 → count 2 로 병합
    assert top["count"] == 2
    assert top["representative"].lower().replace("!", "").strip() in ("raise min score", "raise  min score")
    assert report["missed_signal_clusters"][0]["count"] == 1


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_metrics_aggregate_sums(tmp_path):
    paths = _seed_all(tmp_path)
    agg = read_metrics(str(paths["metrics"]))
    assert agg["rows"] == 3
    assert agg["cycles_run"] == 1
    assert agg["orders"] == 1
    assert agg["api_errors"] == 1
    assert agg["exceptions"] == 1
    assert agg["degraded"] == 1
    assert agg["status_breakdown"] == {"RAN": 3}


def test_metrics_time_window_filters(tmp_path):
    paths = _seed_all(tmp_path)
    start = datetime(2026, 7, 7, 4, 25, tzinfo=timezone.utc)
    agg = read_metrics(str(paths["metrics"]), start=start)
    assert agg["rows"] == 1                       # 04:30 행만
    assert agg["exceptions"] == 1


# ---------------------------------------------------------------------------
# system-review
# ---------------------------------------------------------------------------

def test_system_review_list_and_latest(tmp_path):
    paths = _seed_all(tmp_path)
    rows = read_system_reviews(str(paths["system_review"]))
    assert len(rows) == 1 and rows[0]["grade"] == "A"

    latest = read_system_review_latest(str(paths["system_review"]))
    assert latest["grade"] == "A"
    assert latest["model_name"] == "gpt-5.5"
    # summary/major_issues/recommendations 는 parsed_response 안에 중첩되어 있으므로 거기서 추출돼야 한다
    assert latest["summary"] == "Runtime stable."
    assert latest["major_issues"] == ["latency spike at 03:00"]
    assert latest["recommendations"] == ["increase log retention"]


# ---------------------------------------------------------------------------
# --json 출력
# ---------------------------------------------------------------------------

def test_json_output_is_valid_json(tmp_path, capsys):
    paths = _seed_all(tmp_path)
    rc = main(["latest", "--db", str(paths["journal"]), "--limit", "5", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)               # 파싱 가능해야 함
    assert isinstance(parsed, list) and len(parsed) == 2


# ---------------------------------------------------------------------------
# 빈 DB (테이블은 있지만 행 없음) → 친절한 empty, exit 0
# ---------------------------------------------------------------------------

def test_empty_journal_db_no_rows(tmp_path, capsys):
    path = tmp_path / "empty_journal.sqlite"
    SqliteJournalStore(path).close()           # 스키마만 생성, 행 없음

    assert read_cycles(str(path)) == []
    assert read_latest(str(path)) == []

    rc = main(["cycles", "--db", str(path)])
    assert rc == 0
    assert "데이터 없음" in capsys.readouterr().out


def test_empty_metrics_db_aggregate_is_zero(tmp_path):
    path = tmp_path / "empty_metrics.sqlite"
    SqliteMetricsStore(path).close()
    agg = read_metrics(str(path))
    assert agg["rows"] == 0
    assert agg["cycles_run"] == 0
    assert agg["degraded"] == 0


# ---------------------------------------------------------------------------
# 존재하지 않는 DB → 친절한 에러, non-zero exit, traceback 없음
# ---------------------------------------------------------------------------

def test_missing_db_raises_friendly_dberror(tmp_path):
    missing = tmp_path / "does_not_exist.sqlite"
    try:
        read_cycles(str(missing))
        assert False, "DBError 가 발생해야 한다"
    except DBError as e:
        assert "DB 파일이 없습니다" in str(e)


def test_missing_db_cli_exit_nonzero(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.sqlite"
    rc = main(["cycles", "--db", str(missing)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "DB 파일이 없습니다" in err


# ---------------------------------------------------------------------------
# _parse_since (now() 미의존 케이스만)
# ---------------------------------------------------------------------------

def test_parse_since_iso_and_none():
    assert _parse_since(None) is None
    dt = _parse_since("2026-07-07T00:00:00Z")
    assert dt == datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
