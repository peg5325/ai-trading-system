"""H4 스크리너 선별력 분석 — 사전등록 §7 필수 테스트 (영구 회귀). scipy 무의존.

검증: ①tie-aware 분수배분 ②입력/symbol 순서 불변 ③전수조합 random=풀평균(=equal_all)
      ④EOD=정규장 마감·POST 앵커 미완결·마감봉 ts<=앵커 미완결(계약) ⑤유효일<6 유의불가.
"""
from __future__ import annotations
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "analysis"))
import screener_ranking_h4 as h4   # noqa: E402


def _cand(score, ret, sym="X", sess="REGULAR", close=100.0, date="2026-01-02",
          ts="2026-01-02T14:00:00+00:00"):
    return dict(candidate_score=score, close=close, symbol=sym, et_date=date,
                session=sess, bar_ts_utc=ts,
                forwards={"30": {"complete": True, "return_pct": ret},
                          "60": {"complete": True, "return_pct": ret}})


def test_tie_aware_fractional():
    w = h4.tie_aware_weights([90, 80, 80, 80], 2, top=True)
    assert abs(w[0] - 1.0) < 1e-12
    assert all(abs(x - 1 / 3) < 1e-12 for x in w[1:])
    assert abs(sum(w) - 2.0) < 1e-12
    wb = h4.tie_aware_weights([90, 80, 80, 80], 2, top=False)
    assert abs(sum(wb) - 2.0) < 1e-12
    assert abs(wb[0]) < 1e-12


def test_order_invariance():
    cs = [_cand(90, 1.0), _cand(80, 2.0), _cand(80, 3.0), _cand(80, -1.0)]
    a1 = h4.arm_returns_at_ts(cs, 2, "30", {})
    a2 = h4.arm_returns_at_ts(list(reversed(cs)), 2, "30", {})
    for k in ("top", "bottom", "random"):
        assert abs(a1[k] - a2[k]) < 1e-12


def test_random_equals_pool_mean_and_equal_all():
    cs = [_cand(90, 1.0), _cand(80, 2.0), _cand(70, 3.0), _cand(60, -2.0)]
    a = h4.arm_returns_at_ts(cs, 2, "30", {})
    assert abs(a["random"] - (1.0 + 2.0 + 3.0 - 2.0) / 4) < 1e-12
    assert abs(a["random"] - a["equal"]) < 1e-12


def test_exact_enumeration_equals_pool_mean():
    """전수 조합 C(k,N) subset-mean 의 평균 == 풀 평균 (random_N 축약의 정확성 근거)."""
    rets = [1.0, 2.0, 3.0, -2.0, 0.5]
    for N in (1, 2, 3):
        subs = list(itertools.combinations(rets, N))
        enum_mean = sum(sum(s) / N for s in subs) / len(subs)
        assert abs(enum_mean - sum(rets) / len(rets)) < 1e-12


def test_min_pool_size():
    cs = [_cand(90, 1.0), _cand(80, 2.0)]
    assert h4.arm_returns_at_ts(cs, 2, "30", {}) is None
    assert h4.arm_returns_at_ts(cs, 1, "30", {}) is not None


def test_eod_regular_close_post_incomplete_and_boundary():
    # eod = (정규장 마감 종가, 그 봉 ts_utc)
    eod = {("X", "2026-01-02"): (110.0, "2026-01-02T20:00:00+00:00")}
    reg = _cand(90, 0.0, sess="REGULAR", close=100.0, ts="2026-01-02T14:00:00+00:00")
    assert abs(h4.horizon_ret(reg, "eod", eod) - 10.0) < 1e-9
    pre = _cand(90, 0.0, sess="PRE", close=100.0, ts="2026-01-02T12:00:00+00:00")
    assert abs(h4.horizon_ret(pre, "eod", eod) - 10.0) < 1e-9
    # POST/OVERNIGHT 앵커 → 미완결
    assert h4.horizon_ret(_cand(90, 0.0, sess="POST"), "eod", eod) is None
    assert h4.horizon_ret(_cand(90, 0.0, sess="OVERNIGHT"), "eod", eod) is None
    # ★경계: 마감봉 ts <= 앵커 ts → 미완결 (계약 forward_extended:113)
    at_close = _cand(90, 0.0, sess="REGULAR", ts="2026-01-02T20:00:00+00:00")
    assert h4.horizon_ret(at_close, "eod", eod) is None
    after_close = _cand(90, 0.0, sess="REGULAR", ts="2026-01-02T20:01:00+00:00")
    assert h4.horizon_ret(after_close, "eod", eod) is None


def test_signed_rank_min_effective_days_and_scipy_free():
    p, n = h4.signed_rank([0.1, 0.2, -0.1, 0.0, 0.3])   # 0 제거 후 4개 <6
    assert p is None
    p2, n2 = h4.signed_rank([0.1, -0.2, 0.3, 0.4, -0.1, 0.2])   # 6개
    assert p2 is not None and 0.0 <= p2 <= 1.0


def test_exact_signed_rank_known_values():
    # n=6, 전부 양수·distinct → 양측 exact p = 2 * 1/2^6 = 0.03125 (scipy exact 재현)
    assert abs(h4.wilcoxon_signed_rank_p([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]) - 0.03125) < 1e-9
    # |d| 동점이면 근사 폴백(값이 0~1, exact 와 다를 수 있음) — 경로 존재 확인
    assert h4.wilcoxon_signed_rank_p([0.1] * 6) is not None
