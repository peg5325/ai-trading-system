"""연구↔라이브↔replay 리샘플러 동치 계약 (이슈 #76 — ORB 공식 연구 전 blocking gate).

계약: `docs/research/RESAMPLER_PARITY_CONTRACT.md`, DATA_CONTRACT §2.5 / AMENDMENT A2.

검증 축:
  (a) 부분/진행 중 버킷 — **시간축(closed_only)** 과 **커버리지축(min_bars)** 의 분리
  (b) dedup — 입력 순서 무관(값 충돌 시 (o,h,l,c,v) 최대 튜플)
  (c) 세션/market — 버킷 키에 session 포함, US 하드코딩 없음(KR 정확)
  (d) 경로 동치 — research core == live adapter == candidate_stream == replay
  (e) fail-closed — 빈 입력·잘못된 interval·지원 밖 rule·무시되는 인자

★ 성과·forward return·blind OOS 는 계산하지 않는다(전부 synthetic fixture).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from src.engines.indicator_engine import (candles_to_frame, frame_to_candles,
                                          resample)
from src.history.resample import (bucket_minutes_from_rule, opening_range,
                                  resample_1m, resample_1m_to_5m,
                                  resample_1m_to_15m)
from src.models import ET, KST, Candle, classify_session

UTC = timezone.utc


# --------------------------------------------------------------------------
# fixtures (synthetic — 실데이터·성과 미사용)
# --------------------------------------------------------------------------
def mk_1m(symbol: str, start, count: int, *, currency: str = "USD",
          base: float = 100.0, step: int = 1) -> list[Candle]:
    """start(거래소 로컬 또는 UTC aware) 부터 count 개 1분봉. 값은 index 로 결정론적."""
    out = []
    for i in range(count):
        moment = start + timedelta(minutes=i * step)
        o = base + i
        out.append(Candle.from_moment(symbol, "1m", moment,
                                      o, o + 2, o - 1, o + 1, 10.0 * (i + 1),
                                      currency, True))
    return out


def ser(candles: list[Candle]) -> list[tuple]:
    """비교용 결정론 직렬화 — 계약이 보장하는 전 필드."""
    return [(c.symbol, c.interval, c.ts_utc.isoformat(), c.ts_exchange.isoformat(),
             c.session, c.open, c.high, c.low, c.close, c.volume,
             c.currency, c.adjusted, c.market) for c in candles]


def live_bars(candles_1m: list[Candle], symbol: str, rule: str = "5min") -> list[Candle]:
    """라이브(paper_loop) 경로: frame → resample → frame_to_candles."""
    from src.live.paper_loop import frame_to_candles as live_frame_to_candles
    df = resample(candles_to_frame(candles_1m), rule)
    return live_frame_to_candles(df, symbol)


# --------------------------------------------------------------------------
# (a) 버킷 완성/진행 — 시간축
# --------------------------------------------------------------------------
def test_complete_5m_bucket_0930_0934():
    """09:30~09:34 ET 5개 1분봉 = 완전한 5m 버킷 하나(다음 봉이 도착하면 닫힌다)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)   # 09:30..09:35
    bars = resample_1m_to_5m(c, closed_only=True)
    assert len(bars) == 1
    b = bars[0]
    assert b.ts_exchange.strftime("%H:%M") == "09:30"
    assert b.interval == "5m" and b.session == "REGULAR"
    assert (b.open, b.high, b.low, b.close) == (100.0, 106.0, 99.0, 105.0)
    assert b.volume == 10 + 20 + 30 + 40 + 50


def test_complete_15m_bucket_0930_0944():
    """09:30~09:44 ET 15개 1분봉 = 완전한 15m 버킷 하나."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 16)  # 09:30..09:45
    bars = resample_1m_to_15m(c, closed_only=True)
    assert len(bars) == 1
    b = bars[0]
    assert b.ts_exchange.strftime("%H:%M") == "09:30"
    assert b.interval == "15m"
    assert b.close == mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 16)[14].close


def test_in_progress_last_bucket_is_withheld():
    """진행 중 버킷: 마지막 봉이 09:37 이면 [09:35,09:40) 은 닫혔다는 증거가 없다."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 8)   # 09:30..09:37
    closed = resample_1m_to_5m(c, closed_only=True)
    allbars = resample_1m_to_5m(c)                                 # 시간축 미적용
    assert [b.ts_exchange.strftime("%H:%M") for b in closed] == ["09:30"]
    assert [b.ts_exchange.strftime("%H:%M") for b in allbars] == ["09:30", "09:35"]


