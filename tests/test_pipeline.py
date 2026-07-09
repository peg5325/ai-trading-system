"""TradingPipeline 테스트: 후보 없음, AI WAIT, AI APPROVE+Risk allow/block,
EOD flatten, kill switch EXIT 허용, 주문 결과 기록, 결정론."""
from __future__ import annotations

import pandas as pd

from src.ai.engine import AIConfig, AIDecisionEngine
from src.ai.models import DEC_APPROVE, DEC_REJECT, MarketContext, AITimeoutError
from src.ai.providers import FakeAIProvider
from src.backtest.execution import ExecutionModel
from src.order.manager import OrderManager
from src.order.models import InMemoryOrderStore, InMemoryPositionStore
from src.order.paper_broker import PaperBrokerProvider
from src.pipeline.models import (
    ACTION_ORDERED,
    R_AI_WAIT,
    R_EXIT_ORDERED,
    R_NO_CANDIDATE,
    R_ORDERED,
    R_RISK_BLOCK,
    SystemState,
)
from src.pipeline.pipeline import TradingPipeline
from src.risk.models import BUY, EOD_FLATTEN, EXIT, SELL, OrderIntent, PortfolioSnapshot, Position
from src.risk.risk_manager import RiskManager
from src.universe.screener import CANDIDATE_LONG, Candidate

MARKET = MarketContext("REGULAR")


def stub_df(close=194.0):
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-06T14:00:00Z")], name="ts_utc")
    return pd.DataFrame({
        "close": [close], "ema7": [193.0], "ema20": [192.0], "rsi14": [58.0],
        "vwap": [193.5], "bb_upper": [196.0], "bb_lower": [190.0],
        "stochrsi_k": [70.0], "stochrsi_d": [60.0],
    }, index=idx)


class StubEngine:
    def __init__(self, df=None):
        self._df = df if df is not None else stub_df()

    def compute(self, candles):
        return self._df


class StubScreener:
    def __init__(self, candidates=True):
        self._on = candidates

    def screen(self, inputs):
        if not self._on:
            return []
        sym = inputs[0].symbol
        return [Candidate(sym, 82.5, 1, CANDIDATE_LONG,
                          ["price above EMA20", "price near VWAP"], ["LEVERAGED_ETF"], {})]


def approve_resp(conf=84):
    return {"decision": DEC_APPROVE, "confidence_score": conf, "entry_quality": "GOOD",
            "risk_level": "MEDIUM", "reason": ["trend recovered"], "warnings": ["3x ETF"]}


def build(*, candidates=True, ai_provider=None, order_manager=None):
    ai = AIDecisionEngine(ai_provider or FakeAIProvider(response=approve_resp()), AIConfig())
    om = order_manager or OrderManager(
        PaperBrokerProvider(ExecutionModel(0, 0)), InMemoryOrderStore(), InMemoryPositionStore())
    return TradingPipeline(StubEngine(), StubScreener(candidates), ai, RiskManager(), om)


def run(pipe, *, universe=("SOXL",), system_state=None, portfolio=None, exit_intents=None):
    return pipe.run_once(
        cycle_id="20260706T1400", universe=list(universe),
        candles_by_symbol={s: [1] for s in universe},
        portfolio=portfolio or PortfolioSnapshot(account_value=100_000),
        system_state=system_state or SystemState(), market=MARKET,
        exit_intents=exit_intents)


# ---- 후보 없음 ----
def test_no_candidate():
    res = run(build(candidates=False))
    o = res.outcomes[0]
    assert o.reason == R_NO_CANDIDATE and o.order_record is None
    assert res.orders() == []


# ---- AI WAIT → 주문 없음 ----
def test_ai_wait_no_order():
    res = run(build(ai_provider=FakeAIProvider(raise_exc=AITimeoutError("t"))))
    o = res.outcomes[0]
    assert o.reason == R_AI_WAIT and o.order_record is None
    assert o.ai_decision["decision"] == "WAIT"


# ---- AI REJECT → 주문 없음 ----
def test_ai_reject_no_order():
    resp = approve_resp()
    resp["decision"] = DEC_REJECT
    res = run(build(ai_provider=FakeAIProvider(response=resp)))
    assert res.orders() == []


