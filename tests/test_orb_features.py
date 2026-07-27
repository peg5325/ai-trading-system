"""ORB opening range · gap · RVOL 피처 테스트 — 이슈 #91 (4-A).

사전등록 §5.1(gap)/§5.2(RVOL)/§6.1(opening range)/§11.2[A](데이터 사유 4종)을 손계산
fixture 로 검증한다. 성과·PnL 은 계산하지 않는다(4-A 는 피처까지).

v1.1(수정 라운드 1, 독립 리뷰 2-lane 반영): RVOL 인과성·창 길이 일치·gap 날짜 정합성을
구조적으로 강제하는 API 변경(A1~A3)에 맞춰 기존 테스트를 갱신하고, 그 강제 자체를 검증하는
테스트(및 D2 다중일 혼입 가드)를 추가했다.
"""
from __future__ import annotations

import dataclasses
import inspect
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.history import exchange_calendar as cal
from src.models import MARKET_US, Candle
from src.research.orb.features import (
    DATA_EXCLUSION_REASONS,
    OpeningRange,
    RvolWindow,
    SymbolDayFeatures,
    compute_symbol_day_features,
    cum_today_volume,
    gap_for_day,
    opening_range_for_spec,
    regular_window_volume,
    window_sum_for_lookback,
)

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# R4-1(lane2 C12) — RvolWindow.push 가 이제 거래일 연속(및 R4-2 거래일 자체)을 강제하므로,
# 임의 달력일 시퀀스(date(2024,1,1)+timedelta(days=k), 주말·공휴일 포함)로는 더 이상 fixture
# 를 구성할 수 없다. 실제 거래일만 돌려주는 헬퍼 두 개로 대체한다.
# --------------------------------------------------------------------------
def _trading_days_from(start: date, n: int) -> tuple[date, ...]:
    """`start`(포함, 거래일이어야 함)부터 연속 거래일 n개."""
    end = start + timedelta(days=n * 3 + 20)   # 넉넉한 여유(주말·공휴일 감안)
    days = tuple(cal.trading_days(start, end))
    assert days and days[0] == start, f"{start} 는 거래일이 아니다"
    return days[:n]


def _trading_days_ending(end: date, n: int) -> tuple[date, ...]:
    """`end`(포함, 거래일이어야 함)로 끝나는 연속 거래일 n개 — RvolWindow 사전 seed 가
    `day` 바로 앞 거래일에서 끊기지 않게 한다(R4-1)."""
    start = end - timedelta(days=n * 3 + 20)
    days = tuple(cal.trading_days(start, end))
    assert days and days[-1] == end, f"{end} 는 거래일이 아니다"
    return days[-n:]


def _bar(symbol: str, moment_et: datetime, *, o: float, h: float, low: float, c: float,
        v: float) -> Candle:
    return Candle.from_moment(symbol=symbol, interval="1m", moment=moment_et,
                              o=o, h=h, low=low, c=c, v=v,
                              currency="USD", adjusted=True, market=MARKET_US)


def _minute_bars(symbol: str, d: date, start_h: int, start_m: int, count: int, *,
                 base: float = 100.0) -> list[Candle]:
    out = []
    t = datetime(d.year, d.month, d.day, start_h, start_m, tzinfo=ET)
    for i in range(count):
        out.append(_bar(symbol, t + timedelta(minutes=i),
                        o=base + i, h=base + i + 1, low=base + i - 1, c=base + i + 0.5,
                        v=1000 + i))
    return out


# --------------------------------------------------------------------------
# DATA_EXCLUSION_REASONS — 정확히 4종
# --------------------------------------------------------------------------
def test_data_exclusion_reasons_are_exactly_four():
    assert set(DATA_EXCLUSION_REASONS) == {
        "incomplete_opening_range", "no_previous_regular_close",
        "insufficient_rvol_lookback", "opening_range_not_closed",
    }
    assert len(DATA_EXCLUSION_REASONS) == 4


# --------------------------------------------------------------------------
# opening range — 손계산
# --------------------------------------------------------------------------
def test_opening_range_hand_calculated_high_low():
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 16)   # 09:30..09:45(닫힘 증거)
    orr, reasons = opening_range_for_spec(tuple(bars), minutes=15, day=d)
    assert reasons == ()
    window = bars[:15]
    assert orr == OpeningRange(high=max(b.high for b in window), low=min(b.low for b in window))


def test_opening_range_14_of_15_minutes_is_incomplete():
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 16)
    missing_one = bars[:14] + bars[15:16]       # 09:44 분 결측, 09:45 로 종료 증거는 있음
    orr, reasons = opening_range_for_spec(tuple(missing_one), minutes=15, day=d)
    assert orr is None
    assert reasons == ("incomplete_opening_range",)


def test_opening_range_not_closed_is_a_distinct_reason_from_incomplete():
    """분은 15개 다 있는데 창이 끝났다는 증거(09:45 이후 봉)가 없으면 다른 사유여야 한다."""
    d = date(2024, 3, 4)
    complete_but_unclosed = _minute_bars("AAPL", d, 9, 30, 15)   # 09:30..09:44, 그 이상 없음
    orr_unclosed, reasons_unclosed = opening_range_for_spec(
        tuple(complete_but_unclosed), minutes=15, day=d)
    assert orr_unclosed is None
    assert reasons_unclosed == ("opening_range_not_closed",)

    incomplete = complete_but_unclosed[:14]     # 14개만(닫힘 증거도 없음)
    orr_incomplete, reasons_incomplete = opening_range_for_spec(
        tuple(incomplete), minutes=15, day=d)
    assert orr_incomplete is None
    assert reasons_incomplete == ("incomplete_opening_range",)

    assert reasons_unclosed != reasons_incomplete


def test_opening_range_sensitivity_5_minutes():
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 6)    # 09:30..09:35(닫힘 증거)
    orr, reasons = opening_range_for_spec(tuple(bars), minutes=5, day=d)
    assert reasons == ()
    window = bars[:5]
    assert orr == OpeningRange(high=max(b.high for b in window), low=min(b.low for b in window))


def test_opening_range_for_spec_raises_on_multi_day_input():
    """D2(독립 리뷰 M3) — 이틀치를 한 번에 넘기면 '가장 이른 날' OR 을 조용히 반환하는 대신
    예외를 던진다. 호출자는 반드시 한 종목의 하루치만 넘겨야 한다(§8.7)."""
    d1 = date(2024, 3, 4)
    d2 = date(2024, 3, 5)
    two_days = tuple(_minute_bars("AAPL", d1, 9, 30, 16) + _minute_bars("AAPL", d2, 9, 30, 16))
    with pytest.raises(ValueError, match="day_candles 에 day="):
        opening_range_for_spec(two_days, minutes=15, day=d1)


