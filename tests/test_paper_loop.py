"""PaperTradingLoop 테스트 (실제 Toss/OpenAI 미호출):
5분봉 마감 실행, 중복 방지, 데이터/파이프라인 오류 생존, EOD flatten, pause/stop,
rate limit, kill switch, 결과 저장. 실제 파이프라인 wiring 은 FakeAIProvider 로."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.live.models import (
    FakeMarketDataProvider,
    LoopConfig,
    R_DATA_ERROR,
    R_NO_NEW_BAR,
    R_PAUSED,
    R_PIPELINE_ERROR,
    R_RATE_LIMITED,
    ST_ERROR,
    ST_RAN,
    ST_SKIPPED,
    ST_STOPPED,
)
from src.live.paper_loop import PaperTradingLoop
from src.models import ET, Candle
from src.order.models import InMemoryPositionStore
from src.pipeline.models import PipelineResult
from src.storage.candle_store import CandleStore
from src.risk.models import EOD_FLATTEN


def make_1m(n, start_et, base=100.0):
    """정규장 시작(start_et)부터 연속 1분봉 n개."""
    out, prev = [], base
    for i in range(n):
        m = start_et + timedelta(minutes=i)
        c = base + (i % 10) * 0.1
        out.append(Candle.from_moment("SOXL", "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000 + i, "USD", True))
        prev = c
    return out


class RecordingPipeline:
    """run_once 호출을 기록하는 가짜 파이프라인 (루프 로직만 검증)."""
    def __init__(self, raise_exc=None):
        self.calls = []
        self.raise_exc = raise_exc

    def run_once(self, **kw):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append(kw)
        return PipelineResult(kw["cycle_id"])


def loop(tmp_path, market, pipeline=None, position_store=None, cfg=None):
    return PaperTradingLoop(
        market, CandleStore(tmp_path / "m.sqlite"),
        pipeline or RecordingPipeline(), position_store or InMemoryPositionStore(),
        config=cfg or LoopConfig(universe=("SOXL",)),
        sleep=lambda s: None)


REG = datetime(2026, 7, 6, 10, 0, tzinfo=ET)   # 정규장


# ---- 5분봉 마감 시 run_once 1회 ----
def test_runs_once_on_closed_5m(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})   # 09:60→ 여러 5분봉
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe)
    rep = lp.tick()
    assert rep.status == ST_RAN
    assert len(pipe.calls) == 1
    assert pipe.calls[0]["universe"] == ["SOXL"]


# ---- 같은 5분봉 중복 실행 방지 ----
def test_no_duplicate_execution_same_bar(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe)
    lp.tick()
    rep2 = lp.tick()                       # 동일 데이터 → 새 봉 없음
    assert rep2.status == ST_SKIPPED and rep2.reason == R_NO_NEW_BAR
    assert len(pipe.calls) == 1            # 재실행 안 함


# ---- 새 5분봉이 닫히면 다시 실행 ----
def test_runs_again_on_new_bar(tmp_path):
    data = make_1m(20, REG)
    market = FakeMarketDataProvider({"SOXL": data})
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe)
    lp.tick()
    market.data["SOXL"] = make_1m(30, REG)   # 10분 더 → 새 5분봉
    rep = lp.tick()
    assert rep.status == ST_RAN
    assert len(pipe.calls) == 2


# ---- 데이터 오류 시 SKIP + 루프 생존 ----
def test_data_error_skips_and_survives(tmp_path):
    market = FakeMarketDataProvider(raise_exc=ConnectionError("net"))
    lp = loop(tmp_path, market)
    rep = lp.tick()
    assert rep.status == ST_SKIPPED and rep.reason == R_DATA_ERROR
    # 이후 정상 데이터로 복구 가능
    market.raise_exc = None
    market.data["SOXL"] = make_1m(20, REG)
    assert lp.tick().status == ST_RAN


# ---- 파이프라인 오류 시 ERROR + 루프 생존 ----
def test_pipeline_error_is_contained(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    lp = loop(tmp_path, market, RecordingPipeline(raise_exc=RuntimeError("boom")))
    rep = lp.tick()
    assert rep.status == ST_ERROR and rep.reason == R_PIPELINE_ERROR
    assert lp.stopped is False             # 시스템은 계속


# ---- pause / resume / stop ----
def test_pause_resume_stop(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe)
    lp.pause()
    assert lp.tick().reason == R_PAUSED and len(pipe.calls) == 0
    lp.resume()
    assert lp.tick().status == ST_RAN
    lp.stop()
    assert lp.tick().status == ST_STOPPED


# ---- rate limit: 세션당 최대 호출 ----
def test_rate_limit_per_session(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe, cfg=LoopConfig(universe=("SOXL",), max_calls_per_session=1))
    lp.tick()                              # 1회 실행
    market.data["SOXL"] = make_1m(30, REG)  # 새 봉
    rep = lp.tick()
    assert rep.reason == R_RATE_LIMITED and len(pipe.calls) == 1


# ---- EOD flatten: 레버리지 보유 시 애프터마켓 마감창에서 exit_intents 생성 ----
def test_eod_flatten_generates_exit_intents(tmp_path):
    post_eod = datetime(2026, 7, 6, 19, 55, tzinfo=ET)   # 20:00 ET 5분 전(POST)
    market = FakeMarketDataProvider({"SOXL": make_1m(20, post_eod)})
    ps = InMemoryPositionStore()
    ps.apply_fill("SOXL", "BUY", 10, 194, 0)              # 레버리지 보유
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe, position_store=ps)
    rep = lp.tick()
    assert rep.status == ST_RAN
    exits = pipe.calls[0]["exit_intents"]
    assert exits and exits[0].kind == EOD_FLATTEN and exits[0].symbol == "SOXL"


# ---- kill switch: system_state 에 반영, EOD flatten 은 유지 ----
def test_kill_switch_reflected(tmp_path):
    post_eod = datetime(2026, 7, 6, 19, 55, tzinfo=ET)
    market = FakeMarketDataProvider({"SOXL": make_1m(20, post_eod)})
    ps = InMemoryPositionStore()
    ps.apply_fill("SOXL", "BUY", 10, 194, 0)
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe, position_store=ps)
    lp.set_kill_switch(True)
    rep = lp.tick()
    assert pipe.calls[0]["system_state"].kill_switch is True
    assert pipe.calls[0]["exit_intents"]    # kill switch 여도 EOD flatten 유지


# ---- 결과 저장 ----
def test_results_saved(tmp_path):
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    lp = loop(tmp_path, market)
    lp.tick()
    lp.tick()
    assert len(lp.results.reports) == 2


# ---- 한 종목 데이터 실패해도 나머지는 처리(실패 격리) ----
def test_one_symbol_data_failure_isolated(tmp_path):
    market = FakeMarketDataProvider(
        {"SOXL": make_1m(20, REG), "QQQ": make_1m(20, REG)},
        fail_symbols={"QQQ"})   # QQQ 만 실패
    pipe = RecordingPipeline()
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"), pipe,
                          InMemoryPositionStore(),
                          LoopConfig(universe=("SOXL", "QQQ")), sleep=lambda s: None)
    rep = lp.tick()
    assert rep.status == ST_RAN                     # 전체 사이클 살아있음
    assert rep.error and "QQQ" in rep.error         # 실패 종목 기록
    # 파이프라인엔 두 종목 모두 전달(QQQ 는 빈 캔들)
    cbs = pipe.calls[0]["candles_by_symbol"]
    assert cbs["SOXL"] and cbs["QQQ"] == []


def test_journal_records_partial_data_error(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider(
        {"SOXL": make_1m(20, REG), "QQQ": make_1m(20, REG)}, fail_symbols={"QQQ"})
    journal = InMemoryJournalStore()
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"), RecordingPipeline(),
                          InMemoryPositionStore(), LoopConfig(universe=("SOXL", "QQQ")),
                          journal=journal, sleep=lambda s: None)
    rep = lp.tick()
    got = journal.get_cycle(rep.cycle_id)
    assert got is not None and got.error and "QQQ" in got.error   # Journal 에도 기록


def test_all_symbols_fail_skips_cycle(tmp_path):
    market = FakeMarketDataProvider({}, fail_symbols={"SOXL"})
    lp = loop(tmp_path, market, cfg=LoopConfig(universe=("SOXL",)))
    rep = lp.tick()
    assert rep.status == ST_SKIPPED and rep.reason == R_DATA_ERROR


# ---- 루프가 Journal 에 사이클을 기록 ----
def test_loop_writes_to_journal(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    pipe = RecordingPipeline()
    journal = InMemoryJournalStore()
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"), pipe,
                          InMemoryPositionStore(), LoopConfig(universe=("SOXL",)),
                          journal=journal, sleep=lambda s: None)
    rep = lp.tick()
    assert journal.get_cycle(rep.cycle_id) is not None      # 감사 로그 기록됨


def test_loop_journals_pipeline_error(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"SOXL": make_1m(20, REG)})
    journal = InMemoryJournalStore()
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"),
                          RecordingPipeline(raise_exc=RuntimeError("boom")),
                          InMemoryPositionStore(), LoopConfig(universe=("SOXL",)),
                          journal=journal, sleep=lambda s: None)
    rep = lp.tick()
    got = journal.get_cycle(rep.cycle_id)
    assert got.status == "ERROR" and got.error == "boom"


# ---- 실제 파이프라인 wiring (FakeAIProvider, 실 Toss/OpenAI 없음) ----
def test_real_pipeline_wiring_with_fake_ai(tmp_path):
    from src.ai.engine import AIConfig, AIDecisionEngine
    from src.ai.models import DEC_APPROVE
    from src.ai.providers import FakeAIProvider
    from src.backtest.execution import ExecutionModel
    from src.engines.indicator_engine import IndicatorEngine
    from src.order.manager import OrderManager
    from src.order.models import InMemoryOrderStore
    from src.order.paper_broker import PaperBrokerProvider
    from src.pipeline.pipeline import TradingPipeline
    from src.risk.risk_manager import RiskManager
    from src.universe.screener import UniverseScreener

    ps = InMemoryPositionStore()
    om = OrderManager(PaperBrokerProvider(ExecutionModel(5, 2)), InMemoryOrderStore(), ps)
    ai = AIDecisionEngine(FakeAIProvider(response={
        "decision": DEC_APPROVE, "confidence_score": 75, "entry_quality": "GOOD",
        "risk_level": "HIGH", "reason": ["ok"], "warnings": ["3x"]}), AIConfig())
    pipe = TradingPipeline(IndicatorEngine(), UniverseScreener(), ai, RiskManager(), om)
    market = FakeMarketDataProvider({"SOXL": make_1m(120, REG)})
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"), pipe, ps,
                          LoopConfig(universe=("SOXL",)), sleep=lambda s: None)
    rep = lp.tick()
    assert rep.status == ST_RAN            # 실 Toss/OpenAI 없이 사이클 완주
    assert rep.pipeline_result is not None