# ---- AI APPROVE + Risk allowed → 주문 + 연결 기록 ----
def test_approve_and_allowed_orders_and_links():
    res = run(build())
    o = res.outcomes[0]
    assert o.action == ACTION_ORDERED and o.reason == R_ORDERED
    assert o.order_record["status"] == "FILLED"
    assert o.ai_decision["decision"] == DEC_APPROVE      # AI 연결
    assert o.risk_decision["allowed"] is True            # Risk 연결
    assert len(res.orders()) == 1


# ---- AI APPROVE + Risk block(kill switch) → 주문 없음 ----
def test_approve_but_risk_block():
    res = run(build(), system_state=SystemState(kill_switch=True))
    o = res.outcomes[0]
    assert o.reason == R_RISK_BLOCK and o.order_record is None
    assert o.ai_decision["decision"] == DEC_APPROVE      # AI 는 APPROVE
    assert o.risk_decision["allowed"] is False           # 그러나 Risk block


# ---- reconciliation block_new_entries 도 진입 차단 ----
def test_reconciliation_block_new_entries():
    res = run(build(), system_state=SystemState(block_new_entries=True))
    assert res.outcomes[0].reason == R_RISK_BLOCK


# ---- EOD flatten: AI 우회, Risk→Order 실행 ----
def test_eod_flatten_bypasses_ai():
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)),
                      InMemoryOrderStore(), InMemoryPositionStore())
    om._positions.apply_fill("SOXL", BUY, 10, 194, 0)   # 보유 상태
    pipe = build(candidates=False, order_manager=om)     # 신규 후보 없음
    held = PortfolioSnapshot(account_value=100_000,
                             positions={"SOXL": Position("SOXL", 10, 194, 193, 3)})
    eod = OrderIntent("SOXL", EOD_FLATTEN, SELL, 10, 193.0, 3)
    res = run(pipe, portfolio=held, exit_intents=[eod])
    ex = [o for o in res.outcomes if o.reason == R_EXIT_ORDERED]
    assert ex and ex[0].ai_decision is None              # AI 미개입
    assert ex[0].order_record["kind"] == EOD_FLATTEN
    assert om._positions.get("SOXL") is None             # 청산됨


# ---- kill switch 하에서도 EXIT 실행, 신규 진입은 차단 ----
def test_kill_switch_exit_allowed_entry_blocked():
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)),
                      InMemoryOrderStore(), InMemoryPositionStore())
    om._positions.apply_fill("SOXL", BUY, 10, 194, 0)
    pipe = build(order_manager=om)                        # 후보 있음
    held = PortfolioSnapshot(account_value=100_000,
                             positions={"SOXL": Position("SOXL", 10, 194, 190, 3)})
    exit_intent = OrderIntent("SOXL", EXIT, SELL, 10, 190.0, 3)
    res = run(pipe, portfolio=held, system_state=SystemState(kill_switch=True),
              exit_intents=[exit_intent])
    exits = [o for o in res.outcomes if o.reason == R_EXIT_ORDERED]
    entries = [o for o in res.outcomes if o.reason == R_RISK_BLOCK]
    assert exits and exits[0].order_record["status"] == "FILLED"   # EXIT 실행
    assert entries                                                  # 신규는 차단


# ---- 결과 기록(to_dict) ----
def test_result_to_dict_links_order():
    res = run(build())
    d = res.to_dict()
    assert d["cycle_id"] == "20260706T1400"
    assert d["order_count"] == 1
    assert d["outcomes"][0]["order_record"]["client_order_id"] == "SOXL:20260706T1400"


# ---- 결정론: 동일 입력 → 동일 결정 (감사 메타 latency/cache 제외) ----
def _decision_view(res):
    keys = ("symbol", "action", "reason", "ai_decision", "risk_decision", "order_record")
    return [{k: o[k] for k in keys} for o in res.to_dict()["outcomes"]]


def test_deterministic():
    pipe = build()
    a = run(pipe)
    b = run(pipe)     # 동일 client_order_id → 멱등, AI 캐시
    assert _decision_view(a) == _decision_view(b)   # 결정은 동일 (ai_log latency/cached 는 관측 메타)