def test_opening_range_for_spec_raises_rather_than_silently_returning_wrong_day_or():
    """H2(lane4 최우선, team-lead 재현) — 2024-03-04(16봉 완전) + 2024-03-05(14봉 불완전, 닫힘
    증거도 없음)를 섞어 day=d2 로 호출하면, 예전엔 `opening_range()` 가 만드는 OR 이 1개(d1
    것)뿐이라 개수 기반 가드(`len(...) > 1`)가 침묵하고 '가장 이른 날'의 OR 을 조용히
    돌려줬다(반환 사유는 빈 튜플). 이제는 day 로 검증하므로(v1.2) 무조건 예외다."""
    d1 = date(2024, 3, 4)
    d2 = date(2024, 3, 5)
    complete_d1 = _minute_bars("AAPL", d1, 9, 30, 16)     # 09:30..09:45(닫힘 증거) — 완전
    incomplete_d2 = _minute_bars("AAPL", d2, 9, 30, 14)   # 09:30..09:43 — 14/15(불완전)
    mixed = tuple(complete_d1 + incomplete_d2)
    with pytest.raises(ValueError, match="day_candles 에 day="):
        opening_range_for_spec(mixed, minutes=15, day=d2)   # 호출자가 원한 건 d2 의 OR


def test_opening_range_for_spec_raises_when_two_symbols_share_the_same_day():
    """R4-6(lane3 q-1, team-lead 재현) — `len(closed_complete) > 1` 가드의 옛 주석이 "day 검증을
    이미 통과한 뒤라 정상적으로는 도달하지 않는다"고 적었으나 틀렸다: `day` 검증은 **날짜만**
    보고 symbol 은 보지 않으므로, 같은 날짜의 **두 종목**을 섞으면 실제로 발동한다
    (`opening_range()` 는 (symbol, 거래일) 로 그룹핑하므로 종목이 다르면 그룹도 다르다).
    가드 자신의 에러 메시지("여러 종목을 섞어 넘겼을 수 있다")가 가리키는 바로 그 경로다."""
    d = date(2024, 3, 4)
    aapl_bars = _minute_bars("AAPL", d, 9, 30, 16)
    msft_bars = _minute_bars("MSFT", d, 9, 30, 16)
    mixed = tuple(aapl_bars + msft_bars)
    with pytest.raises(ValueError, match="둘 이상의 opening range 가 나왔다"):
        opening_range_for_spec(mixed, minutes=15, day=d)


def test_opening_range_for_spec_raises_when_two_symbols_share_the_same_day_and_neither_closes():
    """R5-6(lane4 F30, team-lead 재현, 라운드 2 부터 생존) — R4-6 은 첫째 분기
    (`len(closed_complete) > 1`)만 닫았다. 둘째 분기(`len(complete_regardless_of_closed) > 1`)
    는 같은 기법으로도 여전히 살아 있었다: 같은 날 두 종목을 섞되 **둘 다 닫힘 증거가 없으면**
    (분은 15개로 꽉 찼지만 그 이후 봉이 없어 `closed_only=True` 로는 아무것도 안 나옴)
    `closed_complete` 가 0(>1 아님)이라 첫째 분기는 침묵하고, `closed_only=False` 재시도에서
    둘 다 살아나 둘째 분기가 발동한다."""
    d = date(2024, 3, 4)
    aapl_bars = _minute_bars("AAPL", d, 9, 30, 15)   # 09:30..09:44, 그 이상 없음(닫힘 증거 없음)
    msft_bars = _minute_bars("MSFT", d, 9, 30, 15)   # 마찬가지
    mixed = tuple(aapl_bars + msft_bars)
    with pytest.raises(ValueError, match="둘 이상의 opening range 후보가 나왔다"):
        opening_range_for_spec(mixed, minutes=15, day=d)


# --------------------------------------------------------------------------
# gap — 손계산 + 조기폐장 부등호 + 날짜 정합성(A3) + 09:30 부재 매핑(B7)
# --------------------------------------------------------------------------
def test_gap_hand_calculated_normal_day():
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)   # today_open = 150
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)   # 전 정규장
    prev_close = prev_bars[-1].close
    gap, reasons = gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_d,
                              prev_day_regular_1m=tuple(prev_bars))
    assert reasons == ()
    expected = (today_bars[0].open - prev_close) / prev_close
    assert gap == pytest.approx(expected)


def test_gap_early_close_previous_day_excludes_extended_bars_after_1300():
    """조기폐장일 직전 거래일에서 13:00 이후 연장봉을 prev_close 로 쓰지 않는다(부등호 검증).

    `d` 는 2024-07-05(금) — `prev_ec`(2024-07-03, 조기폐장일)가 그 날의 캘린더상 진짜 직전
    거래일이어야 한다(M2, v1.2): 07-04(목)는 독립기념일 휴장이라 건너뛴다.
    """
    d = date(2024, 7, 5)
    prev_ec = date(2024, 7, 3)   # 캘린더 조기폐장일(13:00 ET) — d 의 진짜 직전 거래일
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)

    legit_bars = _minute_bars("AAPL", prev_ec, 9, 30, 210, base=50.0)   # 09:30..12:59(390행 아님, 210분)
    legit_close = legit_bars[-1].close
    extended = [
        _bar("AAPL", datetime(prev_ec.year, prev_ec.month, prev_ec.day, 13, 0, tzinfo=ET),
            o=999, h=999, low=999, c=999.0, v=1),
        _bar("AAPL", datetime(prev_ec.year, prev_ec.month, prev_ec.day, 13, 5, tzinfo=ET),
            o=999, h=999, low=999, c=999.5, v=1),
    ]
    all_prev = tuple(legit_bars + extended)

    gap, reasons = gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_ec,
                              prev_day_regular_1m=all_prev)
    assert reasons == ()
    expected = (today_bars[0].open - legit_close) / legit_close
    assert gap == pytest.approx(expected)
    # 대조군: 연장봉을 prev_close 로 잘못 쓰면 다른 값이 나와야 한다(회귀 감지력 증명).
    wrong = (today_bars[0].open - extended[-1].close) / extended[-1].close
    assert gap != pytest.approx(wrong)


def test_gap_no_previous_regular_bar_is_undefined():
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars = _minute_bars("AAPL", d, 9, 30, 1)
    gap, reasons = gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_d,
                              prev_day_regular_1m=())
    assert gap is None
    assert reasons == ("no_previous_regular_close",)