def test_exact_close_boundary_needs_evidence_past_end():
    """정확히 완료된 경계: [09:30,09:35) 는 09:35 봉(또는 evaluation_time>=09:35)이 있어야 닫힌다.

    마지막 1분봉은 형성 중일 수 있으므로 frontier 에 +1분 하지 않는다(보수적 계약).
    """
    upto_0934 = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 5)   # 09:30..09:34
    assert resample_1m_to_5m(upto_0934, closed_only=True) == []

    # 다음 버킷의 첫 봉이 도착 → 닫힘
    with_0935 = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    assert len(resample_1m_to_5m(with_0935, closed_only=True)) == 1

    # evaluation_time 을 명시하면 데이터 증거 없이도 경계 판정(정확히 종료 시각 = 닫힘)
    end = datetime(2026, 3, 3, 9, 35, tzinfo=ET)
    assert len(resample_1m_to_5m(upto_0934, closed_only=True, evaluation_time=end)) == 1
    just_before = end - timedelta(seconds=1)
    assert resample_1m_to_5m(upto_0934, closed_only=True,
                             evaluation_time=just_before) == []


def test_min_bars_is_coverage_axis_not_time_axis():
    """min_bars(커버리지)와 closed_only(시간)는 독립 — 닫혔어도 결측이면 min_bars 로 거른다."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    holed = [x for x in c if x.ts_exchange.minute != 32]           # 09:32 결측
    assert len(resample_1m_to_5m(holed, closed_only=True)) == 1     # 닫힘(방출)
    assert resample_1m_to_5m(holed, closed_only=True, min_bars=5) == []  # 커버리지 미달


def test_missing_first_minute_labels_bucket_start():
    """첫 분 결측이어도 버킷 라벨은 그리드 시작(09:30), open 은 존재하는 첫 봉."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 31, tzinfo=ET), 5)    # 09:31..09:35
    bars = resample_1m_to_5m(c, closed_only=True)
    assert len(bars) == 1
    assert bars[0].ts_exchange.strftime("%H:%M") == "09:30"
    assert bars[0].open == 100.0                                    # 09:31 봉의 open


def test_missing_middle_minute_does_not_shift_bucket():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    holed = [x for x in c if x.ts_exchange.minute != 33]
    bars = resample_1m_to_5m(holed, closed_only=True)
    assert len(bars) == 1
    assert bars[0].close == 105.0            # 09:34 봉의 close(결측분 건너뜀)
    assert bars[0].volume == 10 + 20 + 30 + 50      # 09:33 거래량은 빠진다


# --------------------------------------------------------------------------
# (b) dedup / 순서 불변
# --------------------------------------------------------------------------
def test_duplicate_ts_same_value_is_idempotent():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    dup = c + [c[2], c[4]]
    assert ser(resample_1m_to_5m(dup, closed_only=True)) == \
           ser(resample_1m_to_5m(c, closed_only=True))


def test_duplicate_ts_conflicting_value_uses_max_tuple_regardless_of_order():
    base = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    lo = base[1]
    hi = Candle(**{**lo.__dict__, "high": lo.high + 50})   # 같은 ts, 더 큰 튜플
    a = resample_1m_to_5m(base[:1] + [lo, hi] + base[2:], closed_only=True)
    b = resample_1m_to_5m(base[:1] + [hi, lo] + base[2:], closed_only=True)
    assert ser(a) == ser(b)
    assert a[0].high == hi.high            # 순서와 무관하게 최대 튜플 채택


def test_input_order_invariance_reverse_and_shuffle():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 21)
    expect = ser(resample_1m_to_5m(c, closed_only=True))
    assert ser(resample_1m_to_5m(list(reversed(c)), closed_only=True)) == expect
    shuffled = c[7:] + c[:3] + c[3:7]
    assert ser(resample_1m_to_5m(shuffled, closed_only=True)) == expect
    # 어댑터(프레임) 경로도 동일
    assert ser(live_bars(list(reversed(c)), "SPY")) == ser(live_bars(c, "SPY"))


