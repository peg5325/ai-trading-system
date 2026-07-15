"""Phase 6 US baseline config + 착수 전 smoke (PR #40).

두 축:
1) config 파일(`config/runtime_us_phase6_baseline.yaml`)이 plan §Phase 6 baseline 정책을 정확히
   인코딩하는가 — Market Context+Shadow ON · Cooldown OFF · 전략 파라미터 불변 · fresh
   identity/namespace.
2) 그 설정으로 실 pipeline(FakeAI/PaperBroker)을 구동하면 journal 에 candidate/context/shadow 가
   기록되고, 주문 행동이 MC/shadow ON/OFF 에서 불변인가 — 시장시간 무관 오프라인 결정론.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from src.live.models import FakeMarketDataProvider, LoopConfig, ST_RAN
from src.live.paper_loop import PaperTradingLoop
from src.market_context.models import MarketContextConfig
from src.models import ET, Candle
from src.order.models import InMemoryPositionStore
from src.risk.models import EOD_FLATTEN
from src.runtime.config import load_yaml
from src.shadow.models import ShadowConfig
from src.storage.candle_store import CandleStore

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ROOT, "config", "runtime_us_phase6_baseline.yaml")
REG = datetime(2026, 7, 6, 10, 0, tzinfo=ET)   # 정규장 10:00 ET


def _make_1m(n, symbol, base=100.0, start=REG):
    out, prev = [], base
    for i in range(n):
        m = start + timedelta(minutes=i)
        c = base + (i % 10) * 0.1
        out.append(Candle.from_moment(symbol, "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000 + i, "USD", True))
        prev = c
    return out


# ── 1) config 파일 계약 ──────────────────────────────────────────────

def test_phase6_market_context_and_shadow_on():
    cfg = load_yaml(CONFIG_PATH)
    assert cfg.market_context.enabled is True
    assert cfg.market_context.benchmark_symbol == "QQQ"
    assert cfg.market_context.timeframe == "5m"
    assert cfg.market_context.ema_period == 20
    assert cfg.shadow.enabled is True


def test_phase6_cooldown_off():
    cfg = load_yaml(CONFIG_PATH)
    assert cfg.reentry_cooldown.enabled is False


def test_phase6_strategy_params_unchanged():
    cfg = load_yaml(CONFIG_PATH)
    ex = cfg.exit_config
    assert ex.stop_loss_pct == 0.01
    assert ex.take_profit_pct == 0.015
    assert ex.time_exit_minutes == 60
    assert ex.enable_all_symbols_eod_flatten is True
    assert ex.leverage_eod_flatten is True
    assert ex.eod_flatten_minutes_before_close == 10
    assert cfg.ai_provider == "fake"          # FakeAI(실AI 금지)


def test_phase6_fresh_identity():
    cfg = load_yaml(CONFIG_PATH)
    assert cfg.start_mode == "FRESH"
    assert cfg.market == "US"
    assert cfg.currency == "USD"
    assert cfg.session_id                      # 세션 식별자 설정됨


def test_phase6_fresh_namespace_and_benchmark_in_universe():
    cfg = load_yaml(CONFIG_PATH)
    dbs = (cfg.journal_db, cfg.candle_db, cfg.metrics_db, cfg.review_db, cfg.system_review_db)
    for db in dbs:
        assert "us_phase6" in db               # 전용 네임스페이스
    # 기존 US 세션 DB 와 겹치지 않아야 startup guard(fresh) 통과
    assert cfg.journal_db not in (
        "data/journal_us.sqlite", "data/journal_us_exit_test_2000usd.sqlite")
    assert "QQQ" in cfg.symbols                 # benchmark ∈ universe(추가 fetch 0)


# ── 2) 실 pipeline 통합 smoke ────────────────────────────────────────

def _real_pipeline():
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
    return pipe, ps


def _run_cycle(tmp_path, cfg, *, mc_on, shadow_on, name):
    from src.journal.models import InMemoryJournalStore
    pipe, ps = _real_pipeline()
    market = FakeMarketDataProvider({s: _make_1m(130, s) for s in cfg.symbols})
    journal = InMemoryJournalStore()
    loop_cfg = LoopConfig(
        universe=tuple(cfg.symbols), account_value=cfg.account_value,
        market_context=MarketContextConfig(
            enabled=mc_on, benchmark_symbol=cfg.market_context.benchmark_symbol,
            timeframe=cfg.market_context.timeframe, ema_period=cfg.market_context.ema_period),
        shadow=ShadowConfig(enabled=shadow_on))
    lp = PaperTradingLoop(market, CandleStore(tmp_path / f"{name}.sqlite"), pipe, ps,
                          loop_cfg, journal=journal, sleep=lambda s: None)
    return lp.tick(), journal


def _order_seq(journal, rep):
    cyc = journal.get_cycle(rep.cycle_id)
    return [(o.get("symbol"), o.get("action")) for o in cyc.outcomes]


def test_phase6_smoke_records_candidate_context_shadow(tmp_path):
    cfg = load_yaml(CONFIG_PATH)
    rep, journal = _run_cycle(tmp_path, cfg, mc_on=cfg.market_context.enabled,
                              shadow_on=cfg.shadow.enabled, name="on")
    assert rep.status == ST_RAN
    cyc = journal.get_cycle(rep.cycle_id)
    # candidate: 관찰 outcome 마다 candidate_id(공통 조인키)
    assert cyc.outcomes
    assert all(o.get("candidate_id") for o in cyc.outcomes)
    # context: benchmark 국면 기록(QQQ)
    assert cyc.benchmark_context is not None
    assert cyc.benchmark_context["benchmark_symbol"] == "QQQ"
    # shadow: policy A/B + context snapshot, 미래 outcome 미기록(4-B 오프라인 전용)
    assert cyc.shadow is not None
    assert {p["policy_id"] for p in cyc.shadow["policies"]} == {"A", "B"}
    assert cyc.shadow["market_context_snapshot"] is not None
    assert "avoided_loss" not in str(cyc.shadow)


def test_phase6_order_behavior_invariant_on_off(tmp_path):
    cfg = load_yaml(CONFIG_PATH)
    rep_on, j_on = _run_cycle(tmp_path, cfg, mc_on=True, shadow_on=True, name="on")
    rep_off, j_off = _run_cycle(tmp_path, cfg, mc_on=False, shadow_on=False, name="off")
    # 주문 행동(symbol, action) 시퀀스가 MC/shadow ON/OFF 에서 동일(관측 무영향)
    assert _order_seq(j_on, rep_on) == _order_seq(j_off, rep_off)
    # OFF 는 관측 미기록
    off_cyc = j_off.get_cycle(rep_off.cycle_id)
    assert off_cyc.benchmark_context is None
    assert off_cyc.shadow is None


class _RecordingPipeline:
    """run_once kwargs 를 기록하는 최소 스텁 — 청산 intent 경로 대조용."""
    def __init__(self):
        self.calls = []

    def run_once(self, **kw):
        from src.pipeline.models import PipelineResult
        self.calls.append(kw)
        return PipelineResult(kw["cycle_id"])


def test_phase6_exit_intents_invariant_on_off(tmp_path):
    """청산(SL/TP/TIME/EOD) 결정도 MC/shadow ON/OFF 에서 불변 — 자본 안전 핵심 경로.

    보유 포지션 + EOD flatten 창에서 실제 청산 intent 를 만들어 ON/OFF 를 대조한다
    (포지션 없는 진입-only 케이스의 공허한 []==[] 비교 보강)."""
    from src.pnl.models import FillEvent
    post_eod = datetime(2026, 7, 6, 19, 55, tzinfo=ET)   # 20:00 ET 애프터마켓 마감 5분 전(EOD flatten 창, POST)

    def run(mc_on, shadow_on, name):
        market = FakeMarketDataProvider({"SOXL": _make_1m(20, "SOXL", start=post_eod)})
        ps = InMemoryPositionStore()
        ps.apply_fill("SOXL", "BUY", 10, 194, 0)                 # 레버리지 보유
        pipe = _RecordingPipeline()
        loop_cfg = LoopConfig(
            universe=("SOXL",),
            market_context=MarketContextConfig(enabled=mc_on, benchmark_symbol="QQQ"),
            shadow=ShadowConfig(enabled=shadow_on))
        lp = PaperTradingLoop(market, CandleStore(tmp_path / f"{name}.sqlite"), pipe, ps,
                              loop_cfg, sleep=lambda s: None)
        lp.account.apply_fill(FillEvent("SOXL", "BUY", 10.0, 194.0))  # 청산 소스=account(FIFO)
        lp.tick()
        return pipe.calls[0]["exit_intents"]

    on, off = run(True, True, "ex_on"), run(False, False, "ex_off")
    assert on and on[0].kind == EOD_FLATTEN                       # 실제 청산 intent 존재(공허 비교 아님)
    assert [(e.symbol, e.kind) for e in on] == [(e.symbol, e.kind) for e in off]