def test_gap_missing_todays_open_bar_maps_to_incomplete_opening_range():
    """B7(독립 리뷰 L1) — 당일 09:30 1분봉이 없으면 `incomplete_opening_range` 로 처리한다.
    `(0.0, ())` 처럼 조용히 0 을 흘리지 않는다(그 봉은 opening range 창의 첫 분이기도 하다)."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars_missing_open = _minute_bars("AAPL", d, 9, 31, 1, base=150.0)   # 09:31 부터 시작
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)
    gap, reasons = gap_for_day(today_regular_1m=tuple(today_bars_missing_open), day=d,
                              prev_day=prev_d, prev_day_regular_1m=tuple(prev_bars))
    assert gap is None
    assert reasons == ("incomplete_opening_range",)


def test_gap_for_day_raises_when_prev_bars_are_not_from_prev_day():
    """A3(독립 리뷰 BLOCKER-3) — prev_day_regular_1m 에 prev_day 가 아닌 날짜가 섞이면 예외.

    실제 사고 시나리오: 호출자가 "마지막으로 본 비어있지 않은 날"을 캐시했다가 조용히 더 오래된
    종가로 gap 을 내는 경우 — 이 검사가 없으면 그 오류가 조용히 통과한다.
    """
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    stale_day = date(2024, 2, 20)   # prev_d 가 아닌 훨씬 이전 날짜
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)
    stale_bars = _minute_bars("AAPL", stale_day, 9, 30, 390, base=100.0)
    with pytest.raises(ValueError, match="prev_day_regular_1m 에 prev_day="):
        gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_d,
                    prev_day_regular_1m=tuple(stale_bars))


def test_gap_for_day_allows_empty_prev_bars_without_date_check_false_positive():
    """빈 prev_day_regular_1m(= no_previous_regular_close 케이스)은 날짜 검사에 걸리지 않아야 한다."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars = _minute_bars("AAPL", d, 9, 30, 1)
    gap, reasons = gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_d,
                              prev_day_regular_1m=())
    assert gap is None
    assert reasons == ("no_previous_regular_close",)