def test_adapter_dedup_matches_core_on_conflicting_duplicates():
    """**어댑터**(프레임) 경로의 dedup 도 순서 무관 최대 튜플이다.

    `candles_to_frame` 이 core 보다 **먼저** 중복을 지우므로, 실제로 tie-break 를 결정하는
    것은 어댑터 쪽 규칙이다. core 만 검증하면 라이브가 조용히 다른 봉을 쓸 수 있다
    (mutation review: `keep="last"`/`keep="first"` 로 바꿔도 기존 테스트는 전부 통과했다).
    """
    base = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    lo = base[1]
    hi = Candle(**{**lo.__dict__, "high": lo.high + 50})
    fwd = base[:1] + [lo, hi] + base[2:]
    rev = base[:1] + [hi, lo] + base[2:]
    expect = ser(resample_1m_to_5m(fwd, closed_only=True))
    assert ser(live_bars(fwd, "SPY")) == expect
    assert ser(live_bars(rev, "SPY")) == expect
    assert live_bars(fwd, "SPY")[0].high == hi.high


def test_nan_duplicate_is_order_invariant_and_prefers_real_value():
    """NaN 중복도 **순서 무관**하며 core 와 어댑터가 같은 행을 남긴다(#76 독립 리뷰 LOW).

    NaN 은 모든 비교에서 False 라 tie-break 가 입력 순서에 좌우됐다. 정상 OHLCV 에는
    NaN 이 없지만, 계약이 "순서 무관"이라고 적혀 있으면 이 코너에서도 참이어야 한다.
    """
    nan = float("nan")
    base = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    good = base[1]
    bad = Candle(**{**good.__dict__, "open": nan})
    for order in ([bad, good], [good, bad]):
        c = base[:1] + order + base[2:]
        core = resample_1m_to_5m(c, closed_only=True)
        live = live_bars(c, "SPY")
        assert core[0].open == 100.0            # 결측 아닌 행 선호
        assert ser(live) == ser(core)           # 어댑터도 같은 총순서


def test_adapter_inherits_adjusted_false():
    """`adjusted` 는 상속한다 — 하드코딩 True 로 되돌리면 실패한다."""
    c = [Candle(**{**x.__dict__, "adjusted": False})
         for x in mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)]
    live = live_bars(c, "SPY")
    assert [b.adjusted for b in live] == [False]
    assert ser(live) == ser(resample_1m_to_5m(c, closed_only=True))


def test_adapter_inherits_session_column_over_reclassifying():
    """프레임의 session 을 **상속**한다 — 재분류하면 원본과 달라진다.

    (원천이 09:30 을 PRE 로 라벨한 비정상 데이터를 어댑터가 조용히 REGULAR 로 고쳐 쓰면
    core 와 갈라진다.)
    """
    c = [Candle(**{**x.__dict__, "session": "PRE"})
         for x in mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)]
    live = live_bars(c, "SPY")
    assert [b.session for b in live] == ["PRE"]           # classify 였다면 REGULAR
    assert ser(live) == ser(resample_1m_to_5m(c, closed_only=True))


def test_adapter_rejects_same_ts_multiple_buckets_instead_of_dropping():
    """프레임이 표현할 수 없는 입력은 **조용히 버리지 않고** fail-closed(독립 리뷰 HIGH)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    mixed = c[:1] + [Candle(**{**c[1].__dict__, "session": "PRE"})] + c[2:]
    core = resample_1m(mixed, 5, closed_only=True)
    assert len(core) == 2 and len({b.ts_utc for b in core}) == 1   # core 는 둘 다 낸다
    with pytest.raises(ValueError, match="복수 버킷"):
        resample(candles_to_frame(mixed), "5min")


def test_adapter_rejects_timezone_naive_frames():
    """tz-naive 프레임은 **머신 TZ 로 추측하지 않고** 거부한다(독립 리뷰 MEDIUM)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    df = candles_to_frame(c).drop(columns=["ts_exchange", "session", "market"])
    naive = df.tz_localize(None)
    with pytest.raises(ValueError, match="naive"):
        resample(naive, "5min", drop_partial=False)
    with pytest.raises(ValueError, match="naive"):
        frame_to_candles(naive, "SPY")


