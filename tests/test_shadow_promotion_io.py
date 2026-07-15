"""Phase 6 리포트 assembly(journal/candle/manifest → SessionSignal) 테스트 (PR #38).

promotion_io 는 read-only 조립 계층 — 순수 판정(promotion.py)과 파일 I/O CLI 사이. 실제 DB 없이
duck-typed fake(기존 test_shadow_outcome 패턴)로 계약을 강제한다. entry_only=True(ORDERED) 신호,
manifest 미존재 시 보수적 UNKNOWN(=primary 제외), regime 최빈값 조립을 검증.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.journal.models import InMemoryJournalStore, JournalCycle
from src.models import Candle
from src.shadow.promotion import evaluate_promotion, GATE_NOT_MET
from src.shadow.promotion_io import analyze_session, session_regime, to_signal


def _c(symbol, minute, close, hour=14):
    ts = datetime(2026, 7, 7, hour, minute, tzinfo=timezone.utc)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=ts,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


def _iso(hour, minute):
    return f"2026-07-07T{hour:02d}:{minute:02d}:00Z"


def _shadow(decision_iso, outcomes, policies, trend="DOWN"):
    return {
        "shadow_version": "1", "policy_version": "1", "decision_time": decision_iso,
        "market_context_snapshot": {"benchmark_symbol": "QQQ", "benchmark_trend": trend},
        "policies": policies,
        "candidate_ids": [o["candidate_id"] for o in outcomes if o.get("candidate_id")],
    }


def _cycle(cid, decision_iso, outcomes, policies, trend="DOWN"):
    return JournalCycle(
        cycle_id=cid, cycle_ts=decision_iso, session="REGULAR", universe=["AAA"],
        status="RAN", reason=None, error=None, outcomes=outcomes,
        shadow=_shadow(decision_iso, outcomes, policies, trend))


class FakeCandleStore:
    def __init__(self, by_symbol):
        self.by_symbol = by_symbol

    def query(self, symbol, interval, **kw):
        return sorted(self.by_symbol.get(symbol, []), key=lambda c: c.ts_utc)


class FakeManifest:
    class _Id:
        market = "US"
        session_id = "sess-x"
    def __init__(self, completion="FULL", manual=0, created="2026-07-07T13:00:00Z"):
        self.completion_status = completion
        self.manual_flatten_count = manual
        self.created_at = created
        self.identity = self._Id()


_POL_BLOCK = [{"policy_id": "A", "would_allow": False, "reason_codes": ["CLOSE_BELOW_EMA"]}]


def test_session_regime_is_mode_across_cycles():
    j = InMemoryJournalStore()
    j.save_cycle(_cycle("c1", _iso(14, 0), [{"symbol": "AAA", "candidate_id": "x1"}],
                        _POL_BLOCK, trend="DOWN"))
    j.save_cycle(_cycle("c2", _iso(14, 5), [{"symbol": "AAA", "candidate_id": "x2"}],
                        _POL_BLOCK, trend="DOWN"))
    j.save_cycle(_cycle("c3", _iso(14, 10), [{"symbol": "AAA", "candidate_id": "x3"}],
                        _POL_BLOCK, trend="UP"))
    assert session_regime(j) == "DOWN"


def test_analyze_session_entry_only_blocked_signal():
    # 결정봉 14:00=100 → 14:30=95(-5%). ORDERED 후보가 gate 차단(would_allow=False)
    # → avoided_loss=+0.05. entry_only 이므로 ORDERED 만 집계.
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0)]})
    cyc = _cycle("c1", _iso(14, 0),
                 [{"symbol": "AAA", "candidate_id": "cid1", "action": "ORDERED"}],
                 _POL_BLOCK)
    j = InMemoryJournalStore()
    j.save_cycle(cyc)
    inputs, aggregates = analyze_session(
        j, store, manifest=FakeManifest(), session_id="sess-x", horizons=(30, 60))
    assert inputs.market == "US"
    assert inputs.completion_status == "FULL"
    assert inputs.regime == "DOWN"
    a30 = aggregates[30]["A"]
    assert a30["n_blocked"] == 1
    assert abs(a30["blocked_avoided_loss"] - 0.05) < 1e-9


def test_analyze_session_skipped_excluded_by_entry_only():
    # SKIPPED 후보는 entry_only 집계에서 빠져 blocked 신호 0
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0)]})
    cyc = _cycle("c1", _iso(14, 0),
                 [{"symbol": "AAA", "candidate_id": "cid1", "action": "SKIPPED"}],
                 _POL_BLOCK)
    j = InMemoryJournalStore()
    j.save_cycle(cyc)
    _, aggregates = analyze_session(
        j, store, manifest=FakeManifest(), session_id="sess-x", horizons=(30,))
    assert aggregates[30].get("A", {}).get("n_blocked", 0) == 0


def test_no_manifest_is_unknown_completion_and_excluded():
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0)]})
    cyc = _cycle("c1", _iso(14, 0),
                 [{"symbol": "AAA", "candidate_id": "cid1", "action": "ORDERED"}],
                 _POL_BLOCK)
    j = InMemoryJournalStore()
    j.save_cycle(cyc)
    inputs, _ = analyze_session(j, store, manifest=None, session_id="sess-x",
                               horizons=(30,))
    assert inputs.completion_status == "UNKNOWN"
    sig = to_signal(inputs, {"blocked_avoided_loss": 0.05, "blocked_missed_profit": 0.0,
                             "n_blocked": 1}, 30)
    assert sig.is_primary is False   # UNKNOWN 은 primary 아님(보수적 제외)


def test_to_signal_maps_aggregate_fields():
    inputs, _ = analyze_session(
        InMemoryJournalStore(), FakeCandleStore({}), manifest=FakeManifest(),
        session_id="sess-x", horizons=(30,))
    sig = to_signal(inputs, {"blocked_avoided_loss": 0.03, "blocked_missed_profit": 0.01,
                             "n_blocked": 2}, 30)
    assert sig.blocked_avoided_loss == 0.03
    assert sig.blocked_missed_profit == 0.01
    assert sig.n_blocked == 2
    assert abs(sig.net - 0.02) < 1e-12


def test_to_signal_missing_policy_agg_is_zero_neutral():
    inputs, _ = analyze_session(
        InMemoryJournalStore(), FakeCandleStore({}), manifest=FakeManifest(),
        session_id="sess-x", horizons=(30,))
    sig = to_signal(inputs, None, 30)   # 이 세션에 해당 policy 집계 없음
    assert sig.blocked_avoided_loss == 0.0
    assert sig.net == 0.0
    assert sig.is_primary is True       # FULL 이면 여전히 primary(중립 기여)


def test_end_to_end_insufficient_sessions_not_met():
    inputs, _ = analyze_session(
        InMemoryJournalStore(), FakeCandleStore({}), manifest=FakeManifest(),
        session_id="only", horizons=(30,))
    sig = to_signal(inputs, {"blocked_avoided_loss": 0.02, "blocked_missed_profit": 0.0,
                             "n_blocked": 1}, 30)
    report = evaluate_promotion([sig], policy_id="A", horizon_min=30, market="US")
    assert report.gate_status == GATE_NOT_MET   # 1개 세션 → FULL<5