def test_gap_for_day_raises_when_today_bars_are_not_from_day():
    """M1(lane4, BLOCKER-3 의 비대칭) — `prev_day_regular_1m` 쪽만 엄격히 검증하고
    `today_regular_1m` 쪽은 방치하면, 완전히 다른 날 봉을 넘겨도 gap 이 산출되고 사유가
    0 이 된다. 이제는 오늘 쪽도 날짜를 대조하고 불일치면 raise 한다."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    wrong_day = date(2024, 3, 5)
    wrong_today_bars = _minute_bars("AAPL", wrong_day, 9, 30, 1, base=150.0)   # day=d 가 아님
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)
    with pytest.raises(ValueError, match="today_regular_1m 에 day="):
        gap_for_day(today_regular_1m=tuple(wrong_today_bars), day=d, prev_day=prev_d,
                    prev_day_regular_1m=tuple(prev_bars))


def test_gap_for_day_raises_when_prev_day_is_not_the_calendar_previous_trading_day():
    """M2(lane4) — 봉이 `prev_day` 의 것인지만 보고 `prev_day` 자체가 캘린더상 진짜 직전
    거래일인지는 검증하지 않으면, 훨씬 이전 날짜를 `prev_day` 로 넘겨도 통과한다(§5.1 위반)."""
    d = date(2024, 3, 4)
    ten_trading_days_earlier = date(2024, 2, 20)   # 실제 직전 거래일(2024-03-01)이 아님
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)
    prev_bars = _minute_bars("AAPL", ten_trading_days_earlier, 9, 30, 390, base=100.0)
    with pytest.raises(ValueError, match="캘린더상 직전 거래일이 아니다"):
        gap_for_day(today_regular_1m=tuple(today_bars), day=d,
                    prev_day=ten_trading_days_earlier, prev_day_regular_1m=tuple(prev_bars))


# --------------------------------------------------------------------------
# R4-4(lane1 §6-A, team-lead 재현) — 570 하드코딩 방어를 형제 두 곳에 전파
# --------------------------------------------------------------------------
def test_gap_for_day_today_open_minute_is_not_hardcoded_570(monkeypatch):
    """C5 는 `loading.py` 의 570 하드코딩만 고쳤다. 형제 두 곳(`gap_for_day` 의 today_open,
    `regular_window_volume` 의 RVOL 창)은 `regular_session_window("US")` 를 하드코딩(570)
    으로 되돌려도 기존 테스트가 전부 통과했다(team-lead 실측: 151 passed — 생존).
    `regular_session_window` 를 실제로 몽키패치해 `gap_for_day` 가 그 값을 실제로 쓰는지
    확인한다(하드코딩이면 이 몽키패치와 무관하게 570 을 봐서 today_open 을 못 찾는다)."""
    import src.research.orb.features as features_mod

    def fake_window(market: str) -> tuple[int, int]:
        return (600, 960)   # 개장을 570 대신 600 으로

    monkeypatch.setattr(features_mod, "regular_session_window", fake_window)
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_open_at_600 = _bar("AAPL", datetime(d.year, d.month, d.day, 10, 0, tzinfo=ET),
                             o=777.0, h=777.0, low=777.0, c=777.0, v=1)   # minute-of-day=600
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)
    gap, reasons = gap_for_day(today_regular_1m=(today_open_at_600,), day=d, prev_day=prev_d,
                              prev_day_regular_1m=tuple(prev_bars))
    assert reasons == ()      # 하드코딩(570)이면 이 600분 봉을 today_open 으로 못 찾아 배제된다
    prev_close = prev_bars[-1].close
    assert gap == pytest.approx((777.0 - prev_close) / prev_close)


def test_regular_window_volume_open_minute_is_not_hardcoded_570(monkeypatch):
    """R4-4 — `regular_window_volume`(RVOL 창)의 형제 하드코딩도 propagate 한다(위 테스트와
    같은 회귀 유형)."""
    import src.research.orb.features as features_mod

    def fake_window(market: str) -> tuple[int, int]:
        return (600, 960)

    monkeypatch.setattr(features_mod, "regular_session_window", fake_window)
    d = date(2024, 3, 4)
    in_window_at_600 = [
        _bar("AAPL", datetime(d.year, d.month, d.day, 10, m, tzinfo=ET),
            o=1, h=1, low=1, c=1, v=100.0 + m)
        for m in range(15)
    ]   # 10:00..10:14 = minute-of-day 600..614(몽키패치된 개장 기준 창)
    total, observed = regular_window_volume(tuple(in_window_at_600), day=d, minutes=15)
    assert observed == 15    # 하드코딩(570)이면 570~584 창을 봐서 0 이 나온다
    assert total == sum(b.volume for b in in_window_at_600)


# --------------------------------------------------------------------------
# RVOL — 분자(미래 배제) / 분모(불완전 배제) / median / insufficient
# --------------------------------------------------------------------------
def test_cum_today_volume_excludes_bars_beyond_the_opening_window():
    d = date(2024, 3, 4)
    in_window = _minute_bars("AAPL", d, 9, 30, 15, base=1.0)   # 09:30..09:44
    future_spike = _bar("AAPL", datetime(d.year, d.month, d.day, 9, 45, tzinfo=ET),
                        o=1, h=1, low=1, c=1, v=10_000_000.0)   # 창 밖 — 절대 포함되면 안 됨
    total_without_future = cum_today_volume(tuple(in_window), day=d, minutes=15)
    total_with_future = cum_today_volume(tuple(in_window) + (future_spike,), day=d, minutes=15)
    assert total_without_future == sum(b.volume for b in in_window)
    assert total_with_future == total_without_future    # 미래 거래량 미반영(동일해야 함)


def test_window_sum_for_lookback_requires_full_window():
    d = date(2024, 3, 4)
    full = _minute_bars("AAPL", d, 9, 30, 15, base=1.0)
    partial = full[:14]
    assert window_sum_for_lookback(tuple(full), day=d, minutes=15) == sum(b.volume for b in full)
    assert window_sum_for_lookback(tuple(partial), day=d, minutes=15) is None


def test_rvol_denominator_excludes_incomplete_lookback_days_and_changes_value():
    """불완전한 과거일을 분모에 포함하면(v.s. 제외) RVOL 값이 실제로 달라져야 한다."""
    days = _trading_days_from(date(2024, 1, 2), 16)   # R4-1 — 연속 거래일 16개(14+1+query)
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    complete_sums = [1000.0 + k * 10 for k in range(14)]
    for k, s in enumerate(complete_sums):
        window.push(days[k], s)
    # 15번째 날은 창이 불완전 — None 으로 push(제외).
    window.push(days[14], None)
    query_day = days[15]
    excluded_rvol = window.rvol(query_day, today_sum=2000.0)
    assert excluded_rvol is None       # 유효일 14 < 15 → insufficient

    # 대조군: 같은 15번째 날의 부분 합을 "완전한 값"인 것처럼 넣으면(오류 시나리오) 유효일이
    # 15 로 채워져 RVOL 이 산출된다 — 이 값이 올바른 처리(None)와 달라야 한다(회귀 감지력).
    window_wrong = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    for k, s in enumerate(complete_sums):
        window_wrong.push(days[k], s)
    window_wrong.push(days[14], 50.0)     # 불완전한 날의 부분합을 그대로 넣은 오류 시나리오
    wrong_rvol = window_wrong.rvol(query_day, today_sum=2000.0)
    assert wrong_rvol is not None
    assert wrong_rvol != excluded_rvol


def test_rvol_insufficient_at_14_valid_days_computed_at_15():
    days = _trading_days_from(date(2024, 1, 2), 16)
    win14 = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    for k in range(14):
        win14.push(days[k], 100.0 + k)
    assert win14.rvol(days[14], 200.0) is None

    win15 = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    for k in range(15):
        win15.push(days[k], 100.0 + k)
    result = win15.rvol(days[15], 200.0)
    assert result is not None
    vals = sorted(100.0 + k for k in range(15))
    median = vals[7]                      # n=15(홀수) → 가운데 값
    assert result == pytest.approx(200.0 / median)


def test_rvol_median_of_even_sample_is_midpoint_average():
    vals = [10.0, 20.0, 5.0, 15.0, 12.0, 8.0, 30.0, 25.0, 1.0, 2.0, 3.0, 4.0, 6.0, 7.0, 9.0, 11.0]
    assert len(vals) == 16                # 짝수 표본
    days = _trading_days_from(date(2024, 2, 1), 17)
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    for k, v in enumerate(vals):
        window.push(days[k], v)
    result = window.rvol(days[len(vals)], 100.0)
    s = sorted(vals)
    expected_median = (s[7] + s[8]) / 2.0     # 가운데 두 값의 산술평균
    assert result == pytest.approx(100.0 / expected_median)


def test_rvol_zero_median_is_undefined():
    days = _trading_days_from(date(2024, 3, 1), 16)
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    for k in range(15):
        window.push(days[k], 0.0)
    assert window.rvol(days[15], 100.0) is None


def test_rvol_window_push_rejects_out_of_order_and_duplicate_days():
    """R5-1(lane3, team-lead 재현) — 이 두 입력(중복/역순)은 순서 검사(day<=last_day)와
    연속성 검사(R4-1, previous_trading_day(day)!=last_day) 가 **함께** 발동한다: day<=last
    이면 previous_trading_day(day) < day <= last 이므로 항상 last 와 다르다. `match=` 없이는
    순서 검사를 통째로 지워도(연속성 검사가 흡수해) 이 테스트가 계속 통과한다(team-lead
    재현: 165개 전부 통과). 순서 검사의 메시지를 직접 겨냥해 그 흡수 관계가 깨지면 죽게 한다."""
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    window.push(date(2024, 1, 10), 100.0)
    with pytest.raises(ValueError, match="시간 순서대로만 호출된다"):
        window.push(date(2024, 1, 10), 200.0)     # 중복 — 순서 검사를 겨냥
    with pytest.raises(ValueError, match="시간 순서대로만 호출된다"):
        window.push(date(2024, 1, 9), 50.0)       # 역순 — 순서 검사를 겨냥


# --------------------------------------------------------------------------
# R4-1(lane2 C12 후속, 권고안 A) — RvolWindow.push 가 거래일 연속성을 강제한다
# --------------------------------------------------------------------------
def test_rvol_window_push_rejects_a_gap_that_skips_a_trading_day():
    """R4-1 — 4-B 가 데이터 없는 종목-일에서 `continue` 하면(=그 날 push 를 건너뛰면) lookback
    이 조용히 캘린더 20 → 39 거래일로 늘어나던 구멍을 닫는다. 거래일 하나를 건너뛰고 push
    하면 즉시 예외(R5-3 — `match=` 로 연속성 검사를 겨냥한다: day > last 이므로 순서 검사는
    이 입력에서 발동하지 않는다 — 위 테스트와 반대로 여기는 이미 모호하지 않다)."""
    days = _trading_days_from(date(2024, 1, 2), 3)   # days[0], days[1], days[2] 연속 거래일
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    window.push(days[0], 100.0)
    with pytest.raises(ValueError, match="거래일 연속이어야 한다"):
        window.push(days[2], 200.0)   # days[1] 을 건너뛰었다 — 연속이 아님


def test_rvol_window_push_allows_consecutive_trading_days_including_across_a_weekend():
    """R4-1 정상 경로 확인(lane2 프로토타입) — warm-up→평가·fold 경계·dev→OOS 는 전부 연속
    거래일이므로 주말을 건너뛰는 정상 진행은 막히면 안 된다."""
    days = _trading_days_from(date(2024, 1, 2), 6)   # 화수목금 + 주말 건너뛴 다음 월요일 포함
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    for d in days[:-1]:
        window.push(d, 100.0)
    result = window.rvol(days[-1], 150.0)
    assert result == pytest.approx(1.5)   # 이전 값이 전부 100 이므로 median=100, 150/100=1.5


# --------------------------------------------------------------------------
# R4-2(lane3 q-3) — 비거래일(휴장일)을 day 로 넘기면 거부한다
# --------------------------------------------------------------------------
def test_rvol_window_push_rejects_a_holiday_even_as_the_first_push():
    """R4-2 — 휴장일은 연속성 검사(R4-1)가 적용되기도 전(첫 push)에 그 자체로 거부돼야 한다."""
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    with pytest.raises(ValueError, match="거래일이 아니다"):
        window.push(date(2024, 7, 4), 100.0)   # 독립기념일, 휴장 — 첫 push 인데도 거부


def test_rvol_window_push_rejects_holiday_immediately_not_only_on_the_next_push():
    """R4-2 — `previous_trading_day` 기반 연속성 검사(R4-1)만으로는 휴장일 자체를 못 잡는다:
    `previous_trading_day(휴장일)` 이 마지막 push 와 우연히 일치할 수 있어서다(휴장일 다음
    날 push 할 때가 돼서야 뒤늦게 어긋난 메시지로 터진다). `is_trading_day(day)` 를 독립적으로
    검사해 위반 시점에 바로 잡는다."""
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    window.push(date(2024, 7, 3), 100.0)   # 수요일, 조기폐장이지만 정상 거래일
    with pytest.raises(ValueError, match="거래일이 아니다"):
        # 목요일 7/4 는 독립기념일 휴장 — previous_trading_day(7/4) == 7/3(직전 push 일)이라
        # R4-1 의 연속성 검사만으로는 통과했을 자리다.
        window.push(date(2024, 7, 4), 200.0)


class _RvolWindowPushSpy:
    """R5-2(lane3, team-lead 재현) — `RvolWindow` 를 감싸 `push` 가 **실제로 호출됐는지**만
    기록한다. `compute_symbol_day_features` 자신의 `require_trading_day(day)` 검사(조립
    진입 시점)와 조립 끝의 `rvol_window.push(day, …)` 내부에서 벌어지는 같은 검사는 **같은
    함수를 호출하므로 예외 메시지가 완전히 동일**하다 — `match=` 로는 "조기 실패"와 "push
    내부까지 가서 뒤늦게 실패"를 구분할 수 없다. push 가 실제로 불렸는지 자체를 관측해야
    한다."""

    def __init__(self, inner: RvolWindow):
        self._inner = inner
        self.push_call_count = 0

    @property
    def minutes(self) -> int:
        return self._inner.minutes

    def rvol(self, day: date, today_sum: float) -> float | None:
        return self._inner.rvol(day, today_sum)

    def push(self, day: date, window_sum: float | None) -> None:
        self.push_call_count += 1
        self._inner.push(day, window_sum)


def test_compute_symbol_day_features_raises_when_day_is_not_a_trading_day():
    """R4-2 — `compute_symbol_day_features` 도 `day` 자체가 거래일인지 독립적으로 검사한다
    (휴장일을 예외 없이 통과시켜 그 날을 RvolWindow 에 유령 push 하던 구멍).

    R5-2(lane3, team-lead 재현) — `compute_symbol_day_features:415` 의 `require_trading_day
    (day)` 를 지워도 165개 전부 통과했다: 조립 끝의 `rvol_window.push(day, …)` 가 첫 줄에서
    같은 검사를 하기 때문이다. docstring 이 "조기 실패"를 설계 의도로 적었는데 그 "조기"가
    테스트로 고정돼 있지 않았다 — 관측 결과(`ValueError`)는 같지만 OR·gap·RVOL 을 다 계산한
    뒤 터지느냐, 진입 즉시 터지느냐의 차이는 남는다. `push` 가 **호출되지 않은 채** 예외가
    나는지(spy) 로 조기 실패를 직접 고정한다.
    """
    spy = _RvolWindowPushSpy(RvolWindow(lookback_days=20, min_valid_days=1, minutes=15))
    with pytest.raises(ValueError, match="거래일이 아니다"):
        compute_symbol_day_features(
            symbol="AAPL", day=date(2024, 7, 4),   # 독립기념일, 휴장
            today_day_candles=(), today_regular_1m=(),
            prev_day=None, prev_day_regular_1m=(),
            opening_range_minutes=15, rvol_window=spy,
        )
    assert spy.push_call_count == 0, (
        "compute_symbol_day_features 자신의 require_trading_day 검사가 없어져(또는 뒤로 밀려)"
        " push 내부까지 도달한 뒤에야 실패했다 — 조기 실패가 아니다(R5-2 회귀).")


def test_rvol_window_constructor_rejects_non_positive_params():
    """R5-3 — 생성자는 3개 인자를 각각 검사하므로(3 raise 지점) `match=` 로 어느 인자가
    걸렸는지 고정한다. 각 호출은 나머지 두 인자가 유효해 셋 중 하나만 걸릴 수 있다."""
    with pytest.raises(ValueError, match="lookback_days"):
        RvolWindow(lookback_days=0, min_valid_days=15, minutes=15)
    with pytest.raises(ValueError, match="min_valid_days"):
        RvolWindow(lookback_days=20, min_valid_days=0, minutes=15)
    with pytest.raises(ValueError, match="minutes 는 1 이상"):
        RvolWindow(lookback_days=20, min_valid_days=15, minutes=0)


def test_rvol_window_trims_history_beyond_lookback():
    days = _trading_days_from(date(2024, 1, 2), 11)
    window = RvolWindow(lookback_days=5, min_valid_days=3, minutes=15)
    for k in range(10):
        window.push(days[k], 100.0 + k)
    # 내부 이력은 최근 5개만 유지돼야(공개 API 로는 rvol 결과가 최근 창만 반영하는지로 확인).
    result = window.rvol(days[10], 1000.0)
    recent_five = [100.0 + k for k in range(5, 10)]
    median = sorted(recent_five)[2]
    assert result == pytest.approx(1000.0 / median)


# --------------------------------------------------------------------------
# RVOL 인과성/창 길이 구조적 강제 (A1/A2, 독립 리뷰 BLOCKER-1/BLOCKER-2)
# --------------------------------------------------------------------------
def test_rvol_window_exposes_its_minutes():
    """A2 — RvolWindow 가 자기 창 길이를 알아야 primary(15분)/sensitivity(5분) 혼동을 잡는다."""
    window15 = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    window5 = RvolWindow(lookback_days=20, min_valid_days=15, minutes=5)
    assert window15.minutes == 15
    assert window5.minutes == 5


def test_rvol_raises_if_day_already_pushed():
    """A1 — 오늘(또는 그 이전 날)을 이미 push 한 뒤 같은/이전 날로 rvol() 을 부르면 인과성 위반.

    이게 없으면 `push(오늘) → rvol(오늘)` 순서로 오늘 거래량이 자기 분모에 들어가는데도
    예외도 사유 기록도 없이 조용히 다른(부풀려진) RVOL 을 낸다(독립 리뷰 실증: 1.5500→1.4588).
    """
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    d0 = date(2024, 1, 2)   # 실제 거래일(R4-2 — 1/1 은 신정 휴장이라 더 이상 쓸 수 없다)
    window.push(d0, 100.0)
    with pytest.raises(ValueError):
        window.rvol(d0, 200.0)                       # 오늘이 이미 push 됨
    with pytest.raises(ValueError):
        window.rvol(d0 - timedelta(days=1), 200.0)   # 과거로도 마찬가지로 금지


def test_rvol_causality_error_message_says_after_not_before():
    """C10(lane4) — 형제 `push()` 오류 메시지는 "이후"(day 는 마지막 push 이후여야 한다)로
    옳게 적혀 있는데, `rvol()` 메시지만 "이전"으로 반대로 적혀 있었다(실제 유효 조건은
    `day > last_day` 이므로 '이후'가 맞다). 문구가 다시 뒤집히면 이 테스트가 죽는다."""
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    d0 = date(2024, 1, 2)   # 실제 거래일(R4-2 — 1/1 은 신정 휴장이라 더 이상 쓸 수 없다)
    window.push(d0, 100.0)
    with pytest.raises(ValueError, match="이후여야 한다"):
        window.rvol(d0, 200.0)


def test_compute_symbol_day_features_raises_when_rvol_window_minutes_mismatched():
    """A2 — primary(15분) 스펙 계산에 sensitivity(5분)용 RvolWindow 를 잘못 넘기면 즉시 실패.

    (독립 리뷰 실증: 뒤바뀐 짝은 RVOL=3.0 대 올바른 짝 RVOL=1.0 으로 임계 1.5 판정을 뒤집었다.)
    """
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_full_day = tuple(_minute_bars("AAPL", d, 9, 30, 390, base=100.0))
    prev_full_day = tuple(_minute_bars("AAPL", prev_d, 9, 30, 390, base=90.0))
    sensitivity_window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=5)
    with pytest.raises(ValueError, match="primary/sensitivity 창이 뒤바뀐"):
        compute_symbol_day_features(
            symbol="AAPL", day=d,
            today_day_candles=today_full_day, today_regular_1m=today_full_day,
            prev_day=prev_d, prev_day_regular_1m=prev_full_day,
            opening_range_minutes=15,             # primary
            rvol_window=sensitivity_window,       # 5분 창 — 불일치
        )


def test_compute_symbol_day_features_pushes_today_automatically_enabling_next_day_lookback():
    """A1 — compute_symbol_day_features 가 오늘을 자동으로 push 하므로, 호출자가 push 순서를
    틀릴 자리가 없다. 다음 날 호출이 자동으로 오늘을 본다."""
    window = RvolWindow(lookback_days=20, min_valid_days=1, minutes=15)
    d1 = date(2024, 3, 4)
    d2 = date(2024, 3, 5)
    bars_d1 = tuple(_minute_bars("AAPL", d1, 9, 30, 390, base=100.0))
    bars_d2 = tuple(_minute_bars("AAPL", d2, 9, 30, 390, base=100.0))

    feats1 = compute_symbol_day_features(
        symbol="AAPL", day=d1, today_day_candles=bars_d1, today_regular_1m=bars_d1,
        prev_day=None, prev_day_regular_1m=(), opening_range_minutes=15, rvol_window=window,
    )
    assert feats1.rvol is None    # d1 시점엔 이력이 비어 있다(유효일 0 < min_valid_days=1)

    feats2 = compute_symbol_day_features(
        symbol="AAPL", day=d2, today_day_candles=bars_d2, today_regular_1m=bars_d2,
        prev_day=None, prev_day_regular_1m=(), opening_range_minutes=15, rvol_window=window,
    )
    # d2 호출 시점엔 d1 이 이미 자동으로 push 돼 있어야(유효일 1개) rvol 이 계산된다.
    assert feats2.rvol is not None

    # 오늘(d2) 을 compute_symbol_day_features 가 이미 push 했으므로, 다시 push 하면 예외
    # (R5-3 — day==last_day 이므로 순서 검사를 겨냥한다).
    with pytest.raises(ValueError, match="시간 순서대로만 호출된다"):
        window.push(d2, 999.0)


# --------------------------------------------------------------------------
# 조립 — compute_symbol_day_features (손계산 fixture: OR/gap/RVOL 동시 대조)
# --------------------------------------------------------------------------
def test_compute_symbol_day_features_hand_calculated_all_present():
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_full_day = tuple(_minute_bars("AAPL", d, 9, 30, 390, base=100.0))   # 09:30..15:59
    prev_full_day = tuple(_minute_bars("AAPL", prev_d, 9, 30, 390, base=90.0))

    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    seed_days = _trading_days_ending(prev_d, 15)   # R4-1 — day 바로 앞 거래일에서 끊기지 않게
    for k, sd in enumerate(seed_days):
        window.push(sd, 5000.0 + k * 100)

    feats = compute_symbol_day_features(
        symbol="AAPL", day=d,
        today_day_candles=today_full_day, today_regular_1m=today_full_day,
        prev_day=prev_d, prev_day_regular_1m=prev_full_day,
        opening_range_minutes=15, rvol_window=window,
    )
    assert isinstance(feats, SymbolDayFeatures)
    assert feats.symbol == "AAPL" and feats.day == d
    window15 = today_full_day[:15]
    assert feats.opening_range == OpeningRange(
        high=max(b.high for b in window15), low=min(b.low for b in window15))
    expected_gap = (today_full_day[0].open - prev_full_day[-1].close) / prev_full_day[-1].close
    assert feats.gap == pytest.approx(expected_gap)
    # F2(lane3 Major) — rvol 도 OR/gap 처럼 손계산 값으로 대조한다("is not None" 만으로는
    # 형제 조항 중 하나만 손계산되는 이 저장소의 전형적 패턴이 된다).
    # cum_today = sum(1000+i for i in range(15)) = 15105. 15일 push 값 = 5000..6400(100 간격,
    # 이미 오름차순) → median(15개, 홀수) = 8번째 값 = 5700. rvol = 15105/5700.
    assert feats.rvol == pytest.approx(15105 / 5700)
    assert feats.excluded_reasons == ()


def test_compute_symbol_day_features_collects_multiple_reasons_sorted():
    d = date(2024, 3, 4)
    incomplete_or_and_no_prev = tuple(_minute_bars("AAPL", d, 9, 30, 14))   # OR 미완
    empty_window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)   # RVOL 도 미달

    feats = compute_symbol_day_features(
        symbol="AAPL", day=d,
        today_day_candles=incomplete_or_and_no_prev,
        today_regular_1m=incomplete_or_and_no_prev,
        prev_day=None, prev_day_regular_1m=(),
        opening_range_minutes=15, rvol_window=empty_window,
    )
    assert feats.opening_range is None
    assert feats.gap is None
    assert feats.rvol is None
    # F9(lane3 Minor, 반드시 고칠 것) — 자기 자신을 정렬한 것과 비교하는 단언은 동어반복에
    # 가깝고 set 순회 순서(PYTHONHASHSEED 의존)에 따라 정렬 누락 변조를 놓칠 수 있었다(12개
    # 시드 중 9개에서만 죽음). 기대 튜플을 문자 그대로 고정한다.
    assert feats.excluded_reasons == (
        "incomplete_opening_range", "insufficient_rvol_lookback", "no_previous_regular_close")
    assert set(feats.excluded_reasons) <= set(DATA_EXCLUSION_REASONS)


def test_compute_symbol_day_features_propagates_gap_reasons_when_prev_day_present_but_empty():
    """F1a(lane3 Major) — `prev_day` 가 있는데 그 날 REGULAR 봉이 0개(no_previous_regular_close)
    일 때도 gap_reasons 가 조립 결과에 반영되는지 확인한다.

    `compute_symbol_day_features` 의 `reasons.update(gap_reasons)` 를 지워도, 기존 다중사유
    테스트는 `prev_day=None` 경로만 써서(그 경로는 `reasons.add(...)` 를 별도로 호출) 잡지
    못했다 — `prev_day` 가 있는 이 경로가 그 구멍을 덮는다.
    """
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_full_day = tuple(_minute_bars("AAPL", d, 9, 30, 390, base=100.0))   # OR 완전
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    seed_days = _trading_days_ending(prev_d, 15)   # R4-1 — day 바로 앞 거래일에서 끊기지 않게
    for k, sd in enumerate(seed_days):
        window.push(sd, 5000.0 + k * 100)

    feats = compute_symbol_day_features(
        symbol="AAPL", day=d,
        today_day_candles=today_full_day, today_regular_1m=today_full_day,
        prev_day=prev_d, prev_day_regular_1m=(),   # prev_day 는 있으나 그 날 REGULAR 봉 0개
        opening_range_minutes=15, rvol_window=window,
    )
    assert feats.opening_range is not None    # OR 입력 자체는 완전 — OR 경로와 무관함을 분리
    assert feats.gap is None
    assert feats.excluded_reasons == ("no_previous_regular_close",)


def test_compute_symbol_day_features_propagates_gap_reasons_when_todays_open_missing():
    """F1b(lane3 Major) — 당일 09:30 봉이 gap 입력에서만 빠진 경우(incomplete_opening_range)도
    조립 결과에 반영되는지 확인한다.

    OR 계산 입력(`today_day_candles`)은 완전하게 따로 둬서 OR 경로가 이미 같은 사유를 내는
    바람에 gap_reasons 전파 여부가 가려지지 않게 한다(두 경로를 의도적으로 분리).
    """
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_full_day_for_or = tuple(_minute_bars("AAPL", d, 9, 30, 390, base=100.0))  # OR 용, 완전
    today_regular_missing_open = tuple(_minute_bars("AAPL", d, 9, 31, 389, base=100.0))  # gap 용
    prev_full_day = tuple(_minute_bars("AAPL", prev_d, 9, 30, 390, base=90.0))
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    seed_days = _trading_days_ending(prev_d, 15)   # R4-1 — day 바로 앞 거래일에서 끊기지 않게
    for k, sd in enumerate(seed_days):
        window.push(sd, 5000.0 + k * 100)

    feats = compute_symbol_day_features(
        symbol="AAPL", day=d,
        today_day_candles=today_full_day_for_or,
        today_regular_1m=today_regular_missing_open,
        prev_day=prev_d, prev_day_regular_1m=prev_full_day,
        opening_range_minutes=15, rvol_window=window,
    )
    assert feats.opening_range is not None    # OR 입력은 완전 — 이 사유가 OR 경로에서 온 게 아님
    assert feats.gap is None
    assert "incomplete_opening_range" in feats.excluded_reasons


def test_compute_symbol_day_features_incomplete_opening_range_reason_appears_exactly_once():
    """M3(lane3·4 수렴, team-lead 재현) — `_add_reason` 의 dedup(`if reason not in reasons`)이
    유일한 방어인데 테스트가 없었다. 당일 09:30 봉이 결측이면 OR 경로(`opening_range_for_spec`)
    와 gap 경로(`gap_for_day`)가 **둘 다** `incomplete_opening_range` 를 낸다 — 도달 불가능한
    방어 코드가 아니다: development 데이터 창에 이런 종목-일이 513개 있다(VST 2023-05-11,
    XLF 2023-05-15, XLY 2023-05-10 …). dedup 이 사라지면 `excluded_reasons` 에 같은 사유가
    두 번 들어가 §11.2 [A] 5% 게이트의 결함률이 과대계상된다."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_missing_open = tuple(_minute_bars("AAPL", d, 9, 31, 389, base=100.0))   # 09:30 결측
    prev_full_day = tuple(_minute_bars("AAPL", prev_d, 9, 30, 390, base=90.0))
    window = RvolWindow(lookback_days=20, min_valid_days=15, minutes=15)
    seed_days = _trading_days_ending(prev_d, 15)   # R4-1 — day 바로 앞 거래일에서 끊기지 않게
    for k, sd in enumerate(seed_days):
        window.push(sd, 5000.0 + k * 100)

    feats = compute_symbol_day_features(
        symbol="AAPL", day=d,
        today_day_candles=today_missing_open, today_regular_1m=today_missing_open,
        prev_day=prev_d, prev_day_regular_1m=prev_full_day,
        opening_range_minutes=15, rvol_window=window,
    )
    assert feats.opening_range is None      # OR 경로도 incomplete_opening_range
    assert feats.gap is None                # gap 경로도 incomplete_opening_range
    assert feats.excluded_reasons.count("incomplete_opening_range") == 1


