"""Session Lifecycle Finalization + Coverage 테스트 (v2.1 Phase 1 · PR 1-B).

매매 로직 무관 관측/기록. 실주문/AI 없음. 결정론적(주입 시각·합성 관측봉).

계약(docs/DESIGN_DELTA_V2_1.md §2-1):
- lifecycle_state CREATED→RUNNING→TERMINATED, completion FULL/PARTIAL/ABORTED/FAILED.
- expected bars 는 캘린더+timeframe 에서 유도(78 하드코딩 금지).
- start_mode(identity) ⟂ completion_status(manifest).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from src.models import ET, regular_session_window
from src.runtime.isolation import (
    COMPLETION_ABORTED,
    COMPLETION_FAILED,
    COMPLETION_FULL,
    COMPLETION_PARTIAL,
    LIFECYCLE_CREATED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
    SessionIdentity,
    SessionManifest,
    classify_completion,
    compute_coverage,
)
from src.runtime.isolation.identity import BROKER_PAPER, SESSION_SCHEMA_VERSION


# ---- helpers ----
def _identity(**over):
    base = dict(
        session_id="007", run_id="007", parent_run_id=None, start_mode="FRESH",
        config_hash="deadbeef", producer_commit="abc123", schema_version=SESSION_SCHEMA_VERSION,
        market="US", currency="USD", initial_equity=2000.0, broker_mode=BROKER_PAPER,
        ai_provider="fake")
    base.update(over)
    return SessionIdentity(**base)


def _us_bars(indexes, day=(2026, 7, 6)):
    """US 정규장 09:30 ET 기준 bar index → ts_exchange(ET) 목록."""
    open_min = 9 * 60 + 30
    out = []
    for i in indexes:
        m = open_min + i * 5
        out.append(datetime(*day, m // 60, m % 60, tzinfo=ET))
    return out


# ---- regular_session_window ----
def test_regular_session_window():
    assert regular_session_window("US") == (9 * 60 + 30, 16 * 60)      # 570,960
    assert regular_session_window("KR") == (9 * 60, 15 * 60 + 30)      # 540,930


# ---- coverage: FULL(78봉 관측, 하드코딩 아님) ----
def test_coverage_full_us():
    cov = compute_coverage("US", 5, _us_bars(range(78)))
    assert cov.expected_bars == 78 and cov.observed_bars == 78
    assert cov.missing_bars == 0 and cov.complete is True
    assert cov.reached_end is True and cov.coverage_pct == 100.0
    assert cov.missing_zones == {"open": False, "mid": False, "close": False}


# ---- coverage: expected 는 timeframe 에서 유도(78 하드코딩 아님) ----
def test_coverage_expected_derived_from_timeframe():
    assert compute_coverage("US", 1, []).expected_bars == 390     # 390/1
    assert compute_coverage("US", 5, []).expected_bars == 78      # 390/5
    assert compute_coverage("US", 30, []).expected_bars == 13     # 390/30
    assert compute_coverage("KR", 5, []).expected_bars == 78


# ---- coverage: 조기폐장 → expected 감소 ----
def test_coverage_early_close():
    cov = compute_coverage("US", 5, [], session_close_min=13 * 60)   # 13:00 조기폐장
    assert cov.expected_bars == (13 * 60 - (9 * 60 + 30)) // 5       # 210/5 = 42
    assert cov.expected_bars == 42


# ---- coverage: 개장 zone 결측 + missing interval ----
def test_coverage_partial_missing_open_zone():
    # 개장 첫 10봉(0..9) 결측, 나머지 관측
    cov = compute_coverage("US", 5, _us_bars(range(10, 78)))
    assert cov.observed_bars == 68 and cov.missing_bars == 10
    assert cov.missing_bar_indexes == list(range(10))
    assert cov.missing_intervals == [[0, 9]]                # 연속 구간
    assert cov.missing_zones["open"] is True
    assert cov.missing_zones["close"] is False
    assert cov.reached_end is True                          # 마지막 봉(77) 관측


# ---- coverage: 마감 zone 결측 → reached_end False ----
def test_coverage_missing_close_zone_not_reached_end():
    cov = compute_coverage("US", 5, _us_bars(range(0, 60)))  # 60..77 결측
    assert cov.reached_end is False
    assert cov.missing_zones["close"] is True
    assert cov.missing_intervals == [[60, 77]]


# ---- coverage: 정규장 밖 봉은 무시 ----
def test_coverage_ignores_out_of_session_bars():
    pre = [datetime(2026, 7, 6, 9, 0, tzinfo=ET)]           # 09:00 = PRE
    cov = compute_coverage("US", 5, pre + _us_bars([0]))
    assert cov.observed_bars == 1                            # PRE 봉 제외, index0 만


# ---- classify_completion: 4상태 각각 ----
@pytest.mark.parametrize("kw,expected", [
    (dict(reached_session_end=True, coverage_complete=True), COMPLETION_FULL),
    (dict(reached_session_end=False, coverage_complete=False), COMPLETION_PARTIAL),
    (dict(reached_session_end=True, coverage_complete=False), COMPLETION_PARTIAL),
    (dict(reached_session_end=True, coverage_complete=True, aborted=True), COMPLETION_ABORTED),
    (dict(reached_session_end=True, coverage_complete=True, failed=True), COMPLETION_FAILED),
    (dict(reached_session_end=False, coverage_complete=False, aborted=True, failed=True),
     COMPLETION_FAILED),   # FAILED > ABORTED
])
def test_classify_completion(kw, expected):
    assert classify_completion(**kw) == expected


# ---- manifest: CREATED → RUNNING → TERMINATED ----
def test_manifest_lifecycle_transitions():
    m0 = SessionManifest.create(_identity(), "t0")
    assert m0.lifecycle_state == LIFECYCLE_CREATED
    m1 = m0.mark_running("t1")
    assert m1.lifecycle_state == LIFECYCLE_RUNNING and m1.updated_at == "t1"
    cov = compute_coverage("US", 5, _us_bars(range(43))).to_dict()
    m2 = m1.finalize("t2", completion_status=COMPLETION_PARTIAL,
                     termination_reason="NETWORK_SCHEDULE", coverage=cov,
                     manual_flatten_count=3)
    assert m2.lifecycle_state == LIFECYCLE_TERMINATED
    assert m2.completion_status == COMPLETION_PARTIAL
    assert m2.termination_reason == "NETWORK_SCHEDULE"
    assert m2.manual_flatten_count == 3
    assert m2.coverage["observed_bars"] == 43
    assert m2.terminated_at == "t2"


# ---- manifest roundtrip(coverage 포함) ----
def test_manifest_roundtrip_with_coverage():
    cov = compute_coverage("US", 5, _us_bars(range(78))).to_dict()
    m = SessionManifest.create(_identity(), "t0").mark_running("t1").finalize(
        "t2", completion_status=COMPLETION_FULL, coverage=cov)
    assert SessionManifest.from_dict(m.to_dict()) == m


# ---- start_mode(RESTART) ⟂ completion_status(PARTIAL) 동시 표현 ----
def test_restart_and_partial_coexist():
    m = SessionManifest.create(_identity(start_mode="RESTART", run_id="006R",
                                         parent_run_id="006", session_id="006"),
                               "t0").finalize("t2", completion_status=COMPLETION_PARTIAL)
    d = m.to_dict()
    assert d["identity"]["start_mode"] == "RESTART"
    assert d["completion_status"] == COMPLETION_PARTIAL
    assert d["identity"]["parent_run_id"] == "006"


# ============================ PaperRuntime 통합 ============================
import os

from src.live.models import FakeMarketDataProvider, LoopConfig
from src.live.paper_loop import PaperTradingLoop
from src.journal.models import InMemoryJournalStore
from src.models import Candle
from src.order.models import InMemoryPositionStore
from src.pipeline.models import PipelineResult
from src.runtime.config import RuntimeConfig
from src.runtime.isolation import SessionLock, read_manifest, write_manifest
from src.runtime.isolation.manifest import SessionManifest
from src.runtime.paper_runtime import PaperRuntime
from src.storage.candle_store import CandleStore

_NOW = lambda: "2026-07-06T20:00:00Z"


def _make_1m(n, start=datetime(2026, 7, 6, 10, 0, tzinfo=ET)):
    from datetime import timedelta
    out, prev = [], 100.0
    for i in range(n):
        m = start + timedelta(minutes=i)
        c = 100.0 + (i % 8) * 0.1
        out.append(Candle.from_moment("SOXL", "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000, "USD", True))
        prev = c
    return out


class _EmptyPipeline:
    def run_once(self, **kw):
        return PipelineResult(kw["cycle_id"], [])


def _runtime_with_session(tmp_path, *, after_cycle_stop=False):
    cfg = RuntimeConfig(symbols=["SOXL"], poll_interval_s=0.0,
                        log_dir=str(tmp_path / "logs"), status_path=str(tmp_path / "status.json"),
                        reports_dir=str(tmp_path / "reports"))
    loop = PaperTradingLoop(
        FakeMarketDataProvider({"SOXL": _make_1m(20)}), CandleStore(tmp_path / "m.sqlite"),
        _EmptyPipeline(), InMemoryPositionStore(),
        LoopConfig(universe=("SOXL",)), journal=InMemoryJournalStore(), sleep=lambda s: None)
    manifest_path = str(tmp_path / "session_manifest.json")
    lock_path = str(tmp_path / "session.lock")
    idn = _identity(market="US")
    write_manifest(manifest_path, SessionManifest.create(idn, "t_created"))
    lock = SessionLock.acquire(lock_path, idn, now_iso=lambda: "t_lock")
    rt = PaperRuntime(loop, cfg, sleep=lambda s: None, now_iso=_NOW,
                      session_manifest_path=manifest_path, session_identity=idn,
                      session_lock=lock)
    return rt, manifest_path, lock_path


# ---- 정상 종료: RUNNING→TERMINATED, coverage 기록, lock 해제(원자적) ----
def test_runtime_finalizes_manifest_and_releases_lock(tmp_path):
    rt, manifest_path, lock_path = _runtime_with_session(tmp_path)
    rt.run(max_iterations=3)
    m = read_manifest(manifest_path)
    assert m.lifecycle_state == LIFECYCLE_TERMINATED
    assert m.completion_status in (COMPLETION_FULL, COMPLETION_PARTIAL,
                                   COMPLETION_ABORTED, COMPLETION_FAILED)
    assert m.coverage is not None and m.coverage["market"] == "US"
    assert m.terminated_at == "2026-07-06T20:00:00Z"
    assert not os.path.exists(lock_path)                 # finalize 후 lock 해제
    assert not os.path.exists(manifest_path + f".{os.getpid()}.tmp")  # 원자적, tmp 잔류 없음


# ---- 장중 일부만 관측 → PARTIAL (자연 종료, abort 아님) ----
def test_runtime_partial_when_session_incomplete(tmp_path):
    rt, manifest_path, _ = _runtime_with_session(tmp_path)
    rt.run(max_iterations=3)                              # 20분치만 → 78봉 중 일부
    m = read_manifest(manifest_path)
    assert m.completion_status == COMPLETION_PARTIAL
    assert m.coverage["expected_bars"] == 78 and m.coverage["observed_bars"] < 78


# ---- 외부 stop(세션말 이전) → ABORTED ----
def test_runtime_aborted_on_external_stop(tmp_path):
    rt, manifest_path, _ = _runtime_with_session(tmp_path)
    rt.run(max_iterations=10, after_cycle=lambda r: r.stop())
    m = read_manifest(manifest_path)
    assert m.completion_status == COMPLETION_ABORTED
    assert m.termination_reason == "STOPPED"


# ---- manual flatten 관측 카운터가 manifest 에 반영 ----
def test_runtime_records_manual_flatten(tmp_path):
    rt, manifest_path, _ = _runtime_with_session(tmp_path)
    rt.record_manual_flatten()
    rt.record_manual_flatten()
    rt.run(max_iterations=1)
    assert read_manifest(manifest_path).manual_flatten_count == 2


# ---- 세션 컨텍스트 미주입(레거시) → 매니페스트 무관, 정상 동작 ----
def test_runtime_without_session_is_noop(tmp_path):
    cfg = RuntimeConfig(symbols=["SOXL"], poll_interval_s=0.0,
                        log_dir=str(tmp_path / "logs"), status_path=str(tmp_path / "s.json"),
                        reports_dir=str(tmp_path / "reports"))
    loop = PaperTradingLoop(
        FakeMarketDataProvider({"SOXL": _make_1m(20)}), CandleStore(tmp_path / "m.sqlite"),
        _EmptyPipeline(), InMemoryPositionStore(),
        LoopConfig(universe=("SOXL",)), journal=InMemoryJournalStore(), sleep=lambda s: None)
    rt = PaperRuntime(loop, cfg, sleep=lambda s: None)
    path = rt.run(max_iterations=1)                       # 예외 없이 종료, 리포트 생성
    assert os.path.exists(path)
