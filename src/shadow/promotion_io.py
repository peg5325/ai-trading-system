"""Phase 6 리포트 assembly — journal/candle/manifest → SessionSignal (PR #38).

순수 판정(promotion.py)과 파일 I/O CLI 사이의 read-only 조립 계층. 매매 로직 무관
(behavior-change=false). 4-B `build_shadow_outcomes`/`aggregate_by_policy`(entry_only=True)와
2-B market_context_snapshot(benchmark_trend)만 소비한다. journal/candle 은 read-only.

핵심 원칙:
- 승격 신호는 **entry_only=True(ORDERED)** 집계 — 관측 유니버스 전체가 아니라 실제 진입 후보만.
- manifest 미존재/미종료 → completion_status="UNKNOWN"(=primary 아님, 보수적 제외).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.shadow.outcome import aggregate_by_policy, build_shadow_outcomes
from src.shadow.promotion import SessionSignal, classify_session_regime

COMPLETION_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SessionInputs:
    """세션의 판정 메타(정책·horizon 무관 부분). blocked 신호는 aggregates 로 별도 전달."""
    session_id: str
    market: str | None
    completion_status: str
    manual_flatten_count: int
    regime: str | None
    started_at: str | None
    config_hash: str | None = None


def session_regime(journal) -> str | None:
    """세션 국면 = 각 사이클 shadow 스냅샷의 benchmark_trend 최빈값(promotion.classify)."""
    trends = []
    for cycle in journal.get_by_time_range():
        shadow = getattr(cycle, "shadow", None)
        if not shadow:
            continue
        snap = shadow.get("market_context_snapshot") or {}
        trends.append(snap.get("benchmark_trend"))
    return classify_session_regime(trends)


def analyze_session(journal, candle_store, *, manifest=None, session_id: str,
                    horizons: tuple[int, ...], built_at: str | None = None):
    """한 세션 → (SessionInputs, {horizon: {policy_id: agg}}). read-only.

    aggregates 는 `aggregate_by_policy(..., entry_only=True)` — 실제 진입(ORDERED) + gate 차단
    후보만 센다(승격 신호 분모). market 은 manifest.identity.market 우선, 없으면 outcome 레코드의
    market 최빈값으로 대체."""
    ds = build_shadow_outcomes(journal, candle_store, horizons_min=tuple(horizons),
                               built_at=built_at, session_id=session_id)
    aggregates = {h: aggregate_by_policy(ds, h, entry_only=True) for h in horizons}

    market = None
    completion = COMPLETION_UNKNOWN
    manual = 0
    started_at = None
    config_hash = None
    if manifest is not None:
        identity = getattr(manifest, "identity", None)
        market = getattr(identity, "market", None)
        config_hash = getattr(identity, "config_hash", None)
        completion = manifest.completion_status or COMPLETION_UNKNOWN
        manual = manifest.manual_flatten_count
        started_at = manifest.created_at
    if not market:   # manifest 없거나 market 미상 → 레코드 market 최빈값
        rec_markets = Counter(r.market for r in ds.records if r.market)
        market = rec_markets.most_common(1)[0][0] if rec_markets else None

    inputs = SessionInputs(
        session_id=session_id, market=market, completion_status=completion,
        manual_flatten_count=manual, regime=session_regime(journal),
        started_at=started_at, config_hash=config_hash)
    return inputs, aggregates


def to_signal(inputs: SessionInputs, agg: dict | None, horizon_min: int) -> SessionSignal:
    """SessionInputs + (해당 policy·horizon) 집계 → SessionSignal. 집계 없으면 0(중립 기여)."""
    agg = agg or {}
    return SessionSignal(
        session_id=inputs.session_id, market=inputs.market or "UNKNOWN",
        completion_status=inputs.completion_status,
        manual_flatten_count=inputs.manual_flatten_count,
        regime=inputs.regime, started_at=inputs.started_at,
        blocked_avoided_loss=float(agg.get("blocked_avoided_loss", 0.0)),
        blocked_missed_profit=float(agg.get("blocked_missed_profit", 0.0)),
        n_blocked=int(agg.get("n_blocked", 0)))