def test_symbol_day_features_is_frozen():
    feats = SymbolDayFeatures(symbol="AAPL", day=date(2024, 1, 1), opening_range=None,
                              gap=None, rvol=None, excluded_reasons=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        feats.symbol = "MSFT"     # frozen dataclass → 변경 불가


# --------------------------------------------------------------------------
# R4-3(lane 1·2·3 전원 수렴) — `is_eligible` 이 excluded_reasons == () 를 타입에 올린다
# --------------------------------------------------------------------------
def test_is_eligible_true_when_excluded_reasons_empty():
    feats = SymbolDayFeatures(symbol="AAPL", day=date(2024, 3, 4), opening_range=None,
                              gap=None, rvol=None, excluded_reasons=())
    assert feats.is_eligible is True


def test_is_eligible_false_when_any_excluded_reason_present():
    feats = SymbolDayFeatures(symbol="AAPL", day=date(2024, 3, 4), opening_range=None,
                              gap=None, rvol=None,
                              excluded_reasons=("insufficient_rvol_lookback",))
    assert feats.is_eligible is False


def test_is_eligible_is_not_the_same_as_meeting_the_candidate_thresholds():
    """R4-3 이름 주의 — `is_eligible=True` 는 §5.5 데이터 배제가 없다는 뜻일 뿐, §5 의
    gap/RVOL 임계(|gap|>=0.30%, RVOL>=1.5)를 만족한다는 뜻이 아니다. 이 값들 자체는 4-A
    범위 밖(4-B 의 후보 판정)이므로, 여기서는 그 구분이 코드 계약(docstring)으로만
    존재함을 실증한다 — gap 이 임계 미만이어도 excluded_reasons 는 비어 있을 수 있다."""
    feats = SymbolDayFeatures(symbol="AAPL", day=date(2024, 3, 4), opening_range=None,
                              gap=0.0001, rvol=0.2, excluded_reasons=())   # 임계 미달값들
    assert feats.is_eligible is True   # 데이터 사유는 없다 — 그러나 후보 자격과는 별개


def test_regular_window_volume_helper_matches_cum_today_and_lookback():
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 15, base=1.0)
    total, observed = regular_window_volume(tuple(bars), day=d, minutes=15)
    assert observed == 15
    assert total == sum(b.volume for b in bars)
    assert total == cum_today_volume(tuple(bars), day=d, minutes=15)


