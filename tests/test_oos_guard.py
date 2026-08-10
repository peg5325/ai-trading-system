"""Blind OOS 게이트 테스트 (계약 §5.1 · 이슈 #75 Phase 1B).

검증(작업 테스트 18~19):
  18. blind OOS 조기 성과 접근이 fail-closed(unlock 없이 차단)
  19. OOS coverage 검사는 허용하되 성과 계산 경로는 차단(경계 분리)
  + unlock provenance(boolean 우회 불가), train 구간 허용, 캔들 기반 성과 가드
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore
from src.history.oos_guard import (
    TRACK_INTRADAY,
    OOSExecutionGrant,
    OOSLockError,
    OOSUnlock,
    blind_oos_window,
    guard_returns_for_candles,
    overlaps_blind_oos,
    research_load,
)
from src.history.store import ImportStats
from src.models import Candle

ET = ZoneInfo("America/New_York")


def _c(sym, dt: datetime, close=100.0) -> Candle:
    return Candle.from_moment(symbol=sym, interval="1m", moment=dt,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True, market="US")


def _make_db(tmp_path) -> str:
    """train(2025-12) 1봉 + OOS(2026-03) 1봉을 넣은 임시 DB."""
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        stats = ImportStats(source="toss_backfill")
        store.upsert_candles([
            _c("SPY", datetime(2025, 12, 15, 10, 0, tzinfo=ET)),   # train(≤2026-01-21 전)
            _c("SPY", datetime(2026, 3, 16, 10, 0, tzinfo=ET)),    # blind OOS 내부
        ], source="toss_backfill", run_id="r", stats=stats)
        store.commit()
    return db


def _valid_grant() -> OOSExecutionGrant:
    """게이트 단위 테스트용 실행 결속 객체(실 결속 검증은 별도 테스트)."""
    return OOSExecutionGrant._from_verified_execution(
        track=TRACK_INTRADAY,
        strategy_id="p0-us30-orb",
        artifact_sha256="e" * 64,
        execution_fingerprint="f" * 64,
    )


# ---- 경계/overlap 단위 ----

def test_blind_oos_window_fixed():
    from datetime import date
    assert blind_oos_window(TRACK_INTRADAY) == (date(2026, 1, 21), date(2026, 7, 21))


def test_overlaps_detection():
    lo = datetime(2026, 3, 1, tzinfo=ET)
    hi = datetime(2026, 4, 1, tzinfo=ET)
    assert overlaps_blind_oos(lo, hi, TRACK_INTRADAY) is True
    # 완전히 train 이전(2025-12) → 겹치지 않음
    assert overlaps_blind_oos(datetime(2025, 11, 1, tzinfo=ET),
                              datetime(2025, 12, 31, tzinfo=ET), TRACK_INTRADAY) is False
    # 무제한(None) → fail-closed(겹침으로 간주)
    assert overlaps_blind_oos(None, None, TRACK_INTRADAY) is True


# ---- 18. 성과 경로 fail-closed ----

def test_performance_load_on_oos_blocked_without_unlock(tmp_path):
    db = _make_db(tmp_path)
    with pytest.raises(OOSLockError):
        research_load(db, "SPY", "1m",
                      start=datetime(2026, 3, 1, tzinfo=ET),
                      end=datetime(2026, 4, 1, tzinfo=ET),
                      purpose="performance", track=TRACK_INTRADAY)


def test_performance_default_purpose_is_failclosed(tmp_path):
    """purpose 기본값이 'performance' 라 무제한 성과 로드는 기본 차단."""
    db = _make_db(tmp_path)
    with pytest.raises(OOSLockError):
        research_load(db, "SPY", "1m")     # start/end None → OOS 덮음 → 차단


def test_performance_train_window_allowed(tmp_path):
    """train(2026-01-21 이전) 구간 성과 로드는 unlock 없이 허용."""
    db = _make_db(tmp_path)
    got = research_load(db, "SPY", "1m",
                        start=datetime(2025, 11, 1, tzinfo=ET),
                        end=datetime(2025, 12, 31, tzinfo=ET),
                        purpose="performance", track=TRACK_INTRADAY)
    assert len(got) == 1 and got[0].ts_exchange.year == 2025


# ---- 19. coverage 는 허용, 성과는 차단(경계 분리) ----

def test_coverage_load_on_oos_allowed(tmp_path):
    """OOS 구간이라도 coverage 목적 로드는 허용(스키마/결측 검사 가능)."""
    db = _make_db(tmp_path)
    got = research_load(db, "SPY", "1m",
                        start=datetime(2026, 3, 1, tzinfo=ET),
                        end=datetime(2026, 4, 1, tzinfo=ET),
                        purpose="coverage", track=TRACK_INTRADAY)
    assert len(got) == 1 and got[0].ts_exchange.year == 2026
    # 같은 창을 성과 목적으로 열면 차단(경계 분리).
    with pytest.raises(OOSLockError):
        research_load(db, "SPY", "1m",
                      start=datetime(2026, 3, 1, tzinfo=ET),
                      end=datetime(2026, 4, 1, tzinfo=ET),
                      purpose="performance", track=TRACK_INTRADAY)


# ---- unlock provenance ----

def test_valid_execution_grant_opens_gate(tmp_path):
    db = _make_db(tmp_path)
    got = research_load(db, "SPY", "1m",
                        start=datetime(2026, 3, 1, tzinfo=ET),
                        end=datetime(2026, 4, 1, tzinfo=ET),
                        purpose="performance", track=TRACK_INTRADAY,
                        strategy_id="p0-us30-orb",
                        execution_grant=_valid_grant())
    assert len(got) == 1


@pytest.mark.parametrize("strategy_id", [None, "other-strategy"])
def test_execution_grant_requires_matching_strategy_id(tmp_path, strategy_id):
    db = _make_db(tmp_path)
    with pytest.raises(OOSLockError):
        research_load(
            db,
            "SPY",
            "1m",
            start=datetime(2026, 3, 1, tzinfo=ET),
            end=datetime(2026, 4, 1, tzinfo=ET),
            purpose="performance",
            track=TRACK_INTRADAY,
            strategy_id=strategy_id,
            execution_grant=_valid_grant(),
        )


def test_incomplete_unlock_stays_locked(tmp_path):
    """임의 문자열로 OOSUnlock을 직접 만들 수 없다."""
    del tmp_path
    with pytest.raises(OOSLockError, match="직접 생성 금지"):
        OOSUnlock(
            track=TRACK_INTRADAY,
            prereg_merge_commit="abc",
            implementation_commit="def",
            snapshot_hash="h",
            review_status="approved",
        )


def test_unlock_wrong_track_rejected():
    u = _valid_grant()                        # track=intraday
    assert u.is_valid_for("swing", "p0-us30-orb") is False   # 다른 track 엔 무효


# ---- 캔들 기반 성과 가드 ----

def test_guard_returns_for_candles(tmp_path):
    oos = [_c("SPY", datetime(2026, 3, 16, 10, 0, tzinfo=ET))]
    train = [_c("SPY", datetime(2025, 12, 15, 10, 0, tzinfo=ET))]
    with pytest.raises(OOSLockError):
        guard_returns_for_candles(oos, track=TRACK_INTRADAY)     # 성과 계산 직전 차단
    guard_returns_for_candles(train, track=TRACK_INTRADAY)       # train → 통과
    guard_returns_for_candles([], track=TRACK_INTRADAY)          # 빈 리스트 no-op
    guard_returns_for_candles(
        oos,
        track=TRACK_INTRADAY,
        strategy_id="p0-us30-orb",
        execution_grant=_valid_grant(),
    )  # 개봉
