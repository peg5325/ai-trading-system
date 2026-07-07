"""PaperRuntime 테스트: run/pause/resume/shutdown, report, health, recovery, status.json.
실주문 없음(PaperBroker), 실 Toss/OpenAI 없음(Fake), 실시간 sleep 없음."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

from src.journal.models import InMemoryJournalStore
from src.live.models import FakeMarketDataProvider, LoopConfig
from src.live.paper_loop import PaperTradingLoop
from src.models import ET, Candle
from src.order.models import InMemoryPositionStore
from src.pipeline.models import (
    ACTION_ORDERED,
    ACTION_SKIPPED,
    R_ORDERED,
    PipelineResult,
    SymbolOutcome,
)
from src.runtime.config import RuntimeConfig
from src.runtime.paper_runtime import PAUSED, RUNNING, STOPPED, PaperRuntime

REG = datetime(2026, 7, 6, 10, 0, tzinfo=ET)


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
    """지정한 outcomes 를 담은 PipelineResult 반환."""
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
        risk_decision={"allowed": True, "reason_codes": [], "risk_flags": ["LEVERAGED_ETF"]},
        order_record={"symbol": "SOXL", "side": "BUY", "kind": "NEW_ENTRY",
                      "status": "FILLED", "filled_quantity": 10, "avg_fill_price": 100.1},
        candidate={"symbol": "SOXL", "score": 82},
        indicator_snapshot={"close": 100.1},
        ai_log={"latency_ms": 12, "input_hash": "a" * 64})


def build(tmp_path, *, pipeline=None, market=None, candle_store=None):
    cfg = RuntimeConfig(
        symbols=["SOXL"], poll_interval_s=0.0,
        log_dir=str(tmp_path / "logs"), status_path=str(tmp_path / "status.json"),
        reports_dir=str(tmp_path / "reports"))
    from src.storage.candle_store import CandleStore
    store = candle_store or CandleStore(tmp_path / "m.sqlite")
    loop = PaperTradingLoop(
        market or FakeMarketDataProvider({"SOXL": make_1m(20)}), store,
        pipeline or ControlledPipeline(), InMemoryPositionStore(),
        LoopConfig(universe=("SOXL",)), journal=InMemoryJournalStore(), sleep=lambda s: None)
    rt = PaperRuntime(loop, cfg, sleep=lambda s: None)
    return rt, cfg


# ---- run (bounded) + report + status.json ----
def test_run_generates_report_and_status(tmp_path):
    rt, cfg = build(tmp_path)
    report_path = rt.run(max_iterations=3)
    assert os.path.exists(report_path)
    assert "Paper Trading Report" in open(report_path, encoding="utf-8").read()
    assert os.path.exists(cfg.status_path)
    status = json.load(open(cfg.status_path, encoding="utf-8"))
    assert status["state"] == STOPPED and "health" in status and "metrics" in status
    assert rt.metrics.cycles_run == 1        # 20개 1분봉 → 첫 사이클만 RAN, 이후 NO_NEW_BAR


# ---- order 집계 ----
def test_metrics_count_orders(tmp_path):
    rt, _ = build(tmp_path, pipeline=ControlledPipeline(lambda: [ordered_outcome()]))
    rt.run(max_iterations=1)
    assert rt.metrics.orders == 1 and rt.metrics.buys == 1 and rt.metrics.ai_approve == 1
    assert rt.metrics.candidates == 1 and rt.metrics.avg_ai_ms() == 12


# ---- pause: 사이클 실행 안 함 ----
def test_pause_skips_cycles(tmp_path):
    rt, _ = build(tmp_path)
    rt.state = RUNNING
    rt.pause()
    assert rt.state == PAUSED
    rt.run(max_iterations=3)          # PAUSED 유지 → 사이클 0
    assert rt.metrics.iterations == 0


# ---- resume: 다시 실행 ----
def test_resume_runs_again(tmp_path):
    rt, _ = build(tmp_path)
    rt.state = RUNNING
    rt.pause()
    rt.resume()
    assert rt.state == RUNNING
    rt.run(max_iterations=1)
    assert rt.metrics.cycles_run == 1


# ---- graceful shutdown via stop() ----
def test_stop_triggers_graceful_shutdown(tmp_path):
    rt, cfg = build(tmp_path)
    path = rt.run(max_iterations=10, after_cycle=lambda r: r.stop())  # 첫 사이클 후 stop
    assert rt.state == STOPPED
    assert os.path.exists(path)               # 종료 리포트 생성
    assert rt.metrics.iterations == 1         # stop 이후 추가 사이클 없음


# ---- health check: 정상 ----
def test_health_ok(tmp_path):
    rt, _ = build(tmp_path)
    rt.run(max_iterations=1)
    h = rt.health_check()
    assert set(h) == {"status", "api", "ai", "broker", "journal", "replay"}
    assert h["status"] == "OK" and h["api"] and h["broker"] and h["journal"]


# ---- health check: 데이터 오류 → DEGRADED ----
def test_health_degraded_on_data_error(tmp_path):
    rt, _ = build(tmp_path, market=FakeMarketDataProvider(raise_exc=ConnectionError("net")))
    rt.run(max_iterations=1)
    assert rt.metrics.api_errors == 1
    assert rt.health_check()["api"] is False
    assert rt.health_check()["status"] == "DEGRADED"


# ---- recovery: SQLite lock 자동 복구(재시도) 후 생존 ----
def test_recovery_sqlite_lock(tmp_path):
    class Locking:
        def query(self, *a, **k): return []
        def upsert_many(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
    rt, _ = build(tmp_path, candle_store=Locking())
    rt.run(max_iterations=1)
    assert rt.metrics.sqlite_locks >= 1
    assert rt.state == STOPPED                # 크래시 없이 정상 종료


# ---- recovery: 일반 예외 흡수 후 생존 ----
def test_recovery_generic_exception(tmp_path):
    class Boom:
        def upsert_many(self, *a, **k): pass
        def query(self, *a, **k): raise RuntimeError("boom")
    rt, _ = build(tmp_path, candle_store=Boom())
    rt.run(max_iterations=1)
    assert rt.metrics.exceptions >= 1
    assert rt.state == STOPPED


# ---- status.json 대시보드 필드 ----
def test_status_json_fields(tmp_path):
    rt, cfg = build(tmp_path, pipeline=ControlledPipeline(lambda: [ordered_outcome()]))
    rt.run(max_iterations=1)
    s = json.load(open(cfg.status_path, encoding="utf-8"))
    for k in ("running", "state", "last_cycle", "candidate", "ai", "order", "pnl", "health", "metrics"):
        assert k in s
    assert s["order"] == 1 and s["candidate"] == 1


# ---- 로그 파일 생성(rotation 핸들러) ----
def test_log_files_created(tmp_path):
    rt, cfg = build(tmp_path, pipeline=ControlledPipeline(lambda: [ordered_outcome()]))
    rt.run(max_iterations=1)
    for f in ("runtime.log", "error.log", "trade.log", "review.log"):
        assert os.path.exists(os.path.join(cfg.log_dir, f))
    assert "SOXL BUY" in open(os.path.join(cfg.log_dir, "trade.log"), encoding="utf-8").read()


# ---- report 섹션 ----
def test_report_sections(tmp_path):
    rt, _ = build(tmp_path, pipeline=ControlledPipeline(lambda: [ordered_outcome()]))
    path = rt.run(max_iterations=1)
    content = open(path, encoding="utf-8").read()
    for sec in ("## Cycles", "## Decisions", "## Orders", "## Stability", "## Resources"):
        assert sec in content


# ---- config from_dict (yaml 매핑) ----
def test_config_from_dict():
    cfg = RuntimeConfig.from_dict({
        "symbols": ["SOXL", "TQQQ"], "poll_interval_s": 15, "fetch_spacing_s": 0.5,
        "ai": {"provider": "openai", "model": "gpt-5.5-mini"},
        "paper_broker": {"slippage_bps": 7, "fee_bps": 3},
        "logging": {"dir": "logs", "max_bytes": 500000, "backup_count": 2}})
    assert cfg.symbols == ["SOXL", "TQQQ"] and cfg.poll_interval_s == 15
    assert cfg.fetch_spacing_s == 0.5
    assert cfg.ai_provider == "openai" and cfg.slippage_bps == 7 and cfg.log_backup_count == 2


def test_config_fetch_spacing_default_zero():
    # 미지정 시 0.0(간격 끔) — 기존 단일종목 config 하위호환
    assert RuntimeConfig.from_dict({"symbols": ["SOXL"]}).fetch_spacing_s == 0.0