def test_regular_window_volume_raises_when_bars_are_not_from_day():
    """M1(lane4) — `cum_today_volume`/`window_sum_for_lookback` 은 이 저장소에서 항상
    `today_regular_1m` 으로만 불린다(과거일 원본 봉은 `RvolWindow` 이력에 합산된 형태로만
    보관되고 여기로 다시 들어오지 않는다). 검증 없이 다른 날 봉이 섞이면 거래량이 조용히
    산출되는 대신 예외를 낸다."""
    d = date(2024, 3, 4)
    wrong_day = date(2024, 3, 5)
    bars = _minute_bars("AAPL", wrong_day, 9, 30, 15, base=1.0)
    with pytest.raises(ValueError, match="day_regular_1m 에 day="):
        regular_window_volume(tuple(bars), day=d, minutes=15)


# --------------------------------------------------------------------------
# F3(lane3 Medium) — 같은 분(minute-of-day) 중복 봉은 조용한 순서 의존 대신 fail-closed
# --------------------------------------------------------------------------
def test_regular_window_volume_raises_on_duplicate_minute_of_day():
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 15, base=1.0)
    dup_open = _bar("AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                    o=999, h=999, low=999, c=999, v=999_999.0)   # 09:30 중복(다른 값)
    with pytest.raises(ValueError, match="minute-of-day=570"):
        regular_window_volume(tuple(bars) + (dup_open,), day=d, minutes=15)


def test_gap_for_day_raises_on_duplicate_prev_close_candidate():
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)
    dup_last = _bar("AAPL", datetime(prev_d.year, prev_d.month, prev_d.day, 15, 59, tzinfo=ET),
                    o=1, h=1, low=1, c=1, v=1)   # 마지막 분(15:59) 중복(다른 값) = minute-of-day 959
    with pytest.raises(ValueError, match="minute-of-day=959"):
        gap_for_day(today_regular_1m=tuple(today_bars), day=d, prev_day=prev_d,
                   prev_day_regular_1m=tuple(prev_bars) + (dup_last,))


