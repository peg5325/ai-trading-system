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
from src.models import ET, KST, Candle, in_eod_flatten_window
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


def pv(entry=100.0, cur=100.0, lev=1, qty=10.0, entry_time=None, entry_bar=None,
       ema20=None, eod_win=False):
    return PositionView("SOXL", qty, entry, cur, lev, entry_time, entry_bar, ema20, eod_win)


def _candle(sym, ex_dt, currency, price=100.0):
    return Candle.from_moment(sym, "5m", ex_dt, price, price, price, price, 1000.0, currency, True)


def kr_candle(hh, mm, price=100.0):   # 거래소 로컬(KST) 시각으로 KR 캔들
    return _candle("005930", datetime(2026, 7, 8, hh, mm, tzinfo=KST), "KRW", price)


def us_candle(hh, mm, sym="QQQ", price=100.0):   # 거래소 로컬(ET) 시각으로 US 캔들
    return _candle(sym, datetime(2026, 7, 8, hh, mm, tzinfo=ET), "USD", price)


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


# ================= EOD flatten 창: market별 정규장 마감 기준 =================
def test_eod_window_kr_regular_close():
    # KR 정규장 마감 15:30 KST, 창 10분 → 15:20~15:30 발동, 15:10 미발동
    assert in_eod_flatten_window(kr_candle(15, 20), 10) is True   # 마감 10분 전
    assert in_eod_flatten_window(kr_candle(15, 25), 10) is True   # 마감 5분 전
    assert in_eod_flatten_window(kr_candle(15, 10), 10) is False  # 마감 20분 전 → 미발동


def test_eod_window_us_regular_close():
    # US 정규장 마감 16:00 ET, 창 10분 → 15:50~16:00 발동, 15:40 미발동
    assert in_eod_flatten_window(us_candle(15, 50), 10) is True
    assert in_eod_flatten_window(us_candle(15, 55), 10) is True
    assert in_eod_flatten_window(us_candle(15, 40), 10) is False


def test_eod_window_us_afterhours_compat():
    # 기존 US 애프터마켓(20:00 ET)/OVERNIGHT 정책 호환 유지
    assert in_eod_flatten_window(us_candle(19, 55), 10) is True   # POST, 20:00 ET 5분 전
    assert in_eod_flatten_window(us_candle(2, 0), 10) is True     # OVERNIGHT 상시


def test_eod_window_configurable_minutes():
    assert in_eod_flatten_window(kr_candle(15, 10), 20) is True   # N=20 이면 15:10도 창
    assert in_eod_flatten_window(kr_candle(15, 10), 5) is False


def test_eod_disabled_non_leverage_when_all_symbols_off():
    # enable_all_symbols_eod_flatten=false → 비레버리지는 마감창이어도 미발동
    g = ExitSignalGenerator(ExitConfig(enable_all_symbols_eod_flatten=False))
    assert g.evaluate(pv(lev=1, eod_win=True), CTX) is None


def test_eod_leverage_still_flattens_when_all_symbols_off():
    # leverage_eod_flatten=true 는 기존 동작 유지(레버리지는 마감창에서 발동)
    g = ExitSignalGenerator(ExitConfig(enable_all_symbols_eod_flatten=False, leverage_eod_flatten=True))
    assert g.evaluate(pv(lev=3, eod_win=True), CTX) == EOD_FLATTEN


def test_mixed_universe_per_symbol_eod(tmp_path):
    """혼합 유니버스: KR/US 각각 자기 시장 close 기준으로 EOD 판단(대표 캔들 하나로 판단 안 함)."""
    cfg = LoopConfig(universe=("005930", "QQQ"),
                     exit_config=ExitConfig(enable_all_symbols_eod_flatten=True))
    lp = PaperTradingLoop(FakeMarketDataProvider({}), CandleStore(tmp_path / "m.sqlite"),
                          None, InMemoryPositionStore(), cfg, sleep=lambda s: None)
    lp.account.apply_fill(FillEvent("005930", BUY, 10.0, 100.0))
    lp.account.apply_fill(FillEvent("QQQ", BUY, 10.0, 100.0))

    # (A) KR 마감창(15:25 KST) + US 마감창 밖(11:00 ET) → KR만 EOD
    kr_in, us_out = kr_candle(15, 25), us_candle(11, 0)
    kinds = {i.symbol: i.kind for i in lp._exit_intents({"005930": [kr_in], "QQQ": [us_out]}, kr_in)}
    assert kinds.get("005930") == EOD_FLATTEN and "QQQ" not in kinds

    # (B) 반대: US 마감창(15:55 ET) + KR 마감창 밖(11:00 KST) → US만 EOD
    kr_out, us_in = kr_candle(11, 0), us_candle(15, 55)
    kinds = {i.symbol: i.kind for i in lp._exit_intents({"005930": [kr_out], "QQQ": [us_in]}, us_in)}
    assert kinds.get("QQQ") == EOD_FLATTEN and "005930" not in kinds


