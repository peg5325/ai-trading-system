"""1분봉 → 5분봉 결정론적 파생 테스트 (계약 §2.5, 이슈 #68 Phase 1A).

TDD: 정렬된 5분 버킷·집계·불완전 버킷·세션 분리·입력순서 불변·중복제거·look-ahead 없음.
합성 fixture 만 사용(네트워크·실 provider 없음).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.models import Candle
from src.history.resample import resample_1m_to_5m

ET = ZoneInfo("America/New_York")


def _c(hh: int, mm: int, o: float, h: float, low: float, c: float, v: float,
       *, day: str = "2026-03-16", sym: str = "SPY") -> Candle:
    """ET 로컬 시각의 1분봉 한 개."""
    y, mo, d = (int(x) for x in day.split("-"))
    moment = datetime(y, mo, d, hh, mm, tzinfo=ET)
    return Candle.from_moment(sym, "1m", moment, o, h, low, c, v,
                              currency="USD", adjusted=True, market="US")


def test_single_full_bucket_aggregates_ohlcv():
    bars = [
        _c(9, 30, 100, 101, 99, 100.5, 10),
        _c(9, 31, 100.5, 102, 100, 101, 20),
        _c(9, 32, 101, 101.5, 100.5, 100.8, 5),
        _c(9, 33, 100.8, 103, 100.7, 102.5, 8),
        _c(9, 34, 102.5, 102.9, 101, 101.2, 12),
    ]
    out = resample_1m_to_5m(bars)
    assert len(out) == 1
    b = out[0]
    assert b.interval == "5m"
    assert b.open == 100            # 첫 1분봉 open
    assert b.close == 101.2         # 마지막 1분봉 close
    assert b.high == 103            # max high
    assert b.low == 99              # min low
    assert b.volume == 55           # 합
    assert b.session == "REGULAR"
    assert b.ts_exchange.hour == 9 and b.ts_exchange.minute == 30
    assert b.market == "US" and b.currency == "USD" and b.adjusted is True


def test_two_buckets_split_on_5min_grid():
    bars = [_c(9, 30 + i, 100 + i, 100 + i, 100 + i, 100 + i, 1) for i in range(10)]
    out = resample_1m_to_5m(bars)
    assert [b.ts_exchange.minute for b in out] == [30, 35]
    assert out[0].open == 100 and out[0].close == 104
    assert out[1].open == 105 and out[1].close == 109


def test_bucket_labelled_bucket_start_even_if_first_minute_missing():
    # 09:30 결측, 09:31~09:34 존재 → 5분봉 라벨은 버킷 시작(09:30), open=09:31 open
    bars = [_c(9, 31, 50, 51, 49, 50.5, 3), _c(9, 33, 50.5, 52, 50, 51, 4)]
    out = resample_1m_to_5m(bars)
    assert len(out) == 1
    assert out[0].ts_exchange.minute == 30
    assert out[0].open == 50        # 존재하는 첫 분봉 open
    assert out[0].close == 51
    assert out[0].volume == 7


def test_min_bars_filters_incomplete_buckets():
    bars = [_c(9, 30, 1, 1, 1, 1, 1), _c(9, 32, 1, 1, 1, 1, 1), _c(9, 34, 1, 1, 1, 1, 1)]
    assert len(resample_1m_to_5m(bars, min_bars=1)) == 1     # 기본: 존재 분만으로 생성
    assert resample_1m_to_5m(bars, min_bars=5) == []         # 완전 버킷만 요구 → 제외


def test_input_order_invariance():
    bars = [_c(9, 30 + i, 100 + i, 100 + i, 100 + i, 100 + i, i + 1) for i in range(10)]
    ref = resample_1m_to_5m(list(bars))
    shuffled = list(bars)
    random.Random(1234).shuffle(shuffled)
    got = resample_1m_to_5m(shuffled)
    assert [(b.ts_utc, b.open, b.high, b.low, b.close, b.volume) for b in ref] == \
           [(b.ts_utc, b.open, b.high, b.low, b.close, b.volume) for b in got]


def test_sessions_not_mixed_across_boundary():
    # PRE 마지막 버킷(09:25~09:29)과 REGULAR 첫 버킷(09:30~)이 섞이지 않는다.
    bars = [_c(9, 25, 1, 1, 1, 1, 1), _c(9, 29, 1, 2, 1, 2, 1),
            _c(9, 30, 3, 3, 3, 3, 1), _c(9, 34, 3, 4, 3, 4, 1)]
    out = resample_1m_to_5m(bars)
    assert len(out) == 2
    pre, reg = out[0], out[1]
    assert pre.session == "PRE" and pre.ts_exchange.minute == 25
    assert reg.session == "REGULAR" and reg.ts_exchange.minute == 30
    assert pre.close == 2 and reg.open == 3     # 경계 넘어 close/open 섞이지 않음


def test_output_sorted_ascending_and_across_days():
    d1 = [_c(9, 30 + i, 1, 1, 1, 1, 1, day="2026-03-16") for i in range(5)]
    d2 = [_c(9, 30 + i, 2, 2, 2, 2, 1, day="2026-03-17") for i in range(5)]
    out = resample_1m_to_5m(d2 + d1)  # 역순 입력
    assert len(out) == 2
    assert out[0].ts_utc < out[1].ts_utc
    assert out[0].ts_exchange.date().isoformat() == "2026-03-16"


def test_duplicate_ts_collapsed_deterministically():
    # 동일 ts_utc 두 개(값 불일치) → 결정론적으로 하나로 축약(입력 순서 무관).
    a = _c(9, 30, 1, 1, 1, 1, 1)
    b = _c(9, 30, 9, 9, 9, 9, 9)   # 같은 시각, 다른 값
    out_ab = resample_1m_to_5m([a, b])
    out_ba = resample_1m_to_5m([b, a])
    assert len(out_ab) == 1 and len(out_ba) == 1
    assert out_ab[0].volume == out_ba[0].volume  # 순서 무관 동일 결과
    # (o,h,l,c,v) 최대 튜플 채택 규칙을 명시적으로 고정(계약 §2.5).
    assert out_ab[0].volume == 9 and out_ab[0].open == 9


def test_empty_input():
    assert resample_1m_to_5m([]) == []


def test_multi_symbol_grouped_separately():
    bars = ([_c(9, 30 + i, 1, 1, 1, 1, 1, sym="SPY") for i in range(5)] +
            [_c(9, 30 + i, 2, 2, 2, 2, 1, sym="QQQ") for i in range(5)])
    out = resample_1m_to_5m(bars)
    syms = sorted(b.symbol for b in out)
    assert syms == ["QQQ", "SPY"]
    assert len(out) == 2


def test_post_to_overnight_boundary_at_2000():
    # 19:55~19:59 = POST 마지막 버킷, 20:00~ = OVERNIGHT — 20:00 경계에서 섞이지 않는다.
    bars = [_c(19, 55, 1, 1, 1, 1, 1), _c(19, 59, 1, 2, 1, 2, 1),
            _c(20, 0, 3, 3, 3, 3, 1), _c(20, 4, 3, 4, 3, 4, 1)]
    out = resample_1m_to_5m(bars)
    assert len(out) == 2
    assert out[0].session == "POST" and out[0].ts_exchange.hour == 19 and out[0].ts_exchange.minute == 55
    assert out[1].session == "OVERNIGHT" and out[1].ts_exchange.hour == 20 and out[1].ts_exchange.minute == 0
    assert out[0].close == 2 and out[1].open == 3


def test_kr_market_resample_session_and_currency():
    KST = ZoneInfo("Asia/Seoul")
    def _kr(hh, mm, o, h, low, c, v):
        moment = datetime(2026, 3, 16, hh, mm, tzinfo=KST)
        return Candle.from_moment("005930", "1m", moment, o, h, low, c, v,
                                  currency="KRW", adjusted=True, market="KR")
    bars = [_kr(9, 0, 100, 101, 99, 100.5, 10), _kr(9, 4, 100.5, 102, 100, 101, 5)]
    out = resample_1m_to_5m(bars)
    assert len(out) == 1
    assert out[0].market == "KR" and out[0].currency == "KRW"
    assert out[0].session == "REGULAR"     # KR 09:00 = 정규장 시작
    assert out[0].ts_exchange.hour == 9 and out[0].ts_exchange.minute == 0
    assert out[0].high == 102 and out[0].low == 99


def test_float_volume_order_invariant():
    # float volume 합도 집계 전 정렬 덕에 순서 불변(부동소수 결합순서 고정).
    bars = [_c(9, 30 + i, 1, 1, 1, 1, 0.1) for i in range(5)]
    ref = resample_1m_to_5m(list(bars))
    shuffled = list(bars)
    random.Random(99).shuffle(shuffled)
    got = resample_1m_to_5m(shuffled)
    assert ref[0].volume == got[0].volume