def test_gap_for_day_raises_on_duplicate_todays_open_candidate():
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    today_bars = _minute_bars("AAPL", d, 9, 30, 1, base=150.0)
    dup_open = _bar("AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                    o=999, h=999, low=999, c=999, v=1)   # minute-of-day 570
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=100.0)
    with pytest.raises(ValueError, match="minute-of-day=570"):
        gap_for_day(today_regular_1m=tuple(today_bars) + (dup_open,), day=d, prev_day=prev_d,
                   prev_day_regular_1m=tuple(prev_bars))


# --------------------------------------------------------------------------
# F7(lane3 Minor) — RVOL 분자 창의 하한(open_minute <= mod) 이 유일한 방어선
# --------------------------------------------------------------------------
def test_regular_window_volume_excludes_bars_before_the_opening_window():
    """regular_window_volume 자체는 세션 필터를 하지 않으므로, 창 하한이 없으면 PRE 봉이
    (상한만 만족하면) 조용히 창 안으로 유입된다."""
    d = date(2024, 3, 4)
    in_window = _minute_bars("AAPL", d, 9, 30, 15, base=1.0)   # 09:30..09:44
    pre_bar = _bar("AAPL", datetime(d.year, d.month, d.day, 9, 0, tzinfo=ET),
                  o=1, h=1, low=1, c=1, v=99_999_999.0)   # 창 앞(09:00, mod=540<585) — 배제돼야 함
    total_without_pre, observed_without_pre = regular_window_volume(
        tuple(in_window), day=d, minutes=15)
    total_with_pre, observed_with_pre = regular_window_volume(
        tuple(in_window) + (pre_bar,), day=d, minutes=15)
    assert total_with_pre == total_without_pre
    assert observed_with_pre == observed_without_pre