def test_adapter_ts_exchange_is_plain_datetime_like_core():
    """어댑터가 pd.Timestamp 를 흘리지 않는다(경로별 타입 동일)."""
    import pandas as _pd
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    live = live_bars(c, "SPY")
    assert not isinstance(live[0].ts_exchange, _pd.Timestamp)
    assert not isinstance(live[0].ts_utc, _pd.Timestamp)
    assert type(live[0].ts_exchange) is type(
        resample_1m_to_5m(c, closed_only=True)[0].ts_exchange)


def test_adapter_forwards_evaluation_time_to_core():
    """어댑터의 `evaluation_time` 이 core 로 전달된다(무시되면 봉 수가 다르다)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 5)   # 09:30..09:34
    assert len(resample(candles_to_frame(c), "5min")) == 0
    at = datetime(2026, 3, 3, 9, 35, tzinfo=ET)
    df = resample(candles_to_frame(c), "5min", evaluation_time=at)
    assert len(df) == 1
    assert ser(frame_to_candles(df, "SPY")) == \
        ser(resample_1m_to_5m(c, closed_only=True, evaluation_time=at))


def test_adapter_default_interval_label_is_canonical():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    assert [b.interval for b in live_bars(c, "SPY")] == ["5m"]
    assert [b.interval for b in live_bars(c, "SPY", rule="15min")] == []   # 미완
    long = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 16)
    assert [b.interval for b in live_bars(long, "SPY", rule="15min")] == ["15m"]


def test_frontier_uses_deduped_max_ts_only():
    """중복 입력이 frontier 를 앞당기지 못한다(중복은 새 시간 증거가 아니다)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 5)    # 09:30..09:34
    assert resample_1m_to_5m(c + c, closed_only=True) == []


# --------------------------------------------------------------------------
# (c) 세션 경계 / market-aware
# --------------------------------------------------------------------------
def test_pre_to_regular_boundary_not_merged():
    pre = mk_1m("SPY", datetime(2026, 3, 3, 9, 25, tzinfo=ET), 5)   # PRE 09:25..09:29
    reg = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)   # REGULAR 09:30..
    bars = resample_1m_to_5m(pre + reg, closed_only=True)
    assert [(b.ts_exchange.strftime("%H:%M"), b.session) for b in bars] == \
           [("09:25", "PRE"), ("09:30", "REGULAR")]


def test_regular_to_post_boundary_not_merged():
    reg = mk_1m("SPY", datetime(2026, 3, 3, 15, 55, tzinfo=ET), 5)  # ..15:59 REGULAR
    post = mk_1m("SPY", datetime(2026, 3, 3, 16, 0, tzinfo=ET), 5)  # 16:00.. POST
    bars = resample_1m_to_5m(reg + post, closed_only=True)
    assert [(b.ts_exchange.strftime("%H:%M"), b.session) for b in bars] == \
           [("15:55", "REGULAR")]                                   # POST 버킷은 미완
    assert all(b.session == "REGULAR" for b in bars)


def test_session_in_bucket_key_is_structural_not_grid_luck():
    """같은 그리드 버킷 안에서 session 라벨이 다르면 **분리**된다(구조적 보장).

    실데이터의 US/KR 세션 경계는 5·15분 그리드와 정렬돼 있어 자연 발생하지 않지만,
    계약은 그리드 정렬에 의존하지 않는다.
    """
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 3)
    odd = Candle(**{**c[1].__dict__, "session": "PRE"})             # 같은 버킷, 다른 세션
    bars = resample_1m(c[:1] + [odd] + c[2:], 5,
                       closed_only=True,
                       evaluation_time=datetime(2026, 3, 3, 9, 35, tzinfo=ET))
    assert sorted(b.session for b in bars) == ["PRE", "REGULAR"]


