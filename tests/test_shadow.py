"""Online Shadow Decision(PR 4-A) — 순수 decider 검증.

- Policy A(close>EMA20) / Policy B(A ∧ slope>0) 로직.
- null context/warm-up → would_allow=None(관측이 매매를 막지 않음).
- 런타임 레코드에 **미래 outcome 필드 없음**(forward_return/avoided_loss 등) — 4-B 가 오프라인 부착.
- 결정론: 같은 입력 → 같은 레코드.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.shadow.decider import build_shadow_record, evaluate_shadow_policies
from src.shadow.models import (
    POLICY_A,
    POLICY_B,
    R_CLOSE_ABOVE_EMA,
    R_CLOSE_BELOW_EMA,
    R_EMA_WARMUP,
    R_NO_CONTEXT,
    R_SLOPE_DOWN_FLAT,
    R_SLOPE_UP,
    R_SLOPE_WARMUP,
    SHADOW_POLICY_VERSION,
    SHADOW_VERSION,
    ShadowConfig,
)

_DT = datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc)

# 미래 outcome 필드(런타임 레코드에 절대 없어야 함 — 4-B 오프라인 전용)
_FUTURE_KEYS = {"forward_return_30m", "forward_return_60m", "avoided_loss",
                "missed_profit", "missed_opportunity", "outcome_complete", "horizon_end"}


def _ctx(close, ema, slope):
    return {"benchmark_symbol": "QQQ", "benchmark_close": close,
            "benchmark_ema20": ema, "benchmark_ema20_slope": slope}


def _by_policy(context):
    return {d.policy_id: d for d in evaluate_shadow_policies(context)}


def test_policy_a_allows_when_close_above_ema():
    a = _by_policy(_ctx(101.0, 100.0, 0.001))[POLICY_A]
    assert a.would_allow is True and a.reason_codes == [R_CLOSE_ABOVE_EMA]
    assert a.policy_version == SHADOW_POLICY_VERSION


def test_policy_a_blocks_when_close_below_ema():
    a = _by_policy(_ctx(99.0, 100.0, 0.001))[POLICY_A]
    assert a.would_allow is False and a.reason_codes == [R_CLOSE_BELOW_EMA]


def test_policy_b_requires_close_above_and_slope_up():
    # close>ema & slope>0 → allow
    b_up = _by_policy(_ctx(101.0, 100.0, 0.002))[POLICY_B]
    assert b_up.would_allow is True
    assert b_up.reason_codes == [R_CLOSE_ABOVE_EMA, R_SLOPE_UP]
    # close>ema & slope<=0 → block(slope 실패)
    b_flat = _by_policy(_ctx(101.0, 100.0, 0.0))[POLICY_B]
    assert b_flat.would_allow is False
    assert R_SLOPE_DOWN_FLAT in b_flat.reason_codes
    # close<ema → block
    b_below = _by_policy(_ctx(99.0, 100.0, 0.002))[POLICY_B]
    assert b_below.would_allow is False and R_CLOSE_BELOW_EMA in b_below.reason_codes


def test_null_context_yields_none_not_block():
    for d in evaluate_shadow_policies(None):
        assert d.would_allow is None and d.reason_codes == [R_NO_CONTEXT]


def test_warmup_missing_ema_yields_none():
    a = _by_policy(_ctx(101.0, None, 0.001))[POLICY_A]
    assert a.would_allow is None and a.reason_codes == [R_EMA_WARMUP]
    # ema 결측 → B 도 EMA_WARMUP
    b_ema = _by_policy(_ctx(101.0, None, 0.001))[POLICY_B]
    assert b_ema.would_allow is None and b_ema.reason_codes == [R_EMA_WARMUP]
    # close/ema 있고 slope 만 결측 → B 는 SLOPE_WARMUP(원인 구분)
    b_slope = _by_policy(_ctx(101.0, 100.0, None))[POLICY_B]
    assert b_slope.would_allow is None and b_slope.reason_codes == [R_SLOPE_WARMUP]


def _all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            keys |= _all_keys(v)
    return keys


def test_build_record_structure_and_no_future_outcome():
    ctx = _ctx(101.0, 100.0, 0.002)
    rec = build_shadow_record(ctx, decision_time=_DT, candidate_ids=["cid1", "cid2"])
    assert rec["shadow_version"] == SHADOW_VERSION
    assert rec["policy_version"] == SHADOW_POLICY_VERSION
    assert rec["decision_time"] == "2026-07-07T14:00:00Z"
    assert rec["market_context_snapshot"] == ctx
    assert rec["candidate_ids"] == ["cid1", "cid2"]
    pids = {p["policy_id"] for p in rec["policies"]}
    assert pids == {POLICY_A, POLICY_B}
    # ★ 런타임 레코드에 미래 outcome 필드가 전혀 없어야 한다
    assert _all_keys(rec).isdisjoint(_FUTURE_KEYS)


def test_build_record_null_context():
    rec = build_shadow_record(None, decision_time=_DT, candidate_ids=[])
    assert rec["market_context_snapshot"] is None
    assert all(p["would_allow"] is None for p in rec["policies"])
    assert rec["candidate_ids"] == []


def test_build_record_deterministic():
    ctx = _ctx(101.0, 100.0, 0.002)
    r1 = build_shadow_record(ctx, decision_time=_DT, candidate_ids=["c"])
    r2 = build_shadow_record(ctx, decision_time=_DT, candidate_ids=["c"])
    assert r1 == r2


def test_shadow_config_default_off():
    assert ShadowConfig().enabled is False