# --------------------------------------------------------------------------
# F8(lane3 Minor) — opening_range_for_spec(minutes) 에 기본값이 없어야 한다(sensitivity footgun)
# --------------------------------------------------------------------------
def test_opening_range_for_spec_minutes_has_no_default():
    """`minutes` 에 기본값이 붙으면 sensitivity(5분) 호출에서 인자를 실수로 빠뜨려도 조용히
    primary(15분 등) 로 계산되는 footgun 이 된다."""
    sig = inspect.signature(opening_range_for_spec)
    assert sig.parameters["minutes"].default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# F10(lane3) — 입력 순서 불변성 / sensitivity 4-of-5 배제
# --------------------------------------------------------------------------
def test_opening_range_gap_and_volume_are_invariant_to_input_order():
    """OR/gap/RVOL 분자가 입력 순열에 무관하게 동일 결과를 내는지(리뷰어는 50회 순열에서
    차이 0 을 확인했다 — 이 저장소는 아직 그 성질을 테스트로 고정하지 않고 있었다)."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)
    bars = _minute_bars("AAPL", d, 9, 30, 16)
    prev_bars = _minute_bars("AAPL", prev_d, 9, 30, 390, base=90.0)

    orr0, or_reasons0 = opening_range_for_spec(tuple(bars), minutes=15, day=d)
    gap0, gap_reasons0 = gap_for_day(today_regular_1m=tuple(bars), day=d, prev_day=prev_d,
                                    prev_day_regular_1m=tuple(prev_bars))
    vol0 = cum_today_volume(tuple(bars), day=d, minutes=15)

    rnd = random.Random(20260727)
    for _ in range(50):
        shuffled_bars = list(bars)
        rnd.shuffle(shuffled_bars)
        shuffled_prev = list(prev_bars)
        rnd.shuffle(shuffled_prev)

        orr, or_reasons = opening_range_for_spec(tuple(shuffled_bars), minutes=15, day=d)
        assert (orr, or_reasons) == (orr0, or_reasons0)

        gap, gap_reasons = gap_for_day(today_regular_1m=tuple(shuffled_bars), day=d,
                                       prev_day=prev_d, prev_day_regular_1m=tuple(shuffled_prev))
        assert (gap, gap_reasons) == (gap0, gap_reasons0)

        vol = cum_today_volume(tuple(shuffled_bars), day=d, minutes=15)
        assert vol == vol0


def test_opening_range_sensitivity_4_of_5_is_incomplete():
    """sensitivity(5분) 창도 4/5 만 있으면 incomplete_opening_range(primary 의 14/15 케이스와
    같은 축을 5분 창에서도 확인)."""
    d = date(2024, 3, 4)
    bars = _minute_bars("AAPL", d, 9, 30, 6)   # 09:30..09:35(닫힘 증거 포함, 6개)
    missing_one = bars[:4] + bars[5:6]         # 09:34 결측, 09:35 로 종료 증거는 있음
    orr, reasons = opening_range_for_spec(tuple(missing_one), minutes=5, day=d)
    assert orr is None
    assert reasons == ("incomplete_opening_range",)