def test_kr_market_session_and_timezone_not_et_hardcoded():
    """KR(KRW) 입력은 KST 세션/거래소시각을 유지한다 — 과거 어댑터는 ET 로 재계산했다."""
    c = mk_1m("005930", datetime(2026, 3, 3, 9, 0, tzinfo=KST), 6, currency="KRW")
    core = resample_1m_to_5m(c, closed_only=True)
    live = live_bars(c, "005930")
    assert core[0].session == "REGULAR" and core[0].market == "KR"
    assert core[0].ts_exchange.strftime("%H:%M") == "09:00"
    assert core[0].ts_exchange.tzinfo is not None
    assert [b.session for b in live] == [b.session for b in core]
    assert [b.market for b in live] == ["KR"]
    assert [b.ts_exchange.utcoffset() for b in live] == [timedelta(hours=9)]
    # ET 로 분류했다면 전날 19:00 → POST 로 잘못 라벨됐을 것이다(회귀 방지).
    assert classify_session(core[0].ts_utc.astimezone(ET), "US") == "POST"


def test_kr_pre_and_regular_sessions_split():
    pre = mk_1m("005930", datetime(2026, 3, 3, 8, 55, tzinfo=KST), 5, currency="KRW")
    reg = mk_1m("005930", datetime(2026, 3, 3, 9, 0, tzinfo=KST), 6, currency="KRW")
    bars = resample_1m_to_5m(pre + reg, closed_only=True)
    assert [(b.ts_exchange.strftime("%H:%M"), b.session) for b in bars] == \
           [("08:55", "PRE"), ("09:00", "REGULAR")]


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------
def test_us_dst_spring_forward_labels_stay_local():
    """DST 전환 전후 모두 09:30 ET 라벨(UTC 오프셋은 -05:00 → -04:00 로 바뀐다)."""
    before = mk_1m("SPY", datetime(2026, 3, 6, 9, 30, tzinfo=ET), 6)   # EST
    after = mk_1m("SPY", datetime(2026, 3, 9, 9, 30, tzinfo=ET), 6)    # EDT
    b0 = resample_1m_to_5m(before, closed_only=True)[0]
    a0 = resample_1m_to_5m(after, closed_only=True)[0]
    assert b0.ts_exchange.strftime("%H:%M") == a0.ts_exchange.strftime("%H:%M") == "09:30"
    assert b0.ts_utc.hour == 14 and a0.ts_utc.hour == 13          # EST 14:30Z / EDT 13:30Z
    assert b0.session == a0.session == "REGULAR"


def test_us_dst_fall_back_repeated_hour_does_not_merge():
    """가을 fold: 로컬 01:00 이 두 번 나와도 UTC 가 달라 버킷이 합쳐지지 않는다."""
    first = mk_1m("SPY", datetime(2026, 11, 1, 5, 0, tzinfo=UTC), 5)    # 01:00 EDT
    second = mk_1m("SPY", datetime(2026, 11, 1, 6, 0, tzinfo=UTC), 6)   # 01:00 EST
    bars = resample_1m_to_5m(first + second, closed_only=True)
    labels = [b.ts_exchange.strftime("%H:%M") for b in bars]
    assert labels == ["01:00", "01:00"]                 # 같은 로컬 라벨
    assert len({b.ts_utc for b in bars}) == 2           # 다른 UTC → 별개 버킷
    assert [b.ts_utc.hour for b in bars] == [5, 6]


def test_bucket_end_uses_utc_arithmetic_across_transition():
    """전환 시각을 포함하는 버킷도 길이가 Δ분(UTC 산술) — 로컬 산술이면 1시간 왜곡된다."""
    # 01:55 EDT(05:55Z) 5봉 → 버킷 [01:55,02:00) EDT 종료 = 06:00Z
    c = mk_1m("SPY", datetime(2026, 11, 1, 5, 55, tzinfo=UTC), 5)
    assert resample_1m_to_5m(c, closed_only=True) == []              # 06:00Z 증거 없음
    at_end = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert len(resample_1m_to_5m(c, closed_only=True, evaluation_time=at_end)) == 1


