"""Exit Signal Generator 테스트 (요구사항 #9).

커버: stop loss / take profit / time exit(bars·minutes) / EOD flatten(전종목·레버리지),
exit 우선순위, no-exit, 0%비활성, AI 우회, kill switch 중 exit 허용, realized_pnl 생성,
그리고 PaperTradingLoop end-to-end 로 STOP_LOSS closed trade 가 실제 생성되는지.
실주문 없음(PaperBrokerProvider), 실 Toss/OpenAI 미호출.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.ai.models import MarketContext
from src.backtest.execution import ExecutionModel
from src.engines.indicator_engine import IndicatorEngine
from src.exits.generator import ExitConfig, ExitContext, ExitSignalGenerator, PositionView
from src.live.models import FakeMarketDataProvider, LoopConfig
from src.live.paper_loop import PaperTradingLoop
from src.models import ET, Candle
from src.order.manager import OrderManager
from src.order.models import InMemoryOrderStore, InMemoryPositionStore
from src.order.paper_broker import PaperBrokerProvider
from src.pipeline.models import SystemState
from src.pipeline.pipeline import TradingPipeline
from src.pnl.fifo import FifoMatcher
from src.pnl.models import FillEvent
from src.risk.models import (
    BUY,
    EOD_FLATTEN,
    SELL,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_EXIT,
    TREND_BREAK,
    OrderIntent,
    Position,
    PortfolioSnapshot,
)
from src.risk.risk_manager import RiskManager
from src.storage.candle_store import CandleStore
from src.universe.screener import UniverseScreener


def pv(entry=100.0, cur=100.0, lev=1, qty=10.0, entry_time=None, entry_bar=None, ema20=None):
    return PositionView("SOXL", qty, entry, cur, lev, entry_time, entry_bar, ema20)


CTX = ExitContext(is_eod_window=False)
EOD_CTX = ExitContext(is_eod_window=True)


# ================= 개별 규칙 =================
def test_stop_loss_triggers():
    g = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.01))
    assert g.evaluate(pv(entry=100, cur=99.0), CTX) == STOP_LOSS      # -1% 도달
    assert g.evaluate(pv(entry=100, cur=99.5), CTX) is None           # -0.5% 미도달


def test_take_profit_triggers():
    g = ExitSignalGenerator(ExitConfig(take_profit_pct=0.015))
    assert g.evaluate(pv(entry=100, cur=101.5), CTX) == TAKE_PROFIT   # +1.5% 도달
    assert g.evaluate(pv(entry=100, cur=101.0), CTX) is None          # +1.0% 미도달


def test_time_exit_bars():
    g = ExitSignalGenerator(ExitConfig(time_exit_bars=12))
    ctx12 = ExitContext(now_bar=12)
    assert g.evaluate(pv(entry_bar=0), ctx12) == TIME_EXIT            # 12봉 경과
    assert g.evaluate(pv(entry_bar=1), ctx12) is None                 # 11봉 (미달)


def test_time_exit_minutes():
    g = ExitSignalGenerator(ExitConfig(time_exit_minutes=60))
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
    ctx = ExitContext(now_time=t0 + timedelta(minutes=60))
    assert g.evaluate(pv(entry_time=t0), ctx) == TIME_EXIT
    ctx2 = ExitContext(now_time=t0 + timedelta(minutes=59))
    assert g.evaluate(pv(entry_time=t0), ctx2) is None


def test_eod_flatten_all_symbols():
    g = ExitSignalGenerator(ExitConfig(enable_all_symbols_eod_flatten=True))
    assert g.evaluate(pv(lev=1), EOD_CTX) == EOD_FLATTEN             # 비레버리지도 청산
    assert g.evaluate(pv(lev=1), CTX) is None                       # 마감창 아니면 청산 안함


def test_eod_flatten_leverage_only_by_default():
    g = ExitSignalGenerator(ExitConfig())   # 기본: leverage_eod_flatten=True, all_symbols=False
    assert g.evaluate(pv(lev=3), EOD_CTX) == EOD_FLATTEN            # 레버리지는 청산
    assert g.evaluate(pv(lev=1), EOD_CTX) is None                  # 비레버리지는 홀드


def test_trend_break_optional():
    g = ExitSignalGenerator(ExitConfig(exit_on_trend_break=True))
    assert g.evaluate(pv(cur=99.0, ema20=100.0), CTX) == TREND_BREAK   # close < ema20
    assert g.evaluate(pv(cur=101.0, ema20=100.0), CTX) is None
    # ema20 NaN 은 무시
    assert g.evaluate(pv(cur=99.0, ema20=float("nan")), CTX) is None


# ================= 우선순위 & no-exit =================
def test_priority_sl_over_tp_over_time_over_eod():
    # SL/TP/Time/EOD 를 모두 발동 가능하게 설정하고 우선순위 확인
    cfg = ExitConfig(stop_loss_pct=0.01, take_profit_pct=0.01,
                     time_exit_bars=1, enable_all_symbols_eod_flatten=True)
    g = ExitSignalGenerator(cfg)
    ctx = ExitContext(is_eod_window=True, now_bar=5)
    # 가격이 -1% 이하 → SL 최우선(동시에 time/eod 도 성립)
    assert g.evaluate(pv(entry=100, cur=98.0, entry_bar=0), ctx) == STOP_LOSS
    # SL 미발동, +1% 이상 → TP (time/eod 보다 우선)
    assert g.evaluate(pv(entry=100, cur=102.0, entry_bar=0), ctx) == TAKE_PROFIT
    # SL/TP 미발동, time 성립 → TIME_EXIT (eod 보다 우선)
    assert g.evaluate(pv(entry=100, cur=100.0, entry_bar=0), ctx) == TIME_EXIT
    # SL/TP/time 미발동, 마감창 → EOD
    ctx_no_time = ExitContext(is_eod_window=True, now_bar=0)
    assert g.evaluate(pv(entry=100, cur=100.0, entry_bar=0), ctx_no_time) == EOD_FLATTEN


def test_no_exit_when_within_bounds():
    g = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.02, take_profit_pct=0.02))
    assert g.evaluate(pv(entry=100, cur=100.5), CTX) is None
    assert g.evaluate(pv(entry=100, cur=100.5), EOD_CTX) is None   # 비레버리지 마감창도 no-exit


def test_zero_pct_disables_rules():
    g = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.0, take_profit_pct=0.0))
    assert g.evaluate(pv(entry=100, cur=1.0), CTX) is None         # 0% 는 비활성
    assert g.evaluate(pv(entry=100, cur=999.0), CTX) is None


def test_no_exit_when_no_position():
    g = ExitSignalGenerator(ExitConfig(enable_all_symbols_eod_flatten=True))
    assert g.evaluate(pv(qty=0.0), EOD_CTX) is None


# ================= generate_intents =================
def test_generate_intents_kind_side_qty():
    g = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.01))
    views = [pv(entry=100, cur=98.0, qty=7.0)]
    intents = g.generate_intents(views, CTX)
    assert len(intents) == 1
    it = intents[0]
    assert it.kind == STOP_LOSS and it.side == SELL and it.quantity == 7.0
    assert STOP_LOSS in it.risk_flags


# ================= RiskManager: 새 청산 kind 는 risk-reducing =================
def _held_snapshot(qty=10.0, kill=False):
    return PortfolioSnapshot(
        account_value=100_000.0,
        positions={"SOXL": Position("SOXL", qty, 100.0, 98.0, 3)},
        kill_switch=kill, session="REGULAR")


def test_risk_allows_new_exit_kinds():
    rm = RiskManager()
    for kind in (STOP_LOSS, TAKE_PROFIT, TIME_EXIT, TREND_BREAK):
        d = rm.evaluate(OrderIntent("SOXL", kind, SELL, 10.0, 98.0, 3), _held_snapshot())
        assert d.allowed and d.adjusted_quantity == 10.0, kind


def test_risk_allows_exit_under_kill_switch():
    rm = RiskManager()
    d = rm.evaluate(OrderIntent("SOXL", STOP_LOSS, SELL, 10.0, 98.0, 3),
                    _held_snapshot(kill=True))
    assert d.allowed and d.adjusted_quantity == 10.0   # kill switch 여도 청산 허용


# ================= FIFO: exit_reason 전파 + realized_pnl =================
def test_exit_reason_and_realized_pnl_via_fifo():
    m = FifoMatcher(bar_minutes=5.0)
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
    m.ingest(FillEvent("SOXL", BUY, 10.0, 100.0, ref_price=100.0, timestamp=t0, bar_index=0))
    trades = m.ingest(FillEvent("SOXL", SELL, 10.0, 110.0, ref_price=110.0,
                                timestamp=t0 + timedelta(minutes=30), bar_index=6,
                                reason=(TAKE_PROFIT,)))
    assert len(trades) == 1
    tr = trades[0]
    assert tr.exit_reason == (TAKE_PROFIT,)
    assert tr.realized_pnl == 100.0            # (110-100)*10
    assert tr.holding_minutes == 30.0 and tr.holding_bars == 6


# ================= 파이프라인: exit 는 AI 우회, kill switch 에도 실행 =================
class SpyAI:
    def __init__(self):
        self.calls = 0

    def decide(self, *a, **k):
        self.calls += 1
        raise AssertionError("AI must not be consulted for exits")

    last_log = None


def test_exit_bypasses_ai_and_runs_under_kill_switch():
    ps = InMemoryPositionStore()
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)), InMemoryOrderStore(), ps)
    spy = SpyAI()
    pipe = TradingPipeline(IndicatorEngine(), UniverseScreener(), spy, RiskManager(), om)
    snap = _held_snapshot(kill=True)
    exit_intent = OrderIntent("SOXL", STOP_LOSS, SELL, 10.0, 98.0, 3)
    result = pipe.run_once(
        cycle_id="c1", universe=[], candles_by_symbol={}, portfolio=snap,
        system_state=SystemState(kill_switch=True),
        market=MarketContext(session="REGULAR", is_eod_flatten_window=False),
        exit_intents=[exit_intent])
    assert spy.calls == 0                                   # AI 미호출(청산은 AI 우회)
    recs = [o.order_record for o in result.outcomes if o.order_record]
    assert recs and recs[0]["status"] == "FILLED"
    assert recs[0]["kind"] == STOP_LOSS and recs[0]["side"] == SELL


# ================= End-to-End 루프: STOP_LOSS closed trade 생성 =================
def _flat_1m(n, start_et, price):
    out = []
    for i in range(n):
        m = start_et + timedelta(minutes=i)
        out.append(Candle.from_moment("SOXL", "1m", m, price, price + 0.1,
                                      price - 0.1, price, 1000 + i, "USD", True))
    return out


def test_time_exit_bars_fires_in_paper_loop(tmp_path):
    """time_exit_bars 가 paper 경로에서 타임스탬프 유도 5분봉 인덱스로 실제 발동하는지."""
    cfg = LoopConfig(universe=("SOXL",), exit_config=ExitConfig(time_exit_bars=12))
    lp = PaperTradingLoop(FakeMarketDataProvider({"SOXL": []}),
                          CandleStore(tmp_path / "m.sqlite"), None,
                          InMemoryPositionStore(), cfg, sleep=lambda s: None)
    t0 = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
    lp.account.apply_fill(FillEvent("SOXL", BUY, 10.0, 100.0, timestamp=t0, bar_index=0))

    def bar_at(mins):
        return Candle.from_moment("SOXL", "5m", t0 + timedelta(minutes=mins),
                                  100.0, 100.0, 100.0, 100.0, 1000.0, "USD", True)

    # 12봉(60분) 경과 → TIME_EXIT 발동
    intents = lp._exit_intents({"SOXL": 100.0}, bar_at(60), eod=False)
    assert len(intents) == 1 and intents[0].kind == TIME_EXIT
    # 11봉(55분) 경과 → 미발동
    assert lp._exit_intents({"SOXL": 100.0}, bar_at(55), eod=False) == []


def test_stop_loss_closed_trade_end_to_end(tmp_path):
    REG = datetime(2026, 7, 6, 10, 0, tzinfo=ET)
    # 가격이 진입가(100) 대비 폭락(50) → -5% 손절 발동
    market = FakeMarketDataProvider({"SOXL": _flat_1m(20, REG, 50.0)})
    ps = InMemoryPositionStore()
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)), InMemoryOrderStore(), ps)
    from src.ai.engine import AIConfig, AIDecisionEngine
    from src.ai.models import DEC_APPROVE
    from src.ai.providers import FakeAIProvider
    ai = AIDecisionEngine(FakeAIProvider(response={
        "decision": DEC_APPROVE, "confidence_score": 70, "entry_quality": "GOOD",
        "risk_level": "HIGH", "reason": ["x"], "warnings": []}), AIConfig())
    pipe = TradingPipeline(IndicatorEngine(), UniverseScreener(), ai, RiskManager(), om)
    cfg = LoopConfig(universe=("SOXL",), exit_config=ExitConfig(stop_loss_pct=0.05))
    lp = PaperTradingLoop(market, CandleStore(tmp_path / "m.sqlite"), pipe, ps, cfg,
                          sleep=lambda s: None)
    # 계좌에 기존 보유 포지션 시드(진입가 100)
    t0 = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
    lp.account.apply_fill(FillEvent("SOXL", BUY, 10.0, 100.0, ref_price=100.0,
                                    timestamp=t0, bar_index=0))
    rep = lp.tick()
    assert rep.status == "RAN"
    trades = lp.account.realized_trades()
    assert len(trades) == 1                                 # 청산되어 closed trade 생성
    assert trades[0].exit_reason == (STOP_LOSS,)
    assert trades[0].realized_pnl < 0                       # 진입가보다 낮게 매도(-손실)
    assert "SOXL" not in lp.account.positions()             # 전량 청산됨
