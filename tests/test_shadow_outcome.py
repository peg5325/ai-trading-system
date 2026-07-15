"""Offline Shadow Outcome(PR 4-B) — 세션 후 read-only batch 로 forward return 부착.

- look-ahead strict(`> decision_time`)·세션(거래일) clip·horizon 세션말 초과 → INCOMPLETE(집계 제외).
- avoided_loss_h=max(-fwd,0)·missed_profit_h=max(fwd,0) (price-direction shadow).
- 런타임 shadow 레코드에는 여전히 미래 outcome 없음(4-B 는 오프라인 dataset 에만 부착).
- src/replay/ 불변, 원본(journal/candle) read-only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.journal.models import InMemoryJournalStore, JournalCycle
from src.models import Candle
from src.shadow.outcome import aggregate_by_policy, build_shadow_outcomes


def _c(symbol, minute, close, hour=14):
    ts = datetime(2026, 7, 7, hour, minute, tzinfo=timezone.utc)  # 10:xx ET(정규장)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=ts,
                             o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                             currency="USD", adjusted=True)


class FakeCandleStore:
    def __init__(self, by_symbol):
        self.by_symbol = by_symbol

    def query(self, symbol, interval, **kw):
        return sorted(self.by_symbol.get(symbol, []), key=lambda c: c.ts_utc)


def _iso(hour, minute):
    return f"2026-07-07T{hour:02d}:{minute:02d}:00Z"


def _shadow(decision_iso, outcomes, policies):
    return {
        "shadow_version": "1", "policy_version": "1", "decision_time": decision_iso,
        "market_context_snapshot": {"benchmark_symbol": "QQQ"},
        "policies": policies,
        "candidate_ids": [o["candidate_id"] for o in outcomes if o.get("candidate_id")],
    }


def _cycle(cid, decision_iso, outcomes, policies):
    return JournalCycle(
        cycle_id=cid, cycle_ts=decision_iso, session="REGULAR", universe=["QQQ"],
        status="RAN", reason=None, error=None, outcomes=outcomes,
        shadow=_shadow(decision_iso, outcomes, policies))


def _journal(*cycles):
    j = InMemoryJournalStore()
    for c in cycles:
        j.save_cycle(c)
    return j


_POL_BLOCK = [{"policy_id": "A", "would_allow": False, "reason_codes": ["CLOSE_BELOW_EMA"]}]
_POL_ALLOW = [{"policy_id": "A", "would_allow": True, "reason_codes": ["CLOSE_ABOVE_EMA"]}]


def _fmap(rec):
    return {f.horizon_min: f for f in rec.forwards}


def test_forward_return_and_directional_shadow():
    # AAA: 결정봉(14:00)=100 → 14:30=95(-5%), 15:00=98(-2%)
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0),
                                     _c("AAA", 0, 98.0, hour=15), _c("AAA", 30, 102.0, hour=15)]})
    cyc = _cycle("c1", _iso(14, 0),
                 [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30, 60))
    assert len(ds.records) == 1
    rec = ds.records[0]
    assert rec.candidate_id == "cid1" and rec.symbol == "AAA"
    fp30 = _fmap(rec)[30]
    assert fp30.complete is True
    assert abs(fp30.forward_return - (-0.05)) < 1e-9
    assert abs(fp30.avoided_loss - 0.05) < 1e-9 and fp30.missed_profit == 0.0
    fp60 = _fmap(rec)[60]
    assert abs(fp60.forward_return - (-0.02)) < 1e-9 and abs(fp60.avoided_loss - 0.02) < 1e-9


def test_missed_profit_when_price_rises():
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 106.0),
                                     _c("AAA", 0, 100.0, hour=15)]})
    cyc = _cycle("c1", _iso(14, 0), [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30,))
    fp = _fmap(ds.records[0])[30]
    assert abs(fp.forward_return - 0.06) < 1e-9
    assert fp.avoided_loss == 0.0 and abs(fp.missed_profit - 0.06) < 1e-9


def test_horizon_beyond_session_is_incomplete():
    # 결정 15:20, 세션말 15:30 → 30m(15:50) 은 세션 초과 → INCOMPLETE
    store = FakeCandleStore({"AAA": [_c("AAA", 20, 100.0, hour=15), _c("AAA", 30, 101.0, hour=15)]})
    cyc = _cycle("c1", _iso(15, 20), [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30,))
    fp = _fmap(ds.records[0])[30]
    assert fp.complete is False
    assert fp.forward_return is None and fp.avoided_loss is None and fp.missed_profit is None


def test_strict_lookahead_reference_is_decision_bar():
    # 결정 14:30 과 동일 ts 봉은 참조(결정봉)이지 forward 가 아니다(strict >).
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 110.0),
                                     _c("AAA", 45, 121.0), _c("AAA", 0, 121.0, hour=16)]})
    cyc = _cycle("c1", _iso(14, 30), [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(cyc), store, horizons_min=(15,))
    rec = ds.records[0]
    assert abs(rec.reference_price - 110.0) < 1e-9   # 결정봉(14:30) close
    fp = _fmap(rec)[15]                              # target 14:45
    assert abs(fp.forward_return - ((121.0 - 110.0) / 110.0)) < 1e-9


def test_fanout_multiple_candidates_per_cycle():
    store = FakeCandleStore({
        "AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 90.0), _c("AAA", 0, 90.0, hour=15)],
        "BBB": [_c("BBB", 0, 50.0), _c("BBB", 30, 55.0), _c("BBB", 0, 55.0, hour=15)],
    })
    cyc = _cycle("c1", _iso(14, 0),
                 [{"symbol": "AAA", "candidate_id": "cidA"},
                  {"symbol": "BBB", "candidate_id": "cidB"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30,))
    by_sym = {r.symbol: r for r in ds.records}
    assert set(by_sym) == {"AAA", "BBB"}
    assert abs(_fmap(by_sym["AAA"])[30].forward_return - (-0.10)) < 1e-9
    assert abs(_fmap(by_sym["BBB"])[30].forward_return - 0.10) < 1e-9


def test_cycle_without_shadow_or_candidate_skipped():
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0),
                                     _c("AAA", 0, 95.0, hour=15)]})
    no_shadow = JournalCycle(cycle_id="c0", cycle_ts=_iso(14, 0), session="REGULAR",
                             universe=["QQQ"], status="RAN", reason=None, error=None,
                             outcomes=[{"symbol": "AAA", "candidate_id": "cid1"}], shadow=None)
    no_cand = _cycle("c1", _iso(14, 0), [{"symbol": "AAA", "candidate_id": None}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(no_shadow, no_cand), store, horizons_min=(30,))
    assert ds.records == []


def test_aggregate_by_policy_excludes_incomplete():
    store = FakeCandleStore({
        "AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 90.0), _c("AAA", 0, 90.0, hour=15)],   # -10%
        "BBB": [_c("BBB", 0, 100.0), _c("BBB", 30, 108.0), _c("BBB", 0, 108.0, hour=15)],  # +8%
        "CCC": [_c("CCC", 20, 100.0, hour=15)],   # 결정 15:20 → 30m 세션 초과 INCOMPLETE
    })
    c1 = _cycle("c1", _iso(14, 0),
                [{"symbol": "AAA", "candidate_id": "a"}, {"symbol": "BBB", "candidate_id": "b"}],
                _POL_BLOCK)
    c2 = _cycle("c2", _iso(15, 20), [{"symbol": "CCC", "candidate_id": "c"}], _POL_BLOCK)
    ds = build_shadow_outcomes(_journal(c1, c2), store, horizons_min=(30,))
    agg = aggregate_by_policy(ds, 30)
    a = agg["A"]
    assert a["n_complete"] == 2 and a["n_blocked"] == 2   # CCC(incomplete) 제외
    assert abs(a["blocked_avoided_loss"] - 0.10) < 1e-9   # AAA 하락
    assert abs(a["blocked_missed_profit"] - 0.08) < 1e-9  # BBB 상승


def test_runtime_record_still_has_no_future_outcome():
    """4-B 는 오프라인 dataset 에만 forward 부착 — 런타임 shadow 레코드는 불변(미래 outcome 없음)."""
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0),
                                     _c("AAA", 0, 95.0, hour=15)]})
    cyc = _cycle("c1", _iso(14, 0), [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    journal = _journal(cyc)
    build_shadow_outcomes(journal, store, horizons_min=(30,))
    runtime_shadow = journal.get_cycle("c1").shadow
    assert "forward_return" not in str(runtime_shadow)
    assert "avoided_loss" not in str(runtime_shadow) and "missed_profit" not in str(runtime_shadow)


def test_deterministic():
    store = FakeCandleStore({"AAA": [_c("AAA", 0, 100.0), _c("AAA", 30, 95.0),
                                     _c("AAA", 0, 95.0, hour=15)]})
    cyc = _cycle("c1", _iso(14, 0), [{"symbol": "AAA", "candidate_id": "cid1"}], _POL_BLOCK)
    d1 = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30,), built_at="2026-07-07T20:00:00Z")
    d2 = build_shadow_outcomes(_journal(cyc), store, horizons_min=(30,), built_at="2026-07-07T20:00:00Z")
    assert d1.to_dicts() == d2.to_dicts()


def test_no_replay_or_order_import():
    """오프라인 batch 는 src/replay/ 및 매매 경로를 import 하지 않는다."""
    src = Path("src/shadow/outcome.py").read_text(encoding="utf-8")
    for banned in ("src.replay", "src.order", "src.broker", "src.pipeline"):
        assert banned not in src