# --------------------------------------------------------------------------
# 복수 symbol
# --------------------------------------------------------------------------
def test_multi_symbol_input_grouped_and_sorted():
    a = mk_1m("AAA", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 11)
    b = mk_1m("BBB", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 11, base=200.0)
    bars = resample_1m_to_5m(a + b, closed_only=True)
    assert [(x.symbol, x.ts_exchange.strftime("%H:%M")) for x in bars] == [
        ("AAA", "09:30"), ("BBB", "09:30"), ("AAA", "09:35"), ("BBB", "09:35")]
    assert bars == sorted(bars, key=lambda x: (x.ts_utc, x.symbol, x.session))
    # 종목별 분리 실행과 동일(공통 frontier, 동일 길이 시리즈)
    assert ser([x for x in bars if x.symbol == "AAA"]) == \
           ser(resample_1m_to_5m(a, closed_only=True))


def test_multi_symbol_frontier_is_per_symbol_and_compositional():
    """frontier 는 **symbol 별** — 신선한 종목이 stale 종목의 버킷을 닫아주지 않는다(#76 리뷰 B1).

    라이브(`paper_loop`)는 종목별로 리샘플한다. frontier 가 입력 전체의 최대 ts 라면
    `resample(A∪B) != resample(A) ∪ resample(B)` 가 되어 연구가 하루치 30종목을 한 번에
    로드하는 순간 다시 연구≠라이브가 된다.
    """
    fresh = mk_1m("AAA", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 11)   # ..09:40
    stale = mk_1m("BBB", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 5)    # ..09:34 에서 멈춤
    batched = resample_1m_to_5m(fresh + stale, closed_only=True)
    apart = resample_1m_to_5m(fresh, closed_only=True) + \
        resample_1m_to_5m(stale, closed_only=True)
    assert ser(batched) == ser(sorted(apart, key=lambda b: (b.ts_utc, b.symbol, b.session)))
    # stale 종목은 단독일 때와 똑같이 방출되지 않는다(라이브 per-symbol 동작 보존)
    assert {x.symbol for x in batched} == {"AAA"}
    assert [x.ts_exchange.strftime("%H:%M") for x in batched] == ["09:30", "09:35"]
    assert resample_1m_to_5m(stale, closed_only=True) == []


def test_market_wide_clock_is_enabled_by_explicit_evaluation_time():
    """시장 전체 시계는 **명시적으로만** 쓴다 — 그때는 stale 종목 버킷도 닫힌다."""
    fresh = mk_1m("AAA", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 11)
    stale = mk_1m("BBB", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 5)
    at = datetime(2026, 3, 3, 9, 40, tzinfo=ET)
    bars = resample_1m_to_5m(fresh + stale, closed_only=True, evaluation_time=at)
    assert ("BBB", "09:30") in [(x.symbol, x.ts_exchange.strftime("%H:%M")) for x in bars]


def test_batched_multi_symbol_matches_per_symbol_live_path():
    """어댑터(라이브) 경로와 batched core 결과가 종목별로 일치한다."""
    a = mk_1m("AAA", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 23)
    b = mk_1m("BBB", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 9, base=200.0)
    batched = resample_1m_to_5m(a + b, closed_only=True)
    for sym, src in (("AAA", a), ("BBB", b)):
        assert ser([x for x in batched if x.symbol == sym]) == ser(live_bars(src, sym))


# --------------------------------------------------------------------------
# (d) 경로 동치 — research core == live == candidate_stream == replay
# --------------------------------------------------------------------------
def _session_1m(symbol="SPY", day=(2026, 3, 3), minutes=45):
    return mk_1m(symbol, datetime(*day, 9, 30, tzinfo=ET), minutes)


def test_research_core_equals_live_adapter():
    c = _session_1m()
    assert ser(live_bars(c, "SPY")) == ser(resample_1m_to_5m(c, closed_only=True))


def test_research_core_equals_live_adapter_15m():
    c = _session_1m(minutes=95)
    assert ser(live_bars(c, "SPY", rule="15min")) == \
           ser(resample_1m_to_15m(c, closed_only=True))


def test_candidate_stream_equals_live_path():
    from src.history.candidate_stream import build_decision_bars
    c = _session_1m()
    assert ser(build_decision_bars(c, "SPY")) == ser(live_bars(c, "SPY"))


