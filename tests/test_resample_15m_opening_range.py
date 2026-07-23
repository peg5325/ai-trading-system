"""1m→15m 파생 + opening range 테스트 (계약 §2.5 / AMENDMENT A1 · 이슈 #75 Phase 1B).

검증(작업 테스트 20~22):
  20. 1m→5m·1m→15m hand-calculated fixture
  21. 세션 경계를 넘는 aggregate 금지
  22. opening range 미래 봉 사용 금지(09:30~09:44 ET 완료봉만)
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.resample import (  # noqa: E402
    opening_range,
    resample_1m,
    resample_1m_to_15m,
    resample_1m_to_5m,
)
from src.models import Candle  # noqa: E402

ET = ZoneInfo("America/New_York")


def _c(sym, hh, mm, o, h, low, c, v=100.0, *, day=7):
    m = datetime(2026, 7, day, hh, mm, tzinfo=ET)
    return Candle.from_moment(symbol=sym, interval="1m", moment=m,
                              o=o, h=h, low=low, c=c, v=v,
                              currency="USD", adjusted=True, market="US")


def test_5m_hand_calculated():
    """20a. 09:30~09:34 → 하나의 5분봉(open=첫, close=마지막, high/low/vol)."""
    bars = [_c("SPY", 9, 30, 10, 12, 9, 11, 100),
            _c("SPY", 9, 31, 11, 15, 10, 14, 200),
            _c("SPY", 9, 32, 14, 14, 8, 9, 150),
            _c("SPY", 9, 33, 9, 13, 9, 12, 120),
            _c("SPY", 9, 34, 12, 16, 11, 15, 130)]
    out = resample_1m_to_5m(bars)
    assert len(out) == 1
    b = out[0]
    assert b.interval == "5m"
    assert (b.open, b.high, b.low, b.close) == (10, 16, 8, 15)
    assert b.volume == 700
    assert b.ts_exchange.hour == 9 and b.ts_exchange.minute == 30


def test_15m_hand_calculated_opening_bucket():
    """20b. 09:30~09:44 15개 1분봉 → 하나의 15분봉(09:30 버킷)."""
    bars = [_c("SPY", 9, 30 + i, 100 + i, 100 + i + 5, 100 + i - 5, 100 + i)
            for i in range(15)]
    out = resample_1m_to_15m(bars)
    assert len(out) == 1
    b = out[0]
    assert b.interval == "15m"
    assert b.ts_exchange.hour == 9 and b.ts_exchange.minute == 30
    assert b.open == 100                     # 첫 분(09:30) open
    assert b.close == 114                    # 마지막 분(09:44) close
    assert b.high == 100 + 14 + 5            # 09:44 high = 119
    assert b.low == 100 - 5                  # 09:30 low = 95
    assert b.volume == 15 * 100


def test_15m_two_buckets_split_at_45():
    """09:44 와 09:45 는 서로 다른 15분 버킷(30, 45)."""
    bars = [_c("SPY", 9, 44, 10, 10, 10, 10), _c("SPY", 9, 45, 20, 20, 20, 20)]
    out = resample_1m_to_15m(bars)
    starts = sorted((b.ts_exchange.hour, b.ts_exchange.minute) for b in out)
    assert starts == [(9, 30), (9, 45)]


def test_15m_input_order_and_dup_invariant():
    """결정론: 입력 순서/중복(값 동일)에 무관하게 동일 결과."""
    bars = [_c("SPY", 9, 30 + i, 100 + i, 100 + i + 1, 100 + i - 1, 100 + i)
            for i in range(15)]
    a = resample_1m_to_15m(bars)
    shuffled = bars + bars[:5]               # 중복(값 동일) 추가
    random.Random(7).shuffle(shuffled)
    b = resample_1m_to_15m(shuffled)
    assert [(x.ts_utc, x.open, x.high, x.low, x.close, x.volume) for x in a] == \
           [(x.ts_utc, x.open, x.high, x.low, x.close, x.volume) for x in b]


def test_resample_1m_rejects_unsupported_bucket():
    with pytest.raises(ValueError):
        resample_1m([_c("SPY", 9, 30, 1, 1, 1, 1)], 7)


def test_no_cross_session_bucket():
    """21. PRE 마지막(09:29)과 REGULAR 첫(09:30)은 다른 세션 → 같은 버킷에 묶이지 않는다.

    5분 그리드상 09:25~09:29 는 PRE, 09:30~09:34 는 REGULAR. 09:29·09:30 이 같은 grid 버킷이
    아니지만, 세션이 버킷 키에 포함돼 있어 경계 혼입이 구조적으로 불가함을 확인.
    """
    pre = _c("SPY", 9, 29, 5, 5, 5, 5)       # PRE (04:00~09:30)
    reg = _c("SPY", 9, 30, 10, 10, 10, 10)   # REGULAR
    out = resample_1m_to_5m([pre, reg])
    by_session = {b.session: b for b in out}
    assert set(by_session) == {"PRE", "REGULAR"}
    assert by_session["PRE"].open == 5 and by_session["PRE"].volume == 100
    assert by_session["REGULAR"].open == 10
    # 어떤 버킷도 두 세션을 섞지 않는다.
    assert all(b.session in ("PRE", "REGULAR") for b in out)


def test_pre_and_regular_at_shared_grid_slot_not_merged():
    """09:30 시작 버킷에 PRE 봉(가상)과 REGULAR 봉이 함께 와도 세션별로 분리된다."""
    import dataclasses
    reg = _c("SPY", 9, 32, 10, 10, 10, 10)                 # REGULAR, 09:30 버킷
    fake_pre = dataclasses.replace(_c("SPY", 9, 33, 7, 7, 7, 7), session="PRE")
    out = resample_1m_to_5m([reg, fake_pre])
    sessions = sorted(b.session for b in out)
    assert sessions == ["PRE", "REGULAR"]                  # 병합되지 않음(세션 키 분리)


# ---- 22. opening range: 미래 봉 사용 금지 ----

def test_opening_range_15m_uses_only_0930_0944():
    """22. 15분 opening range 는 09:30~09:44 완료봉만. 09:45+ 봉은 절대 반영 안 됨."""
    bars = [_c("SPY", 9, 30 + i, 100, 105, 95, 100) for i in range(15)]   # 09:30~09:44
    future_spike = _c("SPY", 9, 45, 999, 9999, 1, 999)                    # 미래 봉(창 밖)
    out = opening_range(bars + [future_spike], minutes=15)
    assert len(out) == 1
    orb = out[0]
    assert orb.interval == "15m_or"
    assert orb.high == 105 and orb.low == 95                 # 9999/1 미반영
    assert orb.close == 100                                  # 09:44 close (09:45 아님)
    assert orb.ts_exchange.hour == 9 and orb.ts_exchange.minute == 30


def test_opening_range_incomplete_window_skipped():
    """require_complete=True: 14개(<15)면 opening range 를 만들지 않는다(미완으로 둠)."""
    bars = [_c("SPY", 9, 30 + i, 100, 101, 99, 100) for i in range(14)]
    assert opening_range(bars, minutes=15) == []
    partial = opening_range(bars, minutes=15, require_complete=False)
    assert len(partial) == 1                                 # 존재하는 분만으로 집계


def test_opening_range_5m_sensitivity():
    """5분 opening range(sensitivity 후보) = 09:30~09:34."""
    bars = [_c("SPY", 9, 30 + i, 100, 102, 98, 100 + i) for i in range(5)]
    bars.append(_c("SPY", 9, 35, 500, 500, 500, 500))       # 창 밖
    out = opening_range(bars, minutes=5)
    assert len(out) == 1 and out[0].interval == "5m_or"
    assert out[0].high == 102 and out[0].close == 104        # 09:34 close


def test_opening_range_excludes_premarket():
    """PRE(09:29 이전) 봉은 opening range 에 들어가지 않는다(REGULAR 창만)."""
    pre = _c("SPY", 9, 29, 1, 1, 1, 1)                       # PRE
    reg = [_c("SPY", 9, 30 + i, 100, 101, 99, 100) for i in range(15)]
    out = opening_range([pre] + reg, minutes=15)
    assert len(out) == 1
    assert out[0].low == 99                                  # PRE low=1 미반영
