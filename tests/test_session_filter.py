"""세션 필터 테스트 (계약 §2.2 · 이슈 #75 Phase 1B).

검증(작업 테스트 1~4):
  1. PRE/REGULAR/POST 저장, OVERNIGHT 제외
  2. 세션 순서와 입력 symbol 순서 불변(결정론)
  3. unknown session fail-closed
  4. 제외 행수·ingest log 정확
전 세션 opt-in 모드도 함께 검증. 임시 DB/fixture 만 사용(기존 사용자 데이터 무접촉).
"""
from __future__ import annotations

import dataclasses
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore  # noqa: E402
from src.history.session_filter import (  # noqa: E402
    DEFAULT_ALLOWED_SESSIONS,
    SessionFilter,
    UnknownSessionError,
)
from src.ingest.backfill_history import backfill_symbol  # noqa: E402
from src.models import Candle  # noqa: E402

ET = ZoneInfo("America/New_York")


def _c(symbol: str, hh: int, mm: int, *, day: int = 7, close: float = 100.0) -> Candle:
    """ET-로컬 시각으로 1분봉 생성 → session 은 classify_session 이 자동 부여."""
    moment = datetime(2026, 7, day, hh, mm, tzinfo=ET)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=moment,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True, market="US")


# 세션별 대표 캔들 (ET): PRE 05:00, REGULAR 10:00, POST 17:00, OVERNIGHT 22:00.
def _pre():        return _c("SPY", 5, 0)
def _regular():    return _c("SPY", 10, 0)
def _post():       return _c("SPY", 17, 0)
def _overnight():  return _c("SPY", 22, 0)


def test_default_allowed_is_pre_regular_post():
    f = SessionFilter.default()
    assert sorted(f.allowed) == sorted(DEFAULT_ALLOWED_SESSIONS) == ["POST", "PRE", "REGULAR"]
    assert f.all_sessions is False


def test_partition_keeps_pre_regular_post_excludes_overnight():
    """1. PRE/REGULAR/POST 저장(kept), OVERNIGHT 제외."""
    f = SessionFilter.default()
    cands = [_pre(), _regular(), _post(), _overnight()]
    kept, excluded = f.partition(cands)
    assert [c.session for c in kept] == ["PRE", "REGULAR", "POST"]
    assert excluded == {"OVERNIGHT": 1}


def test_partition_order_and_symbol_invariant():
    """2. 세션/입력 symbol 순서 불변 — kept 는 입력 순서 그대로."""
    f = SessionFilter.default()
    seq = [_c("QQQ", 22, 0), _c("QQQ", 10, 0), _c("SPY", 5, 0),
           _c("SPY", 17, 0), _c("SPY", 22, 30)]
    kept, excluded = f.partition(seq)
    # OVERNIGHT 2개(QQQ 22:00, SPY 22:30) 제외, 나머지는 입력 순서 유지.
    assert [(c.symbol, c.session) for c in kept] == [
        ("QQQ", "REGULAR"), ("SPY", "PRE"), ("SPY", "POST")]
    assert excluded == {"OVERNIGHT": 2}


def test_unknown_session_fail_closed():
    """3. KNOWN_SESSIONS 밖 라벨 → UnknownSessionError(조용히 저장 금지)."""
    f = SessionFilter.default()
    bad = dataclasses.replace(_regular(), session="WEIRD")
    with pytest.raises(UnknownSessionError):
        f.partition([_pre(), bad, _post()])


def test_all_sessions_optin_keeps_overnight():
    f = SessionFilter.all_sessions_optin()
    kept, excluded = f.partition([_pre(), _regular(), _post(), _overnight()])
    assert [c.session for c in kept] == ["PRE", "REGULAR", "POST", "OVERNIGHT"]
    assert excluded == {}
    assert f.all_sessions is True


def test_all_sessions_still_fail_closed_on_unknown():
    """전 세션 opt-in 이어도 계약 밖 라벨은 여전히 fail-closed."""
    f = SessionFilter.all_sessions_optin()
    bad = dataclasses.replace(_overnight(), session="XYZ")
    with pytest.raises(UnknownSessionError):
        f.partition([_regular(), bad])


def test_from_spec_rejects_typo_and_conflict():
    with pytest.raises(ValueError):
        SessionFilter.from_spec("PRE,REG")           # REG 오타(미지원)
    with pytest.raises(ValueError):
        SessionFilter.from_spec("")                   # 빈 스펙
    with pytest.raises(ValueError):
        SessionFilter.from_spec("PRE", all_sessions=True)  # 모순
    f = SessionFilter.from_spec("pre, regular")       # 대소문자·공백 허용
    assert sorted(f.allowed) == ["PRE", "REGULAR"]


# --- backfill 통합: 저장 전 필터 적용 + DB/ingest_log 검증 ---

class _FakeProvider:
    """(symbol, before) → (candles, nextBefore). fetch_page 만 노출(포트 호환)."""

    def __init__(self, pages: dict):
        self.pages = pages

    def fetch_page(self, symbol: str, interval: str, *, count: int = 200,
                   before: str | None = None):
        return self.pages[(symbol, before)]


def test_backfill_applies_filter_before_write(tmp_path):
    """1+4. 세션 필터가 upsert 전에 적용되어 OVERNIGHT 이 DB 에 저장되지 않는다.

    제외 행수·ingest_log note(허용 세션·excluded·stored) 가 정확한지 확인.
    """
    page = ([_c("SPY", 5, 0), _c("SPY", 10, 0), _c("SPY", 17, 0),
             _c("SPY", 22, 0), _c("SPY", 23, 0)], None)  # PRE,REG,POST,OVN,OVN
    prov = _FakeProvider({("SPY", None): page})
    db = str(tmp_path / "market_history.sqlite")
    with HistoryStore(db) as store:
        res = backfill_symbol(store, prov, "SPY", "1m", max_pages=5,
                              session_filter=SessionFilter.default())
        # DB 에는 PRE/REGULAR/POST 만.
        stored = sorted(r[0] for r in store._con.execute(
            "SELECT session FROM candle_history WHERE symbol='SPY'"))
        assert stored == ["POST", "PRE", "REGULAR"]
        assert store._con.execute(
            "SELECT COUNT(*) FROM candle_history WHERE session='OVERNIGHT'"
        ).fetchone()[0] == 0
        # 집계: rows_seen(전체) 5, rows_stored 3, excluded OVERNIGHT 2.
        assert res.rows_seen == 5
        assert res.rows_stored == 3
        assert res.excluded_by_session == {"OVERNIGHT": 2}
        assert res.allowed_sessions == ["POST", "PRE", "REGULAR"]
        # ingest_log note 에 provenance 기록.
        note = store._con.execute(
            "SELECT note FROM ingest_log WHERE symbol='SPY' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert "sessions_allowed=POST,PRE,REGULAR" in note
        assert "excluded={OVERNIGHT:2}" in note
        assert "stored=3" in note
        assert "all_sessions=False" in note


def test_backfill_no_filter_stores_all(tmp_path):
    """session_filter=None(기본, 예: 1d) → 필터 미적용(기존 동작 보존)."""
    page = ([_c("SPY", 10, 0), _c("SPY", 22, 0)], None)
    prov = _FakeProvider({("SPY", None): page})
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        res = backfill_symbol(store, prov, "SPY", "1m", max_pages=5)
        assert store._con.execute(
            "SELECT COUNT(*) FROM candle_history").fetchone()[0] == 2
        assert res.rows_stored == 2
        assert res.excluded_by_session == {}
        assert res.allowed_sessions is None
