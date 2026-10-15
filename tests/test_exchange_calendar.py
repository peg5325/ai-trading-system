"""NYSE 거래소 캘린더 테스트 (계약 §2.4 · 이슈 #75 Phase 1B).

검증(작업 테스트 8~10 일부):
  8.  주말·휴장·조기폐장 판정 + 기대 정규분수 + 커버 구간 밖 fail-closed
  9.  다일 휴장(연말/롱위켄드) 후에도 pagination 재개(캘린더 근거 예산으로)
  10. provider wall 에서 결정론적 종료(빈 페이지 vs 실제 소진 구별)
(9·10 의 기본 gap-skip 동작은 test_backfill_history.py 에도 있으며, 여기서는 **캘린더가
 산정한 예산**과 결부해 검증한다.)
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore  # noqa: E402
from src.history import exchange_calendar as cal  # noqa: E402
from src.ingest.backfill_history import backfill_symbol  # noqa: E402
from src.models import Candle  # noqa: E402


# ---- 8. 캘린더 단위 ----

def test_weekend_not_trading():
    assert cal.is_trading_day(date(2024, 7, 6)) is False   # 토
    assert cal.is_trading_day(date(2024, 7, 7)) is False   # 일
    assert cal.is_weekend(date(2024, 7, 6)) is True


def test_known_full_holiday():
    assert cal.is_holiday(date(2024, 7, 4)) is True        # Independence Day
    assert cal.is_trading_day(date(2024, 7, 4)) is False
    assert cal.expected_regular_minutes(date(2024, 7, 4)) == 0
    assert cal.regular_close_minute(date(2024, 7, 4)) is None
    # 특별휴장(카터 국가 애도일 2025-01-09)
    assert cal.is_holiday(date(2025, 1, 9)) is True


def test_known_early_close():
    d = date(2024, 12, 24)                                 # 크리스마스이브(화)
    assert cal.is_trading_day(d) is True
    assert cal.is_early_close(d) is True
    assert cal.expected_regular_minutes(d) == cal.EARLY_CLOSE_MINUTES == 210
    assert cal.regular_close_minute(d) == 13 * 60          # 13:00 ET


def test_normal_trading_day():
    d = date(2024, 7, 5)                                   # 금(휴일 아님)
    assert cal.is_trading_day(d) is True
    assert cal.is_early_close(d) is False
    assert cal.expected_regular_minutes(d) == 390
    assert cal.regular_close_minute(d) == 16 * 60


def test_out_of_range_fail_closed():
    with pytest.raises(cal.CalendarRangeError):
        cal.is_trading_day(date(2020, 12, 31))            # 커버 구간 이전(2021-01-01~)
    with pytest.raises(cal.CalendarRangeError):
        cal.is_trading_day(date(2027, 1, 1))              # 커버 구간 이후


def test_calendar_range_starts_2021_and_v1_start_is_frozen():
    assert cal.CALENDAR_START == date(2021, 1, 1)
    assert cal.CALENDAR_V1_START == date(2023, 1, 1)
    assert cal.is_trading_day(date(2021, 1, 4)) is True


# NYSE 2021·2022: 휴장 9일씩, 조기폐장은 Black Friday 만(07-03 이 주말, 12-24 는 관찰 휴장/토요일).
_HOLIDAYS_2021_2022 = [
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31",
    "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24",
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
    "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
]


@pytest.mark.parametrize("iso", _HOLIDAYS_2021_2022)
def test_2021_2022_full_holidays(iso):
    d = date.fromisoformat(iso)
    assert d.weekday() < 5                                 # 관찰일은 평일
    assert cal.is_holiday(d) is True
    assert cal.expected_regular_minutes(d) == 0


def test_2021_2022_early_closes_and_open_days():
    assert cal.early_close_days(date(2021, 1, 1), date(2022, 12, 31)) == [
        date(2021, 11, 26), date(2022, 11, 25)]
    assert cal.holidays_in_range(date(2021, 1, 1), date(2022, 12, 31)) == [
        date.fromisoformat(x) for x in _HOLIDAYS_2021_2022]
    assert cal.regular_close_minute(date(2022, 11, 25)) == 13 * 60
    # 개장일: Juneteenth 는 2022 부터(2021-06-18 금 개장), 2022-01-01(토) 신정은 전일 관찰 없음.
    assert cal.is_trading_day(date(2021, 6, 18)) is True
    assert cal.is_trading_day(date(2021, 12, 31)) is True
    assert cal.is_early_close(date(2021, 12, 31)) is False
    assert cal.is_early_close(date(2022, 7, 1)) is False   # 07-03 이 일요일 → 조기폐장 없음
    assert cal.is_early_close(date(2022, 12, 23)) is False
    assert len(cal.trading_days(date(2021, 1, 1), date(2021, 12, 31))) == 252
    assert len(cal.trading_days(date(2022, 1, 1), date(2022, 12, 31))) == 251


def test_max_consecutive_nontrading_mlk_long_weekend():
    """MLK 롱위켄드(2024-01-13 토·14 일·15 월 휴장) = 연속 3 비거래일."""
    span, start = cal.max_consecutive_nontrading_days(date(2024, 1, 12), date(2024, 1, 16))
    assert span == 3
    assert start == date(2024, 1, 13)
    assert cal.trading_days(date(2024, 1, 12), date(2024, 1, 16)) == [
        date(2024, 1, 12), date(2024, 1, 16)]


def test_recommended_budget_covers_span():
    # gap_skip 200분 → 빈 하루당 8회 → 3일 span → 3*8+2 = 26.
    rec = cal.recommended_max_consecutive_empty(200, date(2024, 1, 12), date(2024, 1, 16))
    assert rec == 26


def test_artifact_summary_deterministic():
    s1 = cal.artifact_summary()
    s2 = cal.artifact_summary()
    assert s1 == s2
    assert s1["schema"] == "nyse_calendar_artifact/v1"
    assert s1["full_holidays"] == sorted(s1["full_holidays"])   # 정렬 결정론


# ---- 9·10. 캘린더 근거 예산으로 다일 휴장 재개 / wall 종료 ----

def _c_at(symbol: str, iso: str) -> Candle:
    m = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return Candle.from_moment(symbol=symbol, interval="1m", moment=m,
                              o=100.0, h=101.0, low=99.0, c=100.0, v=1000.0,
                              currency="USD", adjusted=True)


class _UtcDayProvider:
    """Toss UTC-일자경계 모사: days 에 있는 UTC 일자만 1분봉 존재, 그 외 빈 페이지."""

    def __init__(self, days: set[str], window: int = 200):
        self.days = days
        self.window = window

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        assert before is not None
        end = datetime.fromisoformat(before.replace("Z", "+00:00"))
        day = end.date().isoformat()
        start_of_day = end.replace(hour=0, minute=1, second=0, microsecond=0)
        if day not in self.days or end < start_of_day:
            return ([], None)
        win_start = max(start_of_day, end - timedelta(minutes=self.window - 1))
        bars, t = [], win_start
        while t <= end:
            bars.append(_c_at(symbol, t.isoformat()))
            t += timedelta(minutes=1)
        nxt = (None if win_start <= start_of_day
               else (win_start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return (bars, nxt)


def _seed_cursor(store, symbol, before, interval="1m"):
    store.save_cursor(symbol, interval, oldest_ts_utc=None, next_before=before,
                      exhausted=False)
    store.commit()


def test_pagination_resumes_across_multiday_holiday_with_calendar_budget(tmp_path):
    """9. 3일 휴장(MLK) 을 캘린더 산정 예산으로 건너뛰고 그 이전 거래일까지 수집."""
    # 데이터 UTC 일자: 01-16(화, 재개) 와 01-12(금, 휴장 전). 01-13/14/15 는 비거래(빈 페이지).
    prov = _UtcDayProvider(days={"2024-01-16", "2024-01-12"})
    budget = cal.recommended_max_consecutive_empty(200, date(2024, 1, 12), date(2024, 1, 16))
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SPY", "2024-01-16T20:00:00Z")
        res = backfill_symbol(store, prov, "SPY", "1m", max_pages=200,
                              sleep=lambda s: None, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=budget)
        days = [r[0] for r in store._con.execute(
            "SELECT DISTINCT substr(ts_utc,1,10) FROM candle_history WHERE symbol='SPY' "
            "ORDER BY 1")]
    assert days == ["2024-01-12", "2024-01-16"], f"휴장 3일 건너뛰고 양쪽 수집: {days}"
    assert res.gap_skips >= 1


def test_true_wall_terminates_deterministically(tmp_path):
    """10. 실제 provider wall(이전 데이터 없음) 은 예산 소진 후 결정론적으로 종료."""
    prov = _UtcDayProvider(days={"2024-01-16"})     # 이전으로는 영원히 비어 있음
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SPY", "2024-01-16T20:00:00Z")
        res = backfill_symbol(store, prov, "SPY", "1m", max_pages=500,
                              sleep=lambda s: None, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=6)
    assert res.exhausted is True
    assert res.stopped_reason == "empty_budget_exhausted"
    assert res.gap_skips >= 6