def test_eod_timing_uses_each_symbols_latest_1m_without_cross_market_forcing(tmp_path):
    """#44 보완: 대표 KR 시계가 US 보유 종목의 EOD 창을 대신 결정하면 안 된다."""
    cfg = LoopConfig(universe=("005930", "QQQ"),
                     exit_config=ExitConfig(enable_all_symbols_eod_flatten=True))
    lp = PaperTradingLoop(FakeMarketDataProvider({}), CandleStore(tmp_path / "m.sqlite"),
                          None, InMemoryPositionStore(), cfg, sleep=lambda s: None)
    lp.account.apply_fill(FillEvent("005930", BUY, 10.0, 100.0))
    lp.account.apply_fill(FillEvent("QQQ", BUY, 10.0, 100.0))

    kr_closed = kr_candle(15, 15)
    us_closed = us_candle(10, 55)
    kr_latest_1m = Candle.from_moment(
        "005930", "1m", datetime(2026, 7, 8, 15, 24, tzinfo=KST),
        101.0, 101.0, 101.0, 101.0, 1.0, "KRW", True)
    us_latest_1m = Candle.from_moment(
        "QQQ", "1m", datetime(2026, 7, 8, 11, 0, tzinfo=ET),
        99.0, 99.0, 99.0, 99.0, 1.0, "USD", True)

    kinds = {
        intent.symbol: intent.kind
        for intent in lp._exit_intents(
            {"005930": [kr_closed], "QQQ": [us_closed]},
            kr_closed,
            timing_candles_1m={"005930": kr_latest_1m, "QQQ": us_latest_1m},
        )
    }

    assert kinds.get("005930") == EOD_FLATTEN
    assert "QQQ" not in kinds


def test_time_exit_bars_fires_in_paper_loop(tmp_path):
    """time_exit_bars 가 paper 경로에서 타임스탬프 유도 5분봉 인덱스로 실제 발동하는지.
    EOD 창과 무관하도록 US REGULAR 중간 시간대(11:00 ET~) 사용."""
    cfg = LoopConfig(universe=("SOXL",), exit_config=ExitConfig(time_exit_bars=12))
    lp = PaperTradingLoop(FakeMarketDataProvider({"SOXL": []}),
                          CandleStore(tmp_path / "m.sqlite"), None,
                          InMemoryPositionStore(), cfg, sleep=lambda s: None)
    t0 = datetime(2026, 7, 8, 15, 0, tzinfo=timezone.utc)   # 11:00 ET (REGULAR, 마감창 밖)
    lp.account.apply_fill(FillEvent("SOXL", BUY, 10.0, 100.0, timestamp=t0, bar_index=0))

    def bar_at(mins):
        return Candle.from_moment("SOXL", "5m", t0 + timedelta(minutes=mins),
                                  100.0, 100.0, 100.0, 100.0, 1000.0, "USD", True)

    # 12봉(60분) 경과 → TIME_EXIT 발동
    now = bar_at(60)
    intents = lp._exit_intents({"SOXL": [now]}, now)
    assert len(intents) == 1 and intents[0].kind == TIME_EXIT
    # 11봉(55분) 경과 → 미발동
    b55 = bar_at(55)
    assert lp._exit_intents({"SOXL": [b55]}, b55) == []


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


# ---- 완료 1분봉 기반 판정 (additive, issue #165) ----
def _bar(o, h, low, c):
    return Candle.from_moment("X", "1m", datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
                              o, h, low, c, 0.0, "USD", False)


_BAR_GEN = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.01, take_profit_pct=0.02,
                                          time_exit_minutes=60,
                                          enable_all_symbols_eod_flatten=True))
_BAR_POS = PositionView("X", 5, entry_price=100.0, current_price=100.0,
                        entry_time=datetime(2026, 10, 1, 13, 35, tzinfo=timezone.utc))


def test_intrabar_stop_first_when_both_touched():
    fill = _BAR_GEN.evaluate_intrabar(_BAR_POS, _bar(100.0, 103.0, 98.0, 100.0))
    assert (fill.reason, fill.reference_price) == (STOP_LOSS, 99.0)


def test_intrabar_gap_down_stop_uses_open_and_gap_up_target_uses_target():
    assert _BAR_GEN.evaluate_intrabar(_BAR_POS, _bar(97.0, 97.5, 96.0, 97.0)).reference_price == 97.0
    fill = _BAR_GEN.evaluate_intrabar(_BAR_POS, _bar(105.0, 106.0, 104.0, 105.0))
    assert (fill.reason, fill.reference_price) == (TAKE_PROFIT, 102.0)
    assert _BAR_GEN.evaluate_intrabar(_BAR_POS, _bar(100.0, 101.0, 99.5, 100.5)) is None


def test_bar_open_time_exit_counts_minutes_before_eod():
    t = datetime(2026, 10, 1, 14, 35, tzinfo=timezone.utc)
    fill = _BAR_GEN.evaluate_bar_open(_BAR_POS, ExitContext(is_eod_window=True, now_time=t),
                                      bar_open=100.5)
    assert (fill.reason, fill.reference_price) == (TIME_EXIT, 100.5)
    early = ExitContext(is_eod_window=True, now_time=t - timedelta(minutes=1))
    assert _BAR_GEN.evaluate_bar_open(_BAR_POS, early, bar_open=100.5).reason == EOD_FLATTEN
    assert _BAR_GEN.evaluate_bar_open(_BAR_POS, ExitContext(now_time=t - timedelta(minutes=1)),
                                      bar_open=100.5) is None