# ---- 다종목 정렬(결정론적 순서) ----
def test_multi_symbol_sorted_order():
    res = run(build(), universe=("TQQQ", "SOXL"))
    syms = [o.symbol for o in res.outcomes]
    assert syms == sorted(syms)


# ---- MarketContext 전파: AI 는 MarketContext, Screener 는 시장코드 문자열 ----
class _StubCandle:
    """market 속성을 가진 최소 캔들 (기존 테스트는 int 를 캔들로 써서 이 경로를 못 밟았다)."""
    def __init__(self, market):
        self.market = market


class _SpyAI:
    """decide() 에 전달된 market 인자를 그대로 포착하는 래퍼."""
    def __init__(self, inner):
        self._inner = inner
        self.seen_market = None

    def decide(self, candidate, indicators, portfolio, risk_decision, market):
        self.seen_market = market
        return self._inner.decide(candidate, indicators, portfolio, risk_decision, market)

    @property
    def last_log(self):
        return getattr(self._inner, "last_log", None)


class _SpyScreener(StubScreener):
    """SymbolData.market 으로 넘어온 값을 포착."""
    def __init__(self):
        super().__init__(True)
        self.seen_market = None

    def screen(self, inputs):
        self.seen_market = inputs[0].market
        return super().screen(inputs)


def _build_spied(screener=None, ai_provider=None):
    inner = AIDecisionEngine(ai_provider or FakeAIProvider(response=approve_resp()), AIConfig())
    spy_ai = _SpyAI(inner)
    om = OrderManager(PaperBrokerProvider(ExecutionModel(0, 0)),
                      InMemoryOrderStore(), InMemoryPositionStore())
    pipe = TradingPipeline(StubEngine(), screener or StubScreener(), spy_ai, RiskManager(), om)
    return pipe, spy_ai


def _run_with(pipe, market, candles, cycle_id="20260706T1400"):
    return pipe.run_once(
        cycle_id=cycle_id, universe=["SOXL"], candles_by_symbol={"SOXL": candles},
        portfolio=PortfolioSnapshot(account_value=100_000),
        system_state=SystemState(), market=market)


def test_ai_receives_market_context_not_market_code():
    """회귀: _evaluate_entry 가 market 파라미터를 시장코드 문자열로 덮어쓰면 안 된다."""
    pipe, spy_ai = _build_spied()
    ctx = MarketContext("POST", is_eod_flatten_window=True)
    _run_with(pipe, ctx, [_StubCandle("US")])

    assert isinstance(spy_ai.seen_market, MarketContext), \
        f"AI 는 MarketContext 를 받아야 한다 (실제: {spy_ai.seen_market!r})"
    assert spy_ai.seen_market.session == "POST"
    assert spy_ai.seen_market.is_eod_flatten_window is True


def test_screener_still_receives_market_code_string():
    """Screener 는 캔들에서 얻은 시장코드('US'/'KR')를 계속 받아야 한다."""
    spy_screener = _SpyScreener()
    pipe, spy_ai = _build_spied(screener=spy_screener)
    _run_with(pipe, MarketContext("REGULAR"), [_StubCandle("KR")])

    assert spy_screener.seen_market == "KR"                  # 시장코드
    assert isinstance(spy_ai.seen_market, MarketContext)     # AI 는 컨텍스트


def test_ai_cache_does_not_collide_across_sessions():
    """세션이 다르면 AI 캐시 키도 달라야 한다 (market_context 가 해시에 들어가므로).

    버그 상태에서는 market 이 'US' 문자열로 고정돼 두 세션의 input_hash 가 같아지고,
    두 번째 호출이 첫 번째의 캐시된 결정을 재사용한다.
    """
    provider = FakeAIProvider(response=approve_resp())   # .calls 카운터 내장
    pipe, _ = _build_spied(ai_provider=provider)
    candles = [_StubCandle("US")]

    _run_with(pipe, MarketContext("REGULAR"), candles, cycle_id="c1")
    assert provider.calls == 1
    _run_with(pipe, MarketContext("POST"), candles, cycle_id="c2")

    assert provider.calls == 2, \
        f"세션별로 AI 를 각각 호출해야 한다 (실제 호출 {provider.calls}회 — 캐시 충돌)"
