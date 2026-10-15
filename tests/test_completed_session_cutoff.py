"""완료-거래일 cutoff resolver + 저장 전 cutoff 필터(이슈 #72).

prevention 코어의 회귀 고정:
- NYSE 정상일/조기폐장/주말/휴장 완료 판정(EDT/EST DST 양방향).
- cutoff 당일 포함, 다음 거래일 제외(1d·1m 동일 경계).
- 형식/범위/미완료 요청 fail-closed.

순수 함수·주입 가능한 now 만 사용(코드 내부 시각 고정 금지). Toss API·DB 접근 없음.
"""
from datetime import date, datetime, timezone

import pytest

from src.history import exchange_calendar as cal
from src.history.completed_session import (
    SCOPE_OVERNIGHT,
    SCOPE_POST,
    SCOPE_REGULAR,
    CompletedThroughError,
    completion_deadline_et,
    completion_scope_for_sessions,
    last_completed_trading_day,
    parse_completed_through,
    partition_by_cutoff,
    validate_completed_through,
)
from src.history.session_filter import SessionFilter
from src.models import Candle


def _utc(iso: str) -> datetime:
    """'YYYY-MM-DDTHH:MM' (UTC) → tz-aware UTC datetime."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _c(symbol: str, iso_et: str, interval: str = "1m", close: float = 100.0) -> Candle:
    """ET-offset ISO 문자열로 Candle 생성(ts_exchange 가 그 ET 일자)."""
    m = datetime.fromisoformat(iso_et)
    return Candle.from_moment(symbol=symbol, interval=interval, moment=m,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


# --------------------------------------------------------------------------
# last_completed_trading_day — 정상일 마감 경계(EDT)
# --------------------------------------------------------------------------
def test_normal_day_before_close_is_not_completed_edt():
    # 2025-07-15(화) 19:59Z = 15:59 EDT (< 16:00) → 오늘 미완료 → 직전 거래일.
    now = _utc("2025-07-15T19:59")
    assert last_completed_trading_day(now) == date(2025, 7, 14)


def test_normal_day_after_close_is_completed_edt():
    # 2025-07-15 20:00Z = 16:00 EDT → 오늘 완료.
    now = _utc("2025-07-15T20:00")
    assert last_completed_trading_day(now) == date(2025, 7, 15)


# --------------------------------------------------------------------------
# DST 정확성: 겨울(EST, UTC-5)
# --------------------------------------------------------------------------
def test_normal_day_before_close_is_not_completed_est():
    # 2025-01-15(수) 20:59Z = 15:59 EST → 미완료.
    now = _utc("2025-01-15T20:59")
    assert last_completed_trading_day(now) == date(2025, 1, 14)


def test_normal_day_after_close_is_completed_est():
    # 2025-01-15 21:00Z = 16:00 EST → 완료.
    now = _utc("2025-01-15T21:00")
    assert last_completed_trading_day(now) == date(2025, 1, 15)


# --------------------------------------------------------------------------
# 조기폐장(13:00 ET) — 정상 16:00 로직이면 오판할 구간에서 검증
# --------------------------------------------------------------------------
def test_early_close_regular_uses_fixed_1600_not_1300():
    # ★리뷰 회귀(P1): 조기폐장일이라도 REGULAR 완료 경계는 classify_session 고정 창(16:00)과
    #   일치한다. 13:00 으로 앞당기지 않는다(그래야 저장/완료가 어긋나지 않음).
    assert cal.is_early_close(date(2025, 7, 3))
    # 12:30 EDT → 미완료
    assert last_completed_trading_day(_utc("2025-07-03T16:30")) == date(2025, 7, 2)
    # 13:30 EDT → 여전히 미완료(16:00 전)
    assert last_completed_trading_day(_utc("2025-07-03T17:30")) == date(2025, 7, 2)
    # 16:00 EDT → 완료
    assert last_completed_trading_day(_utc("2025-07-03T20:00")) == date(2025, 7, 3)


# --------------------------------------------------------------------------
# 주말·휴장 → 직전 완료 거래일
# --------------------------------------------------------------------------
def test_weekend_resolves_to_prior_friday():
    now = _utc("2025-07-19T12:00")  # 토요일
    assert last_completed_trading_day(now) == date(2025, 7, 18)


def test_full_holiday_resolves_to_prior_trading_day():
    # 2025-01-20 MLK 전휴장. 직전 거래일 = 2025-01-17(금).
    now = _utc("2025-01-20T18:00")
    assert cal.is_holiday(date(2025, 1, 20))
    assert last_completed_trading_day(now) == date(2025, 1, 17)


# --------------------------------------------------------------------------
# 세션 범위(scope) 반영 완료 경계 — POST 기본 1m 은 애프터마켓까지 끝나야 완료(#72 핵심)
# --------------------------------------------------------------------------
def test_post_scope_requires_extended_close_2000_normal():
    # 정상일 POST 완료 = 20:00 ET. 19:59 EDT 는 아직 미완료.
    assert last_completed_trading_day(_utc("2025-07-15T23:59"),  # 19:59 EDT
                                      session_scope=SCOPE_POST) == date(2025, 7, 14)
    assert last_completed_trading_day(_utc("2025-07-16T00:00"),  # 20:00 EDT
                                      session_scope=SCOPE_POST) == date(2025, 7, 15)


def test_post_scope_early_close_uses_fixed_2000():
    # ★리뷰 회귀(P1): 조기폐장일에도 POST 완료 = 20:00(classify_session POST 창 끝)과 일치.
    #   17:00·19:00 EDT 는 아직 미완료 — classify_session 이 19:00 봉을 POST 로 저장하므로
    #   17:00 으로 앞당기면 그 봉이 '완료' 후에도 새어 들어간다(이슈 #72 결함).
    assert last_completed_trading_day(_utc("2025-07-03T21:00"),   # 17:00 EDT
                                      session_scope=SCOPE_POST) == date(2025, 7, 2)
    assert last_completed_trading_day(_utc("2025-07-03T23:00"),   # 19:00 EDT
                                      session_scope=SCOPE_POST) == date(2025, 7, 2)
    assert last_completed_trading_day(_utc("2025-07-04T00:00"),   # 20:00 EDT
                                      session_scope=SCOPE_POST) == date(2025, 7, 3)


def test_completion_deadline_et_matches_classify_session_windows():
    # 완료 경계가 classify_session 고정 창(REGULAR 16:00 / POST 20:00 / OVERNIGHT 익일 04:00)과
    # 일치하고, 조기폐장일에도 좁혀지지 않음을 고정.
    for d in (date(2025, 7, 3), date(2025, 7, 15)):   # 조기폐장 / 정상
        assert completion_deadline_et(d, SCOPE_REGULAR).hour == 16
        assert completion_deadline_et(d, SCOPE_POST).hour == 20
    ov = completion_deadline_et(date(2025, 7, 3), SCOPE_OVERNIGHT)
    assert (ov.date(), ov.hour) == (date(2025, 7, 4), 4)


def test_post_scope_rejects_today_between_1600_and_2000():
    # 회귀 고정(리뷰 HIGH): 16:30 EDT 에 오늘을 POST 수집 cutoff 로 요청하면 거부.
    now = _utc("2025-07-15T20:30")  # 16:30 EDT — 정규 마감 후·애프터마켓 진행 중
    with pytest.raises(CompletedThroughError):
        validate_completed_through(date(2025, 7, 15), now, session_scope=SCOPE_POST)
    # 같은 시각도 정규(1d)라면 완료로 허용(경계가 세션 범위에 정확히 연동).
    assert validate_completed_through(date(2025, 7, 15), now,
                                      session_scope=SCOPE_REGULAR) == date(2025, 7, 15)


def test_overnight_scope_requires_next_day_0400():
    # OVERNIGHT 완료 = 익일 04:00 ET(자정 교차).
    assert last_completed_trading_day(_utc("2025-07-16T02:00"),  # 07-15 22:00 EDT
                                      session_scope=SCOPE_OVERNIGHT) == date(2025, 7, 14)
    assert last_completed_trading_day(_utc("2025-07-16T08:00"),  # 07-16 04:00 EDT
                                      session_scope=SCOPE_OVERNIGHT) == date(2025, 7, 15)


def test_completion_scope_for_sessions_mapping():
    assert completion_scope_for_sessions("1d", None) == SCOPE_REGULAR
    assert completion_scope_for_sessions(
        "1m", SessionFilter.default().allowed) == SCOPE_POST
    assert completion_scope_for_sessions(
        "1m", SessionFilter.from_spec("PRE,REGULAR").allowed) == SCOPE_REGULAR
    assert completion_scope_for_sessions(
        "1m", SessionFilter.all_sessions_optin().allowed) == SCOPE_OVERNIGHT


# --------------------------------------------------------------------------
# validate_completed_through — fail-closed 규칙
# --------------------------------------------------------------------------
def test_validate_accepts_completed_trading_day():
    now = _utc("2025-07-15T20:30")  # 16:30 EDT, 오늘 완료
    assert validate_completed_through(date(2025, 7, 15), now) == date(2025, 7, 15)


def test_validate_rejects_today_before_close():
    now = _utc("2025-07-15T19:00")  # 15:00 EDT
    with pytest.raises(CompletedThroughError):
        validate_completed_through(date(2025, 7, 15), now)


def test_validate_rejects_future_date():
    now = _utc("2025-07-15T20:30")
    with pytest.raises(CompletedThroughError):
        validate_completed_through(date(2025, 7, 16), now)


def test_validate_rejects_non_trading_day():
    now = _utc("2025-07-21T20:30")  # 월요일(완료)
    with pytest.raises(CompletedThroughError):
        validate_completed_through(date(2025, 7, 19), now)  # 토요일


def test_validate_rejects_out_of_calendar_range():
    now = _utc("2026-07-20T20:30")
    with pytest.raises(cal.CalendarRangeError):
        validate_completed_through(date(2020, 12, 31), now)  # CALENDAR_START 이전


# --------------------------------------------------------------------------
# parse_completed_through — 형식 오류 fail-closed
# --------------------------------------------------------------------------
def test_parse_rejects_bad_format():
    with pytest.raises(ValueError):
        parse_completed_through("2025/07/15")
    with pytest.raises(ValueError):
        parse_completed_through("not-a-date")


def test_parse_accepts_iso():
    assert parse_completed_through("2025-07-15") == date(2025, 7, 15)


# --------------------------------------------------------------------------
# partition_by_cutoff — 당일 포함, 다음 거래일 제외(1m·1d 동일)
# --------------------------------------------------------------------------
def test_cutoff_includes_day_excludes_next_1m():
    cut = date(2025, 7, 15)
    keep_a = _c("QQQ", "2025-07-15T15:30:00-04:00")  # REGULAR, 07-15
    keep_b = _c("QQQ", "2025-07-15T09:35:00-04:00")  # REGULAR, 07-15
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")    # PRE, 07-16
    kept, n_excl, days = partition_by_cutoff([keep_a, drop, keep_b], cut)
    assert kept == [keep_a, keep_b]          # 순서 보존, drop 제외
    assert n_excl == 1
    assert days == {"2025-07-16"}


def test_cutoff_same_boundary_for_daily():
    cut = date(2025, 7, 15)
    keep = _c("QQQ", "2025-07-15T00:00:00-04:00", interval="1d")
    drop = _c("QQQ", "2025-07-16T00:00:00-04:00", interval="1d")
    kept, n_excl, days = partition_by_cutoff([keep, drop], cut)
    assert kept == [keep]
    assert n_excl == 1
    assert days == {"2025-07-16"}


def test_cutoff_empty_when_all_after():
    cut = date(2025, 7, 14)
    c1 = _c("QQQ", "2025-07-15T15:30:00-04:00")
    c2 = _c("QQQ", "2025-07-16T15:30:00-04:00")
    kept, n_excl, days = partition_by_cutoff([c1, c2], cut)
    assert kept == []
    assert n_excl == 2
    assert days == {"2025-07-15", "2025-07-16"}


def test_cutoff_keeps_all_when_all_before_or_equal():
    cut = date(2025, 7, 16)
    c1 = _c("QQQ", "2025-07-15T15:30:00-04:00")
    c2 = _c("QQQ", "2025-07-16T15:30:00-04:00")
    kept, n_excl, days = partition_by_cutoff([c1, c2], cut)
    assert kept == [c1, c2]
    assert n_excl == 0
    assert days == set()
