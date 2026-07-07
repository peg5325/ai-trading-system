"""RiskManager — 최종 주문 가능 여부 결정자.

AI / Rule Engine / UniverseScreener 가 어떤 신호를 내더라도, 최종 allow/block 은 여기서.
- 명목이 아니라 실질 노출(effective = notional * leverage_factor) 기준으로 관리.
- 신규/추가 진입, 청산, EOD flatten 을 구분해 평가.
- AI 는 entry filter 일 뿐. 손절/청산/EOD flatten 은 kill switch·손실한도와 무관하게 허용.
- 결정론적: 현재 시각이 아니라 snapshot 필드(session, is_eod_flatten_window)에 의존.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import SESSION_OVERNIGHT, SESSION_POST, SESSION_PRE
from src.risk.models import (
    ADD,
    ENTRY_KINDS,
    EOD_FLATTEN,
    NEW_ENTRY,
    RISK_REDUCING,
    OrderIntent,
    PortfolioSnapshot,
    RiskDecision,
)

# ---- reason codes ----
RC_INVALID_INTENT = "INVALID_INTENT"
RC_INVALID_SNAPSHOT = "INVALID_SNAPSHOT"
RC_KILL_SWITCH = "KILL_SWITCH_ACTIVE"
RC_DAILY_LOSS = "DAILY_LOSS_LIMIT"
RC_DRAWDOWN = "MAX_DRAWDOWN_EXCEEDED"
RC_CONSEC_LOSS = "CONSECUTIVE_LOSS_LIMIT"
RC_NEAR_CLOSE_NO_ENTRY = "NEAR_CLOSE_NO_NEW_ENTRY"
RC_EXT_HOURS_RISK = "EXTENDED_HOURS_RISK"
RC_POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
RC_NO_EXISTING_POSITION = "NO_EXISTING_POSITION"
RC_MAX_SCALE_INS = "MAX_SCALE_INS_EXCEEDED"
RC_MAX_SYMBOL_NOTIONAL = "MAX_SYMBOL_NOTIONAL_EXCEEDED"
RC_MAX_SYMBOL_EFFECTIVE = "MAX_SYMBOL_EFFECTIVE_EXPOSURE_EXCEEDED"
RC_LEVERAGED_ETF_LIMIT = "LEVERAGED_ETF_LIMIT"
RC_MAX_PORTFOLIO_EFFECTIVE = "MAX_EFFECTIVE_EXPOSURE_EXCEEDED"
RC_QTY_REDUCED = "QUANTITY_REDUCED_TO_LIMIT"
RC_RISK_REDUCING_ALLOWED = "RISK_REDUCING_ALLOWED"
RC_NOTHING_TO_REDUCE = "NOTHING_TO_REDUCE"
RC_MAX_POSITIONS = "MAX_CONCURRENT_POSITIONS"
RC_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"

# 상위(Screener 등)에서 넘어오는 리스크 플래그
FLAG_LEVERAGED = "LEVERAGED_ETF"
FLAG_EXT_HOURS = "EXTENDED_HOURS_SPREAD"
FLAG_LOW_LIQUIDITY = "LOW_LIQUIDITY"

_EXT_SESSIONS = frozenset({SESSION_PRE, SESSION_POST, SESSION_OVERNIGHT})
_EPS = 1e-9


@dataclass(frozen=True)
class RiskConfig:
    max_symbol_notional_pct: float = 0.20       # 단일 종목 최대 명목 노출(계좌 대비)
    max_symbol_effective_pct: float = 0.30      # 단일 종목 최대 실질 노출
    max_portfolio_effective_pct: float = 0.50   # 전체 최대 실질 노출
    daily_loss_limit_pct: float = 0.03          # 일일 손실 한도(3%)
    max_drawdown_pct: float = 0.10              # 최대 낙폭(10%)
    max_consecutive_losses: int = 3
    max_scale_ins: int = 2                      # 최초 진입 후 추가 진입 허용 횟수
    block_extended_hours_risky: bool = True     # 연장시간 리스크 주문 차단(False=감량)
    extended_hours_qty_factor: float = 0.5      # 감량 모드일 때 수량 배수
    max_positions: int = 2                      # 동시 보유 가능한 최대 종목 수(신규 진입 기준)


class RiskManager:
    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig()

    def evaluate(self, intent: OrderIntent, snapshot: PortfolioSnapshot) -> RiskDecision:
        flags = self._risk_flags(intent)

        if intent.quantity <= 0 or intent.price <= 0:
            return RiskDecision(False, [RC_INVALID_INTENT], 0.0, flags)

        # 리스크 축소 주문(청산/손절/EOD flatten)은 항상 허용 (AI/kill switch/손실한도 무관)
        if intent.kind in RISK_REDUCING:
            return self._evaluate_risk_reducing(intent, snapshot, flags)

        if intent.kind not in ENTRY_KINDS:
            return RiskDecision(False, [RC_INVALID_INTENT], 0.0, flags)

        return self._evaluate_entry(intent, snapshot, flags)

    # ---- helpers ----
    def _risk_flags(self, intent: OrderIntent) -> list[str]:
        flags = list(intent.risk_flags)
        if intent.leverage_factor > 1 and FLAG_LEVERAGED not in flags:
            flags.append(FLAG_LEVERAGED)
        return flags

    def _evaluate_risk_reducing(self, intent, snapshot, flags) -> RiskDecision:
        pos = snapshot.positions.get(intent.symbol)
        held = abs(pos.quantity) if pos else 0.0
        if held <= 0:
            # 줄일 포지션이 없음 — 허용하되 실행 수량 0
            return RiskDecision(True, [RC_NOTHING_TO_REDUCE], 0.0, flags)
        qty = min(intent.quantity, held)
        return RiskDecision(True, [RC_RISK_REDUCING_ALLOWED], round(qty, 6), flags)

    def _evaluate_entry(self, intent, snapshot, flags) -> RiskDecision:
        if snapshot.account_value <= 0:
            return RiskDecision(False, [RC_INVALID_SNAPSHOT], 0.0, flags)

        pos = snapshot.positions.get(intent.symbol)
        blocks: list[str] = []

        # 계좌 상태 기반 하드 차단 (신규/추가 진입)
        if snapshot.kill_switch:
            blocks.append(RC_KILL_SWITCH)
        if snapshot.daily_pnl_pct <= -self.cfg.daily_loss_limit_pct:
            blocks.append(RC_DAILY_LOSS)
        if abs(snapshot.drawdown_pct) >= self.cfg.max_drawdown_pct:
            blocks.append(RC_DRAWDOWN)
        if snapshot.consecutive_losses >= self.cfg.max_consecutive_losses:
            blocks.append(RC_CONSEC_LOSS)

        # 레버리지 ETF 는 마감 청산 대상 → flatten 창에서 신규/추가 진입 금지
        if snapshot.is_eod_flatten_window and intent.leverage_factor > 1:
            blocks.append(RC_NEAR_CLOSE_NO_ENTRY)

        # 연장시간 유동성/스프레드 리스크 — 종목별 플래그 기준(포트폴리오 대표 세션 아님).
        # 혼합 유니버스에서 US 심야 종목이 KR 발 REGULAR 세션에 묻혀 통과되던 문제 수정.
        risky = any(f in flags for f in (FLAG_EXT_HOURS, FLAG_LOW_LIQUIDITY))
        ext_reduce = False
        if risky:
            if self.cfg.block_extended_hours_risky:
                blocks.append(RC_EXT_HOURS_RISK)
            else:
                ext_reduce = True

        # 주문 종류 정합성
        has_position = pos is not None and abs(pos.quantity) > 0
        if intent.kind == NEW_ENTRY and has_position:
            blocks.append(RC_POSITION_ALREADY_OPEN)
        if intent.kind == ADD:
            if not has_position:
                blocks.append(RC_NO_EXISTING_POSITION)
            elif pos.scale_in_count >= self.cfg.max_scale_ins + 1:
                blocks.append(RC_MAX_SCALE_INS)

        # 자금관리: 동시 보유 종목 수 한도 (신규 진입만 대상, 기존 보유 종목의 추가진입은 무관)
        open_count = sum(1 for p in snapshot.positions.values() if abs(p.quantity) > 0)
        if intent.kind == NEW_ENTRY and not has_position and open_count >= self.cfg.max_positions:
            blocks.append(RC_MAX_POSITIONS)

        # 자금관리: 가용 현금 부족(명목 기준)
        if snapshot.cash is not None and intent.notional > snapshot.cash + _EPS:
            blocks.append(RC_INSUFFICIENT_CASH)

        if blocks:
            return RiskDecision(False, blocks, 0.0, flags)

        return self._apply_exposure(intent, snapshot, flags, ext_reduce)

    def _apply_exposure(self, intent, snapshot, flags, ext_reduce) -> RiskDecision:
        av = snapshot.account_value
        price = intent.price
        lev = intent.leverage_factor
        pos = snapshot.positions.get(intent.symbol)
        exist_notional = pos.notional if pos else 0.0
        exist_effective = pos.effective_exposure if pos else 0.0
        total_eff_before = snapshot.total_effective_exposure

        max_sym_notional = av * self.cfg.max_symbol_notional_pct
        max_sym_eff = av * self.cfg.max_symbol_effective_pct
        max_port_eff = av * self.cfg.max_portfolio_effective_pct

        # 각 한도가 허용하는 추가 수량(headroom)
        hq_notional = (max_sym_notional - exist_notional) / price
        hq_sym_eff = (max_sym_eff - exist_effective) / (price * lev)
        hq_port_eff = (max_port_eff - total_eff_before) / (price * lev)

        allowed_qty = max(0.0, min(intent.quantity, hq_notional, hq_sym_eff, hq_port_eff))
        if ext_reduce:
            allowed_qty *= self.cfg.extended_hours_qty_factor

        # 요청 수량 기준 어떤 한도가 초과됐는지(코드 산출)
        breached: list[str] = []
        if exist_notional + intent.notional > max_sym_notional + _EPS:
            breached.append(RC_MAX_SYMBOL_NOTIONAL)
        if exist_effective + intent.effective > max_sym_eff + _EPS:
            breached.append(RC_LEVERAGED_ETF_LIMIT if lev > 1 else RC_MAX_SYMBOL_EFFECTIVE)
        if total_eff_before + intent.effective > max_port_eff + _EPS:
            breached.append(RC_MAX_PORTFOLIO_EFFECTIVE)

        if allowed_qty <= _EPS:
            return RiskDecision(False, breached or [RC_MAX_PORTFOLIO_EFFECTIVE], 0.0, flags)

        if allowed_qty < intent.quantity - _EPS:
            codes = [RC_QTY_REDUCED] + breached
            if ext_reduce and RC_EXT_HOURS_RISK not in codes:
                codes.append(RC_EXT_HOURS_RISK)
            return RiskDecision(True, codes, round(allowed_qty, 6), flags)

        return RiskDecision(True, [], round(allowed_qty, 6), flags)
