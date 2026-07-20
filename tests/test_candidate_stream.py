"""후보 스트림 추출기 계약 검증 — forward return look-ahead/세션 clip/미완결 처리.

전부 합성 캔들. 네트워크/실데이터 불필요(실데이터 대조는 test_s001_parity.py).

여기서 고정하는 것:
- forward return 이 **결정 봉보다 뒤(>)의 봉만** 쓴다 (strict look-ahead)
- 거래일 경계를 넘으면 미완결 (오버나이트 수익률 혼입 금지)
- 미완결은 0 이 아니라 complete=False → 집계에서 자동 제외
- 스크리너 window 가 i 이하로만 잘려 전달된다 (미래 봉이 지표에 새지 않음)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.candidate_stream import (
    CandidateObservation,
    CandidateStreamExtractor,
    build_decision_bars,
    completed_returns,
    compute_forward_returns,
)
from src.models import Candle

# 2026-07-07 09:30 ET = 13:30Z (정규장 시작)
OPEN_UTC = datetime(2026, 7, 7, 13, 30, tzinfo=timezone.utc)


def _c1(minute: int, close: float, symbol: str = "TEST") -> Candle:
    return Candle.from_moment(symbol, "1m", OPEN_UTC + timedelta(minutes=minute),
                              o=close, h=close + 0.1, low=close - 0.1, c=close,
                              v=1000.0, currency="USD", adjusted=True)


def _bars_5m(n_minutes: int, price=lambda m: 100.0 + m * 0.01) -> list[Candle]:
    ones = [_c1(m, price(m)) for m in range(n_minutes)]
    return build_decision_bars(ones, "TEST")


def test_build_decision_bars_matches_live_5m_rule():
    """1m → 5m 라벨은 버킷 시작시각(label=left). 마지막(진행중) 버킷은 버린다."""
    bars = _bars_5m(20)
    assert len(bars) == 3, "live 와 동일하게 마지막 버킷을 버린다(4개가 아니라 3개)"
    assert bars[0].ts_utc == OPEN_UTC
    assert bars[1].ts_utc == OPEN_UTC + timedelta(minutes=5)
    # 버킷 [0,5) 의 종가 = 1m 봉 4번의 종가
    assert bars[0].close == 100.0 + 4 * 0.01


def test_partial_last_bucket_is_not_emitted():
    """★ 미완결 버킷이 관측으로도 exit_price 로도 새지 않는다.

    live 는 drop_partial=True 라 진행 중 버킷을 결코 쓰지 않는다. 재구성이 그 버킷을
    만들면 (a) 라이브가 만들지 않는 관측이 1건 생기고 (b) 다른 앵커의 exit_price 로
    complete=True 로 쓰인다.
    """
    # 42분 = 8개 완결 버킷 + 마지막 [40,45) 는 1m 봉 2개뿐(미완결)
    ones = [_c1(m, 100.0 + m * 0.01) for m in range(42)]
    bars = build_decision_bars(ones, "TEST")
    assert len(bars) == 8, f"완결 버킷 8개만 나와야 한다(실제 {len(bars)})"
    last_label = OPEN_UTC + timedelta(minutes=35)
    assert bars[-1].ts_utc == last_label
    assert all(b.ts_utc <= last_label for b in bars), "미완결 버킷 라벨이 섞이면 안 된다"


def test_realized_horizon_is_recorded():
    """목표 라벨이 비어 뒤 봉을 쓰면 실현 지평이 요청 지평과 다르게 기록된다."""
    # 30분 지평의 목표 봉만 빠뜨린다: [30,35) 버킷에 1m 봉이 없도록 구성
    minutes = [m for m in range(0, 120) if not (30 <= m < 35)]
    ones = [_c1(m, 100.0 + m * 0.01) for m in minutes]
    bars = build_decision_bars(ones, "TEST")
    fwd = compute_forward_returns(bars, 0, (30,))["30"]
    assert fwd["complete"] is True
    assert fwd["realized_horizon_min"] == 35, "실제로는 35분 뒤 종가로 계산됐음이 기록돼야 한다"
    assert fwd["horizon_min"] == 30


def test_forward_return_uses_only_future_bars():
    """앵커 봉 자신이나 과거 봉으로 수익률을 만들지 않는다."""
    bars = _bars_5m(120)                      # 24개 5m 봉
    fwd = compute_forward_returns(bars, 0, (30,))["30"]
    assert fwd["complete"] is True
    # 앵커 라벨 13:30 → 목표 라벨 14:00 (정확히 30분 뒤)
    assert fwd["exit_ts_utc"] == (OPEN_UTC + timedelta(minutes=30)).isoformat()
    assert fwd["anchor_price"] == bars[0].close
    assert fwd["exit_price"] == bars[6].close     # 6봉 뒤 = 30분
    assert fwd["exit_price"] > fwd["anchor_price"]


def test_forward_return_sign_and_magnitude():
    """가격이 내려가면 음수, 계산식은 (exit-anchor)/anchor*100."""
    ones = [_c1(m, 100.0 - m * 0.1) for m in range(120)]
    bars = build_decision_bars(ones, "TEST")
    fwd = compute_forward_returns(bars, 0, (30,))["30"]
    expected = (fwd["exit_price"] - fwd["anchor_price"]) / fwd["anchor_price"] * 100.0
    assert fwd["return_pct"] < 0
    assert abs(fwd["return_pct"] - expected) < 1e-12


def test_incomplete_at_series_end_is_not_zero():
    """세션 끝이라 지평을 못 채우면 complete=False — 0 대입 금지."""
    bars = _bars_5m(60)                       # 12개 봉 = 60분
    fwd = compute_forward_returns(bars, len(bars) - 1, (30, 60))
    for h in ("30", "60"):
        assert fwd[h]["complete"] is False
        assert fwd[h]["return_pct"] is None, "미완결에 0 을 넣으면 안 된다"
        assert fwd[h]["incomplete_reason"] == "no_bar"


def test_session_boundary_clip_blocks_overnight_return():
    """다음 거래일 봉으로는 수익률을 만들지 않는다(오버나이트 혼입 금지)."""
    day1 = [_c1(m, 100.0) for m in range(0, 60)]
    # 다음날 같은 시각(+24h) 봉들 — 라벨상으로는 '미래'지만 거래일이 다르다
    day2 = [Candle.from_moment("TEST", "1m", OPEN_UTC + timedelta(days=1, minutes=m),
                               o=200.0, h=200.1, low=199.9, c=200.0, v=1000.0,
                               currency="USD", adjusted=True) for m in range(0, 60)]
    bars = build_decision_bars(day1 + day2, "TEST")
    last_day1 = max(i for i, b in enumerate(bars)
                    if b.ts_exchange.date() == bars[0].ts_exchange.date())
    fwd = compute_forward_returns(bars, last_day1, (30,))["30"]
    assert fwd["complete"] is False
    assert fwd["incomplete_reason"] == "session_end"
    assert fwd["return_pct"] is None


def test_gap_too_large_marks_incomplete():
    """데이터 공백으로 목표 시각 근처 봉이 없으면 미완결(먼 봉으로 대체하지 않는다)."""
    # 0~10분 봉만 있고, 그 다음은 60분 뒤로 점프 → 30m 지평의 목표 봉이 없다
    ones = [_c1(m, 100.0) for m in range(0, 10)] + [_c1(m, 105.0) for m in range(60, 70)]
    bars = build_decision_bars(ones, "TEST")
    fwd = compute_forward_returns(bars, 0, (30,))["30"]
    assert fwd["complete"] is False
    assert fwd["incomplete_reason"] == "gap_too_large"


def test_completed_returns_excludes_incomplete_and_missing():
    """집계 헬퍼는 미완결/결측을 자동 제외한다 — 조용한 0 채움 없음."""
    def obs(score, complete, ret):
        return CandidateObservation(
            symbol="T", market="US", bar_ts_utc="x", bar_ts_exchange="x",
            et_date="2026-07-07", session="REGULAR", leverage_factor=1,
            close=100.0, candidate_score=score, candidate_type="LONG_SETUP", rank=1,
            forwards={"30": {"horizon_min": 30, "complete": complete,
                             "return_pct": ret}})
    obs_list = [obs(50.0, True, 1.0), obs(60.0, False, None), obs(70.0, True, -2.0)]
    xs, ys = completed_returns(obs_list, 30, "candidate_score")
    assert xs == [50.0, 70.0], "미완결 관측이 섞이면 안 된다"
    assert ys == [1.0, -2.0]
    assert len(xs) == len(ys)


def test_extractor_window_is_causal(monkeypatch):
    """스크리너에 넘어가는 지표 window 가 결정 봉 이하로만 잘린다."""
    ex = CandidateStreamExtractor()
    ones = [_c1(m, 100.0 + m * 0.01) for m in range(200)]
    bars, ind = ex.prepare(ones, "TEST")
    seen: list[int] = []
    real_screen = ex._screener.screen

    def spy(inputs):
        seen.append(len(inputs[0].indicators))
        return real_screen(inputs)

    monkeypatch.setattr(ex._screener, "screen", spy)
    for i in range(len(ind)):
        ex.screen_at("TEST", ind, i, 1)
    assert seen == [i + 1 for i in range(len(ind))], "window 는 i+1 행이어야 한다"
    # 마지막 행의 close 가 미래 봉의 close 와 섞이지 않았는지
    assert float(ind.iloc[0]["close"]) == bars[0].close


def test_extract_rejects_non_us_market_loudly():
    """market 인자가 계산에 반영되지 않으므로 조용히 틀리느니 크게 실패한다."""
    import pytest
    ex = CandidateStreamExtractor()
    ones = [_c1(m, 100.0) for m in range(60)]
    with pytest.raises(NotImplementedError, match="ET"):
        ex.extract("005930", ones, market="KR")


def test_alignment_returns_none_when_ambiguous():
    """2등도 tolerance 이내면 봉을 단정하지 않는다(모호 = 정렬 실패)."""
    import pandas as pd
    from src.history.candidate_stream import align_live_outcome
    # 완전히 동일한 두 행 → 어느 쪽인지 구분 불가
    row = {"close": 100.0, "ema7": 99.0, "ema20": 98.0, "rsi14": 55.0,
           "vwap": 99.5, "bb_upper": 102.0, "bb_lower": 96.0}
    ind = pd.DataFrame([row, row])
    assert align_live_outcome(ind, row) is None, "동률이면 None 이어야 한다"
    # 한 행만 맞으면 정상 정렬
    other = dict(row, close=200.0)
    ind2 = pd.DataFrame([row, other])
    assert align_live_outcome(ind2, row) == 0


def test_extract_produces_components_and_forwards():
    """추출 결과에 score_components 와 forward 가 모두 실린다(분석 입력 계약)."""
    ex = CandidateStreamExtractor(horizons_min=(30, 60))
    ones = [_c1(m, 100.0 + (m % 7) * 0.05) for m in range(400)]
    obs = ex.extract("TEST", ones, market="US", leverage_factor=1)
    assert obs, "후보가 하나도 없으면 계약 검증 불가"
    o = obs[0]
    assert set(o.components) <= {"trend", "momentum", "volume", "vwap_position", "volatility"}
    assert o.components, "컴포넌트가 비면 H2 분석이 불가능하다"
    for name, c in o.components.items():
        assert abs(c["normalized_score"] * c["weight"] - c["weighted_score"]) < 1e-9
    total = sum(c["weighted_score"] for c in o.components.values())
    assert abs(total - o.weighted_component_sum) < 1e-9
    assert abs((o.weighted_component_sum - o.penalty_score) - o.pre_clamp_score) < 1e-9
    assert set(o.forwards) == {"30", "60"}
