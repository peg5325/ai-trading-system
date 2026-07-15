"""build_shadow_outcomes — 세션 후 read-only batch 로 shadow 결정에 forward return 부착(PR 4-B).

Runtime/Paper 와 완전히 분리된 read-only 배치. 매매 로직에 일절 관여하지 않고, 런타임 shadow
레코드(4-A)에는 아무것도 쓰지 않는다(미래 outcome 격리 유지). 각 사이클의 shadow 결정을
후보(candidate_id)로 fan-out 해 candidate symbol 의 결정봉 종가 대비 price-direction forward 를 잰다.

Look-ahead 방지(exit_builder 와 동일 계약 재사용):
- forward 는 decision_time '초과'(strict, bisect) 봉만, 같은 거래소 날짜(세션)로 clip.
- horizon 이 세션 종료를 넘으면 forward=None + complete=False(INCOMPLETE) → 집계 자동 제외.
결정론적: 입력이 같으면 출력이 같다(현재시각/난수 미사용; built_at 은 외부 주입).
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone

from src.models import exchange_tz
from src.shadow.outcome_models import (
    ShadowForwardPoint,
    ShadowOutcomeDataset,
    ShadowOutcomeMeta,
    ShadowOutcomeRecord,
    iso,
)

DEFAULT_HORIZONS_MIN = (30, 60)


def _parse_iso_z(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ShadowOutcomeBuilder:
    def __init__(self, candle_store=None, *, interval: str = "1m",
                 horizons_min: tuple[int, ...] = DEFAULT_HORIZONS_MIN,
                 built_at: str | None = None):
        self._store = candle_store
        self._interval = interval
        self._horizons = tuple(sorted(set(horizons_min)))
        self._built_at = built_at
        self._cache: dict[str, tuple[list[datetime], list]] = {}

    def build(self, journal, *, session_id: str | None = None,
              source: str | None = None) -> ShadowOutcomeDataset:
        records: list[ShadowOutcomeRecord] = []
        n_bad_time = 0
        for cycle in journal.get_by_time_range():
            shadow = getattr(cycle, "shadow", None)
            if not shadow:
                continue
            decision_time = _parse_iso_z(shadow.get("decision_time"))
            if decision_time is None:
                n_bad_time += 1            # shadow 있으나 시각 파싱 실패 → 데이터 품질 카운트
                continue
            policies = shadow.get("policies", []) or []
            # 4-A 가 기록한 authoritative candidate_ids 를 조인 기준으로 사용(outcomes 재스캔 아님).
            # symbol/action 은 outcomes 에서 candidate_id 로 해소한다(둘의 정합은 4-A 가 보장).
            by_cid = {o.get("candidate_id"): o for o in cycle.outcomes if o.get("candidate_id")}
            for cid in (shadow.get("candidate_ids") or []):
                o = by_cid.get(cid)
                if o is None or not o.get("symbol"):
                    continue                # 레코드된 후보에 대응 outcome 없으면 방어적 skip
                records.append(self._record(cid, o["symbol"], o.get("action"),
                                            decision_time, policies))
        meta = ShadowOutcomeMeta(
            session_id=session_id, interval=self._interval, horizons_min=self._horizons,
            source=source, built_at=self._built_at, n_bad_decision_time=n_bad_time)
        return ShadowOutcomeDataset(records=records, meta=meta)

    def _record(self, cid, symbol, action, decision_time, policies) -> ShadowOutcomeRecord:
        ts_list, candles = self._candles(symbol)
        # 결정봉 = decision_time 이하 마지막 봉(결정 시점에 알려진 종가 → look-ahead 없음).
        di = bisect_right(ts_list, decision_time) - 1
        decision_bar = candles[di] if 0 <= di < len(candles) else None
        market = getattr(decision_bar, "market", None) if decision_bar else None

        # 결정봉이 결정일 이전 거래일이면(장초 데이터 갭) stale → 참조 없음 취급.
        # 그러면 아래 세션 clip 으로 forward 는 전부 INCOMPLETE(집계 제외)가 된다.
        if decision_bar is not None:
            dec_exch_date = decision_time.astimezone(exchange_tz(market)).date()
            if decision_bar.ts_exchange.date() < dec_exch_date:
                decision_bar = None
        ref_price = _f(decision_bar.close) if decision_bar else None
        decision_date = decision_bar.ts_exchange.date() if decision_bar else None

        day_bars = [c for c in candles if decision_date is not None
                    and c.ts_exchange.date() == decision_date]
        sess_end_ts = day_bars[-1].ts_utc if day_bars else None
        post = [c for c in day_bars if c.ts_utc > decision_time]     # strict: 결정 이후만

        rec = ShadowOutcomeRecord(
            candidate_id=cid, symbol=symbol, market=market,
            decision_time=iso(decision_time), reference_price=ref_price,
            action=action, policies=[dict(p) for p in policies])   # 정책 dict 깊은 복사(원본 격리)
        for h in self._horizons:
            rec.forwards.append(self._forward_point(decision_time, ref_price, post,
                                                    sess_end_ts, h))
        return rec

    def _forward_point(self, decision_time, ref_price, post, sess_end_ts,
                       horizon_min) -> ShadowForwardPoint:
        target = decision_time + timedelta(minutes=horizon_min)
        # 세션 종료를 넘는 horizon → INCOMPLETE
        if (ref_price is None or not ref_price or sess_end_ts is None or target > sess_end_ts):
            return ShadowForwardPoint(horizon_min, iso(target), None, None, None, False)
        win = [c for c in post if c.ts_utc <= target]      # (decision, target] 같은 날
        if not win:
            return ShadowForwardPoint(horizon_min, iso(target), None, None, None, False)
        # 'complete'=target 이하 마지막 close 존재(≤ target). 데이터 갭이 크면 target 보다
        # 오래된 봉일 수 있다(exit_builder 와 동일한 의도된 한계 — 봉이 target 에 '정확히'
        # 있음을 뜻하지 않는다). 희소 구간 정확도가 필요하면 staleness 가드는 후속 과제.
        price = win[-1].close                              # target 이하 마지막 close
        fwd = (price - ref_price) / ref_price
        return ShadowForwardPoint(
            horizon_min=horizon_min, target_time=iso(target), forward_return=fwd,
            avoided_loss=max(-fwd, 0.0), missed_profit=max(fwd, 0.0), complete=True)

    def _candles(self, symbol: str):
        if symbol in self._cache:
            return self._cache[symbol]
        candles: list = []
        if self._store is not None:
            candles = self._store.query(symbol, self._interval)   # read-only
        # bisect/윈도 슬라이싱은 ts_utc 오름차순을 전제 — 주입 store 무관하게 방어적 정렬.
        candles = sorted(candles, key=lambda c: c.ts_utc)
        ts_list = [c.ts_utc for c in candles]
        self._cache[symbol] = (ts_list, candles)
        return ts_list, candles


def build_shadow_outcomes(journal, candle_store, *,
                          horizons_min: tuple[int, ...] = DEFAULT_HORIZONS_MIN,
                          interval: str = "1m", built_at: str | None = None,
                          session_id: str | None = None,
                          source: str | None = None) -> ShadowOutcomeDataset:
    """Journal(shadow 레코드) + Candle → ShadowOutcomeDataset. read-only 배치 진입점."""
    builder = ShadowOutcomeBuilder(candle_store, interval=interval,
                                   horizons_min=horizons_min, built_at=built_at)
    return builder.build(journal, session_id=session_id, source=source)


def aggregate_by_policy(dataset: ShadowOutcomeDataset, horizon_min: int, *,
                        entry_only: bool = False) -> dict:
    """정책별 집계(해당 horizon). INCOMPLETE 는 제외. gate 가 차단(would_allow=False)한 후보의
    avoided_loss/missed_profit 합이 Phase 6 승격 신호('avoided loss > missed profit').

    ★ 분모 주의: 기본은 사이클의 **전 관측 후보(candidate_id 부여된 모든 outcome)**를 센다.
    pipeline 은 진입 여부와 무관하게 관측 심볼마다 candidate_id 를 부여하므로(NO_CANDIDATE/
    AI_WAIT/REJECT 포함), 이 기본 집계는 '관측 유니버스 전체' 기준이다. **Phase 6 승격 판정처럼
    '국면 gate 가 실제 진입을 막았을 때의 가치'를 재려면 `entry_only=True`** 로 실제 진입(ORDERED)
    후보만 필터해야 한다(그 외는 애초에 진입 안 했을 후보라 신호를 희석/편향한다)."""
    out: dict[str, dict] = {}
    for rec in dataset.records:
        if entry_only and rec.action != "ORDERED":   # 실제 진입 후보만(승격 신호용)
            continue
        fp = rec.forward_map().get(horizon_min)
        if fp is None or not fp.complete:     # 미완결 horizon 자동 제외
            continue
        for pol in rec.policies:
            pid = pol.get("policy_id")
            if pid is None:
                continue
            agg = out.setdefault(pid, dict(
                n_complete=0, n_blocked=0, n_allowed=0,
                blocked_avoided_loss=0.0, blocked_missed_profit=0.0,
                allowed_forward_return=0.0))
            agg["n_complete"] += 1
            wa = pol.get("would_allow")
            if wa is False:                    # gate 차단 → 차단 가치(피한 손실 vs 놓친 이익)
                agg["n_blocked"] += 1
                agg["blocked_avoided_loss"] += fp.avoided_loss
                agg["blocked_missed_profit"] += fp.missed_profit
            elif wa is True:                   # gate 허용 → 허용 후보의 실제 방향
                agg["n_allowed"] += 1
                agg["allowed_forward_return"] += fp.forward_return
            # wa None(warm-up) → 판단 불가, 집계 제외
    return out
