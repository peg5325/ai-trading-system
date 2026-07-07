"""Runtime Metrics Store 테스트.

MetricsStore(SQLite/InMemory) CRUD·query·aggregate, build_cycle_metric/cycle_delta,
PaperRuntime 통합(cycle별 per-cycle delta + health 영속). 실 sleep/네트워크 없음.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.journal.models import InMemoryJournalStore
from src.live.models import FakeMarketDataProvider, LoopConfig
from src.live.paper_loop import PaperTradingLoop
from src.models import ET, Candle
from src.order.models import InMemoryPositionStore
from src.pipeline.models import ACTION_ORDERED, R_ORDERED, PipelineResult, SymbolOutcome
from src.runtime.config import RuntimeConfig
from src.runtime.metrics_store import (
    CycleMetric,
    InMemoryMetricsStore,
    SqliteMetricsStore,
    build_cycle_metric,
    cycle_delta,
)
from src.runtime.paper_runtime import PaperRuntime

REG = datetime(2026, 7, 6, 10, 0, tzinfo=ET)
NOW = "2026-07-06T14:00:00Z"


def make_1m(n, start=REG):
    out, prev = [], 100.0
    for i in range(n):
        m = start + timedelta(minutes=i)
        c = 100.0 + (i % 8) * 0.1
        out.append(Candle.from_moment("SOXL", "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000, "USD", True))
        prev = c
    return out


class ControlledPipeline:
    def __init__(self, outcomes_fn=None, raise_exc=None):
        self.outcomes_fn = outcomes_fn or (lambda: [])
        self.raise_exc = raise_exc

    def run_once(self, **kw):
        if self.raise_exc:
            raise self.raise_exc
        return PipelineResult(kw["cycle_id"], self.outcomes_fn())


def ordered_outcome():
    return SymbolOutcome(
        "SOXL", ACTION_ORDERED, R_ORDERED,
        ai_decision={"decision": "APPROVE_ENTRY", "confidence_score": 80},
        risk_decision={"allowed": True},
        order_record={"symbol": "SOXL", "side": "BUY", "kind": "NEW_ENTRY",
                      "status": "FILLED", "filled_quantity": 10, "avg_fill_price": 100.1},
        candidate={"symbol": "SOXL", "score": 82},
        indicator_snapshot={"close": 100.1},
        ai_log={"latency_ms": 12, "input_hash": "a" * 64})


def build(tmp_path, store, *, pipeline=None):
    cfg = RuntimeConfig(symbols=["SOXL"], poll_interval_s=0.0,
                        log_dir=str(tmp_path / "logs"),
                        status_path=str(tmp_path / "status.json"),
                        reports_dir=str(tmp_path / "reports"))
    from src.storage.candle_store import CandleStore
    loop = PaperTradingLoop(
        FakeMarketDataProvider({"SOXL": make_1m(20)}), CandleStore(tmp_path / "m.sqlite"),
        pipeline or ControlledPipeline(lambda: [ordered_outcome()]),
        InMemoryPositionStore(), LoopConfig(universe=("SOXL",)),
        journal=InMemoryJournalStore(), sleep=lambda s: None)
    return PaperRuntime(loop, cfg, sleep=lambda s: None, metrics_store=store,
                        now_iso=lambda: NOW)


# ---- 통합: cycle별 영속 ----
def test_runtime_persists_per_cycle_metrics(tmp_path):
    store = InMemoryMetricsStore()
    rt = build(tmp_path, store)
    rt.run(max_iterations=3)
    assert len(store.rows) == 3                     # 매 cycle 1 row
    first = store.rows[0]
    assert first.status == "RAN" and first.cycles_run == 1
    assert first.orders == 1 and first.buys == 1 and first.candidates == 1
    assert first.ai_approve == 1 and first.ai_ms == 12.0
    assert first.health_status == "OK" and first.api_ok is True
    assert first.created_at == NOW and first.loop_ms >= 0
    # 이후 cycle 은 NO_NEW_BAR → SKIPPED, per-cycle count 0
    assert store.rows[1].status == "SKIPPED" and store.rows[1].cycles_run == 0
    assert store.rows[1].orders == 0


def test_runtime_aggregate_matches(tmp_path):
    store = InMemoryMetricsStore()
    rt = build(tmp_path, store)
    rt.run(max_iterations=3)
    agg = store.aggregate()
    assert agg["rows"] == 3
    assert agg["cycles_run"] == 1 and agg["skipped"] == 2
    assert agg["orders"] == 1 and agg["buys"] == 1
    assert agg["degraded_cycles"] == 0 and agg["max_loop_ms"] >= 0


def test_runtime_pipeline_error_recorded(tmp_path):
    store = InMemoryMetricsStore()
    rt = build(tmp_path, store, pipeline=ControlledPipeline(raise_exc=RuntimeError("boom")))
    rt.run(max_iterations=1)
    row = store.rows[0]
    assert row.status == "ERROR" and row.pipeline_errors == 1
    assert row.health_status in ("OK", "DEGRADED")


def test_no_store_is_noop(tmp_path):
    # metrics_store 없이도 정상 동작(하위호환)
    cfg = RuntimeConfig(symbols=["SOXL"], poll_interval_s=0.0,
                        log_dir=str(tmp_path / "logs"),
                        status_path=str(tmp_path / "status.json"),
                        reports_dir=str(tmp_path / "reports"))
    from src.storage.candle_store import CandleStore
    loop = PaperTradingLoop(
        FakeMarketDataProvider({"SOXL": make_1m(20)}), CandleStore(tmp_path / "m.sqlite"),
        ControlledPipeline(lambda: [ordered_outcome()]), InMemoryPositionStore(),
        LoopConfig(universe=("SOXL",)), journal=InMemoryJournalStore(), sleep=lambda s: None)
    rt = PaperRuntime(loop, cfg, sleep=lambda s: None)
    rt.run(max_iterations=2)
    assert rt.metrics.cycles_run == 1


# ---- 유닛: build/delta ----
def test_cycle_delta():
    before = {"cycles_run": 5, "orders": 3, "exceptions": 1}
    after = {"cycles_run": 6, "orders": 5, "exceptions": 1}
    d = cycle_delta(before, after)
    assert d["cycles_run"] == 1 and d["orders"] == 2 and d["exceptions"] == 0


def test_build_cycle_metric_none_report():
    # 사이클 전체 예외(report None) → ERROR row, exceptions delta 반영
    cm = build_cycle_metric(None, 0.05, {"status": "DEGRADED", "api": False},
                            {"exceptions": 1}, created_at=NOW)
    assert cm.status == "ERROR" and cm.reason == "EXCEPTION"
    assert cm.cycle_id is None and cm.exceptions == 1
    assert cm.loop_ms == 50.0 and cm.health_status == "DEGRADED" and cm.api_ok is False
    assert cm.ai_ms is None


# ---- SQLite store ----
def test_sqlite_store_record_query_aggregate(tmp_path):
    store = SqliteMetricsStore(tmp_path / "metrics.sqlite")
    store.record(build_cycle_metric_helper("RAN", "OK", orders=1, created_at="2026-07-06T14:00:00Z"))
    store.record(build_cycle_metric_helper("SKIPPED", "OK", created_at="2026-07-06T14:00:20Z"))
    store.record(build_cycle_metric_helper("RAN", "DEGRADED", orders=2,
                                           created_at="2026-07-06T14:00:40Z"))
    assert len(store.query()) == 3
    assert len(store.query(status="RAN")) == 2
    assert len(store.query(health_status="DEGRADED")) == 1
    agg = store.aggregate()
    assert agg["rows"] == 3 and agg["orders"] == 3 and agg["degraded_cycles"] == 1
    # 시간 필터
    lo = datetime(2026, 7, 6, 14, 0, 30)
    assert len(store.query(start=lo)) == 1
    store.close()


def build_cycle_metric_helper(status, health, *, orders=0, created_at=None):
    cm = CycleMetric(created_at=created_at, cycle_id="C", cycle_ts=None, session="REGULAR",
                     status=status, reason="OK", loop_ms=10.0, ai_ms=None,
                     fetch_latency_ms=None, health_status=health,
                     api_ok=True, ai_ok=True, broker_ok=True, journal_ok=True, replay_ok=True)
    cm.orders = orders
    cm.cycles_run = 1 if status == "RAN" else 0
    return cm
