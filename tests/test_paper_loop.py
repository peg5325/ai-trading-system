"""PaperTradingLoop 테스트 (실제 Toss/OpenAI 미호출):
5분봉 마감 실행, 중복 방지, 데이터/파이프라인 오류 생존, EOD flatten, pause/stop,
rate limit, kill switch, 결과 저장. 실제 파이프라인 wiring 은 FakeAIProvider 로."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.exits.generator import ExitConfig
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
from src.models import ET, KST, Candle
from src.order.models import InMemoryPositionStore
from src.pipeline.models import PipelineResult, SymbolOutcome
from src.storage.candle_store import CandleStore
from src.pnl.models import FillEvent
from src.risk.models import EOD_FLATTEN


def make_1m(n, start_et, base=100.0, symbol="SOXL"):
    """정규장 시작(start_et)부터 연속 1분봉 n개."""
    out, prev = [], base
    for i in range(n):
        m = start_et + timedelta(minutes=i)
        c = base + (i % 10) * 0.1
        out.append(Candle.from_moment(symbol, "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000 + i, "USD", True))
        prev = c
    return out


def make_kr_1m(n, start_kst, base=100.0, symbol="005930"):
    """KR 거래소 로컬 시각부터 연속 1분봉 n개."""
    out = []
    for i in range(n):
        moment = start_kst + timedelta(minutes=i)
        price = base + i
        out.append(Candle.from_moment(
            symbol, "1m", moment, price, price, price, price, 1000 + i, "KRW", True))
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
    lp.account.apply_fill(FillEvent("SOXL", "BUY", 10.0, 194.0))  # 청산 소스=account(FIFO)
    rep = lp.tick()
    assert rep.status == ST_RAN
    exits = pipe.calls[0]["exit_intents"]
    assert exits and exits[0].kind == EOD_FLATTEN and exits[0].symbol == "SOXL"


def test_eod_flatten_uses_position_latest_1m_when_closed_5m_is_stale(tmp_path):
    """#44: 대표 종목은 15:20 봉이 닫혔지만 보유 종목은 15:15 봉만 닫힌 skew.

    보유 종목의 최신 1분봉(15:24)은 자기 시장 EOD 창 안이므로 같은 tick 에 청산해야 한다.
    청산 가격은 여전히 마지막 *닫힌* 5분봉 종가를 사용한다.
    """
    position_1m = make_kr_1m(
        10, datetime(2026, 7, 8, 15, 15, tzinfo=KST), base=100.0, symbol="005930")
    representative_1m = make_kr_1m(
        6, datetime(2026, 7, 8, 15, 20, tzinfo=KST), base=200.0, symbol="402340")
    market = FakeMarketDataProvider({
        "005930": position_1m,
        "402340": representative_1m,
    })
    pipe = RecordingPipeline()
    cfg = LoopConfig(
        universe=("005930", "402340"),
        exit_config=ExitConfig(enable_all_symbols_eod_flatten=True),
    )
    lp = loop(tmp_path, market, pipe, cfg=cfg)
    lp.account.apply_fill(FillEvent("005930", "BUY", 10.0, 100.0))

    rep = lp.tick()

    assert rep.status == ST_RAN
    assert pipe.calls[0]["cycle_id"].endswith("062000Z")  # 15:20 KST 대표 닫힌 봉
    exits = pipe.calls[0]["exit_intents"]
    assert len(exits) == 1
    assert exits[0].kind == EOD_FLATTEN
    assert exits[0].symbol == "005930"
    assert exits[0].price == position_1m[4].close  # 15:15 버킷의 닫힌 종가


# ---- kill switch: system_state 에 반영, EOD flatten 은 유지 ----
def test_kill_switch_reflected(tmp_path):
    post_eod = datetime(2026, 7, 6, 19, 55, tzinfo=ET)
    market = FakeMarketDataProvider({"SOXL": make_1m(20, post_eod)})
    ps = InMemoryPositionStore()
    ps.apply_fill("SOXL", "BUY", 10, 194, 0)
    pipe = RecordingPipeline()
    lp = loop(tmp_path, market, pipe, position_store=ps)
    lp.account.apply_fill(FillEvent("SOXL", "BUY", 10.0, 194.0))  # 청산 소스=account(FIFO)
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


# ---- v2.1 PR 2-B: Market Context 관측 (매매 무영향, §3-6) ----
from src.market_context.models import MarketContextConfig     # noqa: E402


class _CountingMarket:
    """market.recent 호출 수를 세는 스파이 (호출예산 검증)."""
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def recent(self, symbol, interval, count):
        self.calls += 1
        return self.inner.recent(symbol, interval, count)


def _mc_loop(tmp_path, market, journal, *, enabled, name="m", universe=("QQQ",),
             benchmark="QQQ"):
    return PaperTradingLoop(
        market, CandleStore(tmp_path / f"{name}.sqlite"), RecordingPipeline(),
        InMemoryPositionStore(),
        LoopConfig(universe=universe,
                   market_context=MarketContextConfig(enabled=enabled,
                                                      benchmark_symbol=benchmark)),
        journal=journal, sleep=lambda s: None)


def test_market_context_journaled_when_enabled(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _mc_loop(tmp_path, market, journal, enabled=True).tick()
    assert rep.status == ST_RAN
    bc = journal.get_cycle(rep.cycle_id).benchmark_context
    assert bc is not None and bc["benchmark_symbol"] == "QQQ"
    assert bc["market_context_version"]


def test_market_context_absent_when_disabled(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _mc_loop(tmp_path, market, journal, enabled=False).tick()
    assert journal.get_cycle(rep.cycle_id).benchmark_context is None


def test_market_context_no_extra_fetch_budget(tmp_path):
    """ON/OFF 호출예산 동일 — benchmark 는 이미 가져온 candle 재사용(추가 fetch 0)."""
    from src.journal.models import InMemoryJournalStore
    off_m = _CountingMarket(FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")}))
    on_m = _CountingMarket(FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")}))
    _mc_loop(tmp_path, off_m, InMemoryJournalStore(), enabled=False, name="off").tick()
    _mc_loop(tmp_path, on_m, InMemoryJournalStore(), enabled=True, name="on").tick()
    assert on_m.calls == off_m.calls and on_m.calls > 0


def test_market_context_failure_does_not_block_trading(tmp_path, monkeypatch):
    """builder 예외가 나도 사이클은 RAN, context 는 null (관측이 거래 게이팅 금지)."""
    from src.journal.models import InMemoryJournalStore
    def boom(*a, **k):
        raise RuntimeError("benchmark calc failed")
    monkeypatch.setattr("src.live.paper_loop.build_benchmark_context", boom)
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _mc_loop(tmp_path, market, journal, enabled=True).tick()
    assert rep.status == ST_RAN
    assert journal.get_cycle(rep.cycle_id).benchmark_context is None


def test_market_context_skipped_when_benchmark_not_in_universe(tmp_path):
    """benchmark 가 universe 에 없으면 관측 생략(추가 fetch 금지) — KRX benchmark 비활성과 동일 원리."""
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"SOXL": make_1m(130, REG)})
    journal = InMemoryJournalStore()
    rep = _mc_loop(tmp_path, market, journal, enabled=True,
                   universe=("SOXL",), benchmark="QQQ").tick()
    assert rep.status == ST_RAN
    assert journal.get_cycle(rep.cycle_id).benchmark_context is None


def test_market_context_does_not_change_pipeline_inputs(tmp_path):
    """ON/OFF 에서 파이프라인에 전달되는 매매 입력이 동일(계측 무영향)."""
    from src.journal.models import InMemoryJournalStore

    def run(enabled, name):
        market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
        pipe = RecordingPipeline()
        lp = PaperTradingLoop(
            market, CandleStore(tmp_path / f"{name}.sqlite"), pipe,
            InMemoryPositionStore(),
            LoopConfig(universe=("QQQ",),
                       market_context=MarketContextConfig(enabled=enabled, benchmark_symbol="QQQ")),
            journal=InMemoryJournalStore(), sleep=lambda s: None)
        lp.tick()
        return pipe.calls[0]

    on, off = run(True, "on"), run(False, "off")
    assert on["cycle_id"] == off["cycle_id"]
    assert on["universe"] == off["universe"]
    assert on["exit_intents"] == off["exit_intents"]
    assert on["system_state"].kill_switch == off["system_state"].kill_switch


# ---- Online Shadow Decision (PR 4-A) — 가상 결정 관측 배선 ----
from src.shadow.models import ShadowConfig     # noqa: E402


def _shadow_loop(tmp_path, market, journal, *, shadow_on, mc_on=True, name="s",
                 universe=("QQQ",), benchmark="QQQ"):
    return PaperTradingLoop(
        market, CandleStore(tmp_path / f"{name}.sqlite"), RecordingPipeline(),
        InMemoryPositionStore(),
        LoopConfig(universe=universe,
                   market_context=MarketContextConfig(enabled=mc_on, benchmark_symbol=benchmark),
                   shadow=ShadowConfig(enabled=shadow_on)),
        journal=journal, sleep=lambda s: None)


def test_shadow_journaled_when_enabled(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _shadow_loop(tmp_path, market, journal, shadow_on=True).tick()
    assert rep.status == ST_RAN
    sh = journal.get_cycle(rep.cycle_id).shadow
    assert sh is not None
    assert {p["policy_id"] for p in sh["policies"]} == {"A", "B"}
    assert sh["market_context_snapshot"] is not None    # benchmark_ctx 재사용(2-B)
    assert isinstance(sh["candidate_ids"], list)
    # 미래 outcome 필드는 런타임 레코드에 없다(4-B 오프라인 전용)
    assert "forward_return_30m" not in str(sh) and "avoided_loss" not in str(sh)


def test_shadow_absent_when_disabled(tmp_path):
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _shadow_loop(tmp_path, market, journal, shadow_on=False).tick()
    assert journal.get_cycle(rep.cycle_id).shadow is None


def test_shadow_records_no_context_when_market_context_off(tmp_path):
    """shadow 는 market_context 관측을 소비 → mc off 면 context None(would_allow=None)."""
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _shadow_loop(tmp_path, market, journal, shadow_on=True, mc_on=False).tick()
    sh = journal.get_cycle(rep.cycle_id).shadow
    assert sh is not None and sh["market_context_snapshot"] is None
    assert all(p["would_allow"] is None for p in sh["policies"])


def test_shadow_failure_does_not_block_trading(tmp_path, monkeypatch):
    """shadow builder 예외 → 사이클 RAN, shadow null(관측이 거래 게이팅 금지)."""
    from src.journal.models import InMemoryJournalStore
    def boom(*a, **k):
        raise RuntimeError("shadow calc failed")
    monkeypatch.setattr("src.live.paper_loop.build_shadow_record", boom)
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    rep = _shadow_loop(tmp_path, market, journal, shadow_on=True).tick()
    assert rep.status == ST_RAN
    assert journal.get_cycle(rep.cycle_id).shadow is None


def test_shadow_failure_is_logged(tmp_path, monkeypatch, caplog):
    """shadow 실패는 조용히 삼키되 진단용 debug 로그를 남긴다(관측 유실 추적)."""
    import logging
    from src.journal.models import InMemoryJournalStore
    def boom(*a, **k):
        raise RuntimeError("shadow calc failed")
    monkeypatch.setattr("src.live.paper_loop.build_shadow_record", boom)
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    with caplog.at_level(logging.DEBUG, logger="src.live.paper_loop"):
        _shadow_loop(tmp_path, market, InMemoryJournalStore(), shadow_on=True).tick()
    assert any("shadow decision skipped" in r.message for r in caplog.records)


class _CandidatePipeline:
    """진입 후보(candidate_id 부여)를 반환하는 가짜 파이프라인 — 조인 키 검증용."""
    def __init__(self, cand_ids):
        self._cids = cand_ids
        self.calls = []

    def run_once(self, **kw):
        self.calls.append(kw)
        outs = [SymbolOutcome(symbol=f"S{i}", action="SKIPPED", reason="X", candidate_id=cid)
                for i, cid in enumerate(self._cids)]
        return PipelineResult(kw["cycle_id"], outs)


def test_shadow_candidate_ids_populated_and_filtered(tmp_path):
    """candidate_ids(4-B 조인 키)가 실제 후보로 채워지고 결측(None)은 제외된다."""
    from src.journal.models import InMemoryJournalStore
    market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
    journal = InMemoryJournalStore()
    pipe = _CandidatePipeline(["cid1", None, "cid2"])   # None 은 제외돼야 함
    lp = PaperTradingLoop(
        market, CandleStore(tmp_path / "cand.sqlite"), pipe, InMemoryPositionStore(),
        LoopConfig(universe=("QQQ",),
                   market_context=MarketContextConfig(enabled=True, benchmark_symbol="QQQ"),
                   shadow=ShadowConfig(enabled=True)),
        journal=journal, sleep=lambda s: None)
    rep = lp.tick()
    sh = journal.get_cycle(rep.cycle_id).shadow
    assert sh["candidate_ids"] == ["cid1", "cid2"]


def test_shadow_does_not_change_pipeline_inputs(tmp_path):
    """shadow ON/OFF 에서 파이프라인 매매 입력 동일(run_once 이후 관측 — 구조 회귀 잠금)."""
    from src.journal.models import InMemoryJournalStore

    def run(shadow_on, name):
        market = FakeMarketDataProvider({"QQQ": make_1m(130, REG, symbol="QQQ")})
        pipe = RecordingPipeline()
        lp = PaperTradingLoop(
            market, CandleStore(tmp_path / f"{name}.sqlite"), pipe, InMemoryPositionStore(),
            LoopConfig(universe=("QQQ",),
                       market_context=MarketContextConfig(enabled=True, benchmark_symbol="QQQ"),
                       shadow=ShadowConfig(enabled=shadow_on)),
            journal=InMemoryJournalStore(), sleep=lambda s: None)
        lp.tick()
        return pipe.calls[0]

    on, off = run(True, "son"), run(False, "soff")
    assert on["cycle_id"] == off["cycle_id"]
    assert on["universe"] == off["universe"]
    assert on["exit_intents"] == off["exit_intents"]
    assert on["system_state"].kill_switch == off["system_state"].kill_switch