def test_candidate_stream_equals_live_path_kr():
    """KR 에서도 동일 — 이전에는 candidate_stream 이 ET 로 변환해 달랐다."""
    from src.history.candidate_stream import build_decision_bars
    c = mk_1m("005930", datetime(2026, 3, 3, 9, 0, tzinfo=KST), 45, currency="KRW")
    assert ser(build_decision_bars(c, "005930")) == ser(live_bars(c, "005930"))


def test_replay_equals_live_path():
    from src.replay.system import ReplaySystem, candle_to_dict

    class _Store:
        def __init__(self, candles):
            self._c = candles

        def query(self, symbol, interval):
            return [x for x in self._c if x.symbol == symbol and x.interval == interval]

    c = _session_1m()
    replayed = ReplaySystem(journal=None, candle_store=_Store(c))._candles("SPY", None)
    assert replayed == [candle_to_dict(x) for x in live_bars(c, "SPY")]


def test_full_regular_session_regression():
    """기존 full-session fixture 회귀: 390분 정규장 → 닫힌 5m 77개(+POST 증거 시 78)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 390)      # 09:30..15:59
    bars = resample_1m_to_5m(c, closed_only=True)
    assert len(bars) == 77                        # [15:55,16:00) 은 증거 없음
    assert all(b.session == "REGULAR" for b in bars)
    post = mk_1m("SPY", datetime(2026, 3, 3, 16, 0, tzinfo=ET), 1)
    with_post = resample_1m_to_5m(c + post, closed_only=True)
    assert len([b for b in with_post if b.session == "REGULAR"]) == 78
    assert ser(live_bars(c, "SPY")) == ser(bars)


def test_frame_roundtrip_preserves_contract_fields():
    c = _session_1m()
    df = resample(candles_to_frame(c), "5min")
    assert list(df.index.names) == ["ts_utc"]
    assert {"session", "ts_exchange", "market", "adjusted", "interval",
            "currency", "et_date"} <= set(df.columns)
    assert ser(frame_to_candles(df, "SPY")) == ser(resample_1m_to_5m(c, closed_only=True))


# --------------------------------------------------------------------------
# (e) fail-closed
# --------------------------------------------------------------------------
def test_live_import_path_does_not_pull_history_package():
    """라이브 결정 경로가 오프라인 데이터 인프라를 끌어오지 않는다(#76 리뷰 R1).

    core 가 `src/history` 안에 있으면 `indicator_engine` import 가
    `src/history/__init__.py`(loader·store·quality·sqlite3)를 실행하고, `candidate_stream`
    이 engines 를 되짚어 import 하므로 패키지 초기화 순환에 노출된다. core 를 leaf
    `src/bars` 로 옮긴 상태를 **구조적으로** 고정한다.
    """
    import subprocess
    import sys as _sys

    code = ("import sys, importlib;"
            "importlib.import_module('src.engines.indicator_engine');"
            "print('\\n'.join(m for m in sys.modules if m.startswith('src.')))")
    out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=os.path.dirname(os.path.dirname(
                             os.path.abspath(__file__))))
    assert out.returncode == 0, out.stderr
    loaded = set(out.stdout.split())
    assert "src.bars.resample" in loaded          # kernel 은 온다
    assert not [m for m in loaded if m.startswith("src.history")], loaded
    assert not [m for m in loaded
                if m.startswith(("src.order", "src.risk", "src.live",
                                 "src.pipeline", "src.ai"))], loaded


def test_shim_reexports_are_the_same_objects():
    """`src.history.resample` shim 은 core 와 동일 객체를 노출한다(중복 구현 금지)."""
    from src.bars import resample as core
    from src.history import resample as shim

    for name in ("resample_1m", "resample_1m_to_5m", "resample_1m_to_15m",
                 "opening_range", "bucket_minutes_from_rule", "SUPPORTED_BUCKETS"):
        assert getattr(shim, name) is getattr(core, name)


def test_empty_input_is_empty_not_error():
    assert resample_1m_to_5m([]) == []
    assert resample_1m_to_5m([], closed_only=True) == []
    assert resample(candles_to_frame([]), "5min").empty
    assert frame_to_candles(candles_to_frame([]), "SPY") == []


def test_non_1m_input_fails_closed():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    derived = resample_1m_to_5m(c, closed_only=True)
    with pytest.raises(ValueError, match="1분봉"):
        resample_1m_to_5m(derived)
    with pytest.raises(ValueError, match="1분봉|재리샘플"):
        resample(candles_to_frame(derived), "5min")


def test_unsupported_rule_fails_closed():
    for bad in ("1h", "3min", "5s", "", "5MIN_"):
        with pytest.raises(ValueError):
            bucket_minutes_from_rule(bad)
    with pytest.raises(ValueError):
        bucket_minutes_from_rule(7)
    with pytest.raises(ValueError):
        bucket_minutes_from_rule(True)
    assert bucket_minutes_from_rule("15m") == bucket_minutes_from_rule("15min") == 15
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    with pytest.raises(ValueError):
        resample(candles_to_frame(c), "1h")


def test_unsupported_bucket_and_bad_args_fail_closed():
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 6)
    with pytest.raises(ValueError):
        resample_1m(c, 7)
    with pytest.raises(ValueError, match="min_bars"):
        resample_1m_to_5m(c, min_bars=0)
    with pytest.raises(ValueError, match="evaluation_time"):
        resample_1m_to_5m(c, evaluation_time=datetime(2026, 3, 3, 9, 35, tzinfo=ET))
    with pytest.raises(ValueError, match="timezone-aware"):
        resample_1m_to_5m(c, closed_only=True,
                          evaluation_time=datetime(2026, 3, 3, 9, 35))


def test_opening_range_shares_the_same_axes():
    """opening_range 도 커버리지축(require_complete)·시간축(closed_only)이 같은 계약이다."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 15)       # 09:30..09:44
    assert len(opening_range(c, minutes=15)) == 1                       # 기존 동작 보존
    assert opening_range(c, minutes=15, closed_only=True) == []         # 창 종료 증거 없음
    with_next = c + mk_1m("SPY", datetime(2026, 3, 3, 9, 45, tzinfo=ET), 1)
    assert len(opening_range(with_next, minutes=15, closed_only=True)) == 1
    at_end = datetime(2026, 3, 3, 9, 45, tzinfo=ET)
    assert len(opening_range(c, minutes=15, closed_only=True,
                             evaluation_time=at_end)) == 1
    with pytest.raises(ValueError, match="evaluation_time"):
        opening_range(c, minutes=15, evaluation_time=at_end)


def test_opening_range_labels_window_start_even_when_first_minutes_missing():
    """라벨은 **개장 시각**이다 — 첫 분 결측 시 존재하는 첫 봉으로 밀리지 않는다(#76).

    docstring 은 '창 시작' 이라고 적었지만 `require_complete=False` 에서는 첫 멤버 시각이
    라벨로 나갔다. ORB 사전등록이 이 라벨을 신호 시각으로 고정하기 전에 문서와 코드를 맞춘다.
    """
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 32, tzinfo=ET), 13)     # 09:32..09:44
    bars = opening_range(c, minutes=15, require_complete=False)
    assert len(bars) == 1
    assert bars[0].ts_exchange.strftime("%H:%M") == "09:30"
    assert bars[0].ts_utc == datetime(2026, 3, 3, 9, 30, tzinfo=ET).astimezone(UTC)
    assert bars[0].open == 100.0                    # open 은 존재하는 첫 봉(09:32)
    # 완전한 창에서는 기존과 동일한 라벨
    full = opening_range(mk_1m("SPY", datetime(2026, 3, 3, 9, 30, tzinfo=ET), 15),
                         minutes=15)
    assert full[0].ts_exchange.strftime("%H:%M") == "09:30"


def test_opening_range_closed_boundary_with_missing_first_minutes():
    """창 종료 판정도 개장 기준이다(첫 분 결측이 경계를 밀지 않는다)."""
    c = mk_1m("SPY", datetime(2026, 3, 3, 9, 32, tzinfo=ET), 13)     # 09:32..09:44
    just_before = datetime(2026, 3, 3, 9, 44, 59, tzinfo=ET)
    at_end = datetime(2026, 3, 3, 9, 45, tzinfo=ET)
    assert opening_range(c, minutes=15, require_complete=False,
                         closed_only=True, evaluation_time=just_before) == []
    assert len(opening_range(c, minutes=15, require_complete=False,
                             closed_only=True, evaluation_time=at_end)) == 1
