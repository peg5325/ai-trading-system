"""TradingPipeline — MarketData→Indicator→Screener→AI→Risk→Order(Paper) 연결자.

원칙:
- 각 컴포넌트를 주입받아 실행만 한다. 지표/AI/리스크 판단을 직접 구현하지 않는다.
- 실주문 금지: OrderManager 에 PaperBrokerProvider 를 넣어서 사용.
- AI 는 신규 진입 필터일 뿐. EXIT/EOD_FLATTEN 은 AI 를 거치지 않고 Risk→Order 로 실행.
- AI WAIT/REJECT 또는 Risk block 이면 주문하지 않는다(사유 기록).
- 동일 입력에 대해 결정론적(universe 정렬, client_order_id 결정론, 난수/시각 미사용).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from src.ai.engine import is_entry_permitted
from src.ai.models import DEC_APPROVE, DEC_WAIT
from src.pipeline.candidate_id import canonical_candidate_id
from src.pipeline.models import (
    ACTION_ORDERED,
    ACTION_SKIPPED,
    R_AI_REJECT,
    R_AI_WAIT,
    R_EXIT_BLOCKED,
    R_EXIT_ORDERED,
    R_NO_CANDIDATE,
    R_NO_DATA,
    R_ORDERED,
    R_RISK_BLOCK,
    PipelineResult,
    SymbolOutcome,
)
from src.models import MARKET_US
from src.order.models import ORD_FILLED
from src.risk.models import BUY, NEW_ENTRY, OrderIntent
from src.universe.screener import SymbolData, leverage_for

_SNAP_COLS = ["close", "ema7", "ema20", "rsi14", "vwap",
              "bb_upper", "bb_lower", "stochrsi_k", "stochrsi_d"]


def _trade_id(rec) -> str | None:
    """체결(FILLED) 시에만 trade_id 부여(§2-2). 미체결/거부는 null.
    dataset 의 trade_taken(=status FILLED) 판정과 일관되게 broker_order_id 를 쓴다."""
    if getattr(rec, "status", None) != ORD_FILLED:
        return None
    return getattr(rec, "broker_order_id", None) or rec.client_order_id


@dataclass(frozen=True)
class PipelineConfig:
    entry_notional_pct: float = 0.05
    # v2.1 계측(§2-2): candidate_id 해시 입력. 미설정(None)이면 빈 문자열로 canonicalize.
    session_id: str | None = None
    run_id: str | None = None


class TradingPipeline:
    def __init__(self, indicator_engine, screener, ai_engine, risk_manager,
                 order_manager, config: PipelineConfig | None = None,
                 leverage_by_symbol: dict[str, int] | None = None):
        self.engine = indicator_engine
        self.screener = screener
        self.ai = ai_engine
        self.risk = risk_manager
        self.orders = order_manager
        self.cfg = config or PipelineConfig()
        self._lev = leverage_by_symbol or {}

    def run_once(self, *, cycle_id: str, universe: list[str],
                 candles_by_symbol: dict, portfolio, system_state, market,
                 exit_intents: list[OrderIntent] | None = None) -> PipelineResult:
        result = PipelineResult(cycle_id)
        # 신규 진입 차단 상태를 리스크 스냅샷에 반영 (kill switch / reconciliation block)
        block_entries = (portfolio.kill_switch or system_state.kill_switch
                         or system_state.block_new_entries)
        risk_snap = replace(portfolio, kill_switch=block_entries)

        # 1) EXIT / EOD_FLATTEN 먼저 (AI 우회, Risk→Order)
        for intent in (exit_intents or []):
            decision = self.risk.evaluate(intent, risk_snap)   # 리스크 축소는 허용됨
            if decision.allowed and decision.adjusted_quantity > 0:
                coid = f"{intent.symbol}:{cycle_id}:{intent.kind}"
                rec = self.orders.submit(intent, decision, client_order_id=coid,
                                         ref_price=intent.price)
                result.outcomes.append(SymbolOutcome(
                    intent.symbol, ACTION_ORDERED, R_EXIT_ORDERED,
                    ai_decision=None, risk_decision=decision.to_dict(),
                    order_record=rec.to_dict()))
            else:
                result.outcomes.append(SymbolOutcome(
                    intent.symbol, ACTION_SKIPPED, R_EXIT_BLOCKED,
                    risk_decision=decision.to_dict()))

        # 2) 신규 진입 (Screener → AI → Risk → Order)
        for symbol in sorted(universe):
            result.outcomes.append(
                self._evaluate_entry(symbol, cycle_id, candles_by_symbol.get(symbol),
                                     portfolio, risk_snap, market))
        return result

    # ---- entry per symbol ----
    def _evaluate_entry(self, symbol, cycle_id, candles, portfolio, risk_snap, market_ctx):
        """market_ctx 는 MarketContext(세션/EOD 창) — AI 에 그대로 전달한다.
        종목의 시장코드('US'/'KR')는 캔들에서 따로 뽑아 Screener 에만 넘긴다(둘을 섞지 말 것)."""
        lev = self._lev.get(symbol) or leverage_for(symbol)
        if not candles:
            return SymbolOutcome(symbol, ACTION_SKIPPED, R_NO_DATA)

        ind = self.engine.compute(candles)
        if ind is None or len(ind) == 0:
            return SymbolOutcome(symbol, ACTION_SKIPPED, R_NO_DATA)

        snapshot = self._indicator_snapshot(ind)
        symbol_market = getattr(candles[-1], "market", MARKET_US)
        # candidate_id 는 관찰이 존재하면 후보 유무와 무관하게 항상 부여(공통 조인키).
        candidate_id = self._candidate_id(cycle_id, symbol, symbol_market)
        cands = self.screener.screen([SymbolData(symbol, ind, lev, market=symbol_market)])
        if not cands:
            return SymbolOutcome(symbol, ACTION_SKIPPED, R_NO_CANDIDATE,
                                 indicator_snapshot=snapshot, candidate_id=candidate_id)
        candidate = cands[0]
        cand_dict = candidate.to_dict(include_features=True)   # journal 직렬화엔 feature 포함
        #   ※ AI 는 candidate 객체를 받아 to_dict() 기본값(feature 제외)으로 해싱한다 →
        #     여기서 feature 를 붙여도 AI/risk/order 입력은 불변(계측이 매매를 바꾸지 않음).

        ref_price = float(ind["close"].iloc[-1])

        # 사이징 + 리스크(1회 평가: AI 컨텍스트 겸 최종 게이트)
        qty = (portfolio.account_value * self.cfg.entry_notional_pct) / ref_price if ref_price > 0 else 0.0
        intent = OrderIntent(symbol, NEW_ENTRY, BUY, qty, ref_price, lev,
                             risk_flags=list(candidate.risk_flags))
        risk_decision = self.risk.evaluate(intent, risk_snap)

        # AI entry filter — MarketContext(세션/EOD 창)를 전달
        ai = self.ai.decide(candidate, snapshot, portfolio, risk_decision, market_ctx)
        ai_log = getattr(self.ai, "last_log", None)
        ai_log = ai_log.to_dict() if ai_log is not None else None

        def outcome(action, reason):
            return SymbolOutcome(symbol, action, reason,
                                 ai_decision=ai.to_dict(), risk_decision=risk_decision.to_dict(),
                                 indicator_snapshot=snapshot, candidate=cand_dict, ai_log=ai_log,
                                 candidate_id=candidate_id)

        if ai.decision == DEC_WAIT:
            return outcome(ACTION_SKIPPED, R_AI_WAIT)
        if ai.decision != DEC_APPROVE:   # REJECT
            return outcome(ACTION_SKIPPED, R_AI_REJECT)

        # 최종 게이트: AI APPROVE 이고 Risk allow 여야 주문
        if not is_entry_permitted(ai, risk_decision):
            return outcome(ACTION_SKIPPED, R_RISK_BLOCK)

        coid = f"{symbol}:{cycle_id}"
        rec = self.orders.submit(intent, risk_decision, client_order_id=coid, ref_price=ref_price)
        o = outcome(ACTION_ORDERED, R_ORDERED)
        o.order_record = rec.to_dict()
        o.trade_id = _trade_id(rec)                # 체결 시에만(미체결/거부 null)
        return o

    def _candidate_id(self, cycle_id, symbol, market) -> str:
        return canonical_candidate_id(
            session_id=self.cfg.session_id, run_id=self.cfg.run_id,
            cycle_id=cycle_id, symbol=symbol, market=market)

    def _indicator_snapshot(self, ind) -> dict:
        row = ind.iloc[-1]
        out = {}
        for k in _SNAP_COLS:
            if k in ind.columns:
                v = row[k]
                out[k] = None if pd.isna(v) else float(v)
        return out
