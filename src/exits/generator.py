"""ExitSignalGenerator — 보유 포지션에 대한 청산 신호를 생성하는 단일 엔진.

원칙:
- AI 는 청산에 절대 관여하지 않는다. 청산은 순수 결정론 코드(현재 시각/난수 미사용;
  판단은 주입된 스냅샷 필드에만 의존).
- live / paper / backtest 가 이 엔진을 공유한다(청산 로직이 갈라지지 않게).
- 생성된 청산은 RISK_REDUCING 주문(EXIT/STOP_LOSS/TAKE_PROFIT/TIME_EXIT/EOD_FLATTEN)으로
  기존 RiskManager→OrderManager→Broker 경로를 재사용한다(RiskManager 는 risk-reducing 을 허용).

청산 우선순위(요구사항 #5): STOP_LOSS > TAKE_PROFIT > TIME_EXIT > EOD_FLATTEN.
  (TREND_BREAK 은 선택 옵션으로 신호 계층에서 TIME_EXIT 다음, EOD 앞에 위치 — 기본 비활성.)

각 사유는 곧 OrderIntent.kind 로 사용되며, kind 는 journal→FIFO 를 통해 그대로
RealizedTrade.exit_reason 으로 전파된다(별도 배선 없이 exit_reason 저장이 보장됨).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.risk.models import (
    EOD_FLATTEN,
    SELL,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_EXIT,
    TREND_BREAK,
    OrderIntent,
)


@dataclass(frozen=True)
class ExitConfig:
    """청산 규칙 설정. 0.0/None 은 해당 규칙 비활성으로 간주한다."""
    stop_loss_pct: float = 0.0            # 예: 0.01 == -1.0% 손절 (0/None=끔)
    take_profit_pct: float = 0.0          # 예: 0.015 == +1.5% 익절 (0/None=끔)
    time_exit_bars: int | None = None     # N봉 보유 후 청산(진입/현재 bar_index 필요)
    time_exit_minutes: float | None = None  # N분 보유 후 청산(진입/현재 timestamp 필요)
    enable_all_symbols_eod_flatten: bool = False   # True: 전 종목 세션마감 청산(Paper 검증용)
    leverage_eod_flatten: bool = True     # 레버리지 ETF(>1x)는 세션마감 청산(보수적 기본)
    exit_on_trend_break: bool = False     # close < ema20 → 청산(선택; backtest 패리티)


@dataclass(frozen=True)
class PositionView:
    """청산 평가에 필요한 포지션의 정규화 뷰(계좌/백테스트 공통 입력)."""
    symbol: str
    quantity: float
    entry_price: float               # 평균 진입가(체결가)
    current_price: float             # 현재 마크 가격
    leverage_factor: int = 1
    entry_time: datetime | None = None
    entry_bar: int | None = None
    ema20: float | None = None       # 선택: trend break 판정용


@dataclass(frozen=True)
class ExitContext:
    """청산 평가 시점의 시장/시각 컨텍스트(모든 포지션 공통)."""
    is_eod_window: bool = False      # 세션 마감(청산 창) 여부 — market-correct 로 주입
    now_time: datetime | None = None  # 현재 봉 시각(time_exit_minutes 용)
    now_bar: int | None = None       # 현재 봉 인덱스(time_exit_bars 용)


class ExitSignalGenerator:
    def __init__(self, config: ExitConfig | None = None):
        self.cfg = config or ExitConfig()

    # ---- 개별 규칙(순수 술어) ----
    def hit_stop_loss(self, pos: PositionView) -> bool:
        sl = self.cfg.stop_loss_pct
        return bool(sl) and pos.entry_price > 0 and \
            pos.current_price <= pos.entry_price * (1.0 - sl)

    def hit_take_profit(self, pos: PositionView) -> bool:
        tp = self.cfg.take_profit_pct
        return bool(tp) and pos.entry_price > 0 and \
            pos.current_price >= pos.entry_price * (1.0 + tp)

    def hit_time_exit(self, pos: PositionView, ctx: ExitContext) -> bool:
        cfg = self.cfg
        if (cfg.time_exit_bars is not None and pos.entry_bar is not None
                and ctx.now_bar is not None):
            if (ctx.now_bar - pos.entry_bar) >= cfg.time_exit_bars:
                return True
        if (cfg.time_exit_minutes is not None and pos.entry_time is not None
                and ctx.now_time is not None):
            held_min = (ctx.now_time - pos.entry_time).total_seconds() / 60.0
            if held_min >= cfg.time_exit_minutes:
                return True
        return False

    def hit_trend_break(self, pos: PositionView) -> bool:
        return bool(self.cfg.exit_on_trend_break) and pos.ema20 is not None \
            and pos.ema20 == pos.ema20 and pos.current_price < pos.ema20  # ema20==ema20: NaN 제외

    def eod_applies(self, leverage_factor: int) -> bool:
        """세션 마감 청산이 이 종목에 적용되는가(레버리지/전종목 옵션)."""
        if self.cfg.enable_all_symbols_eod_flatten:
            return True
        return self.cfg.leverage_eod_flatten and leverage_factor > 1

    # ---- 신호 계층(EOD 제외): SL > TP > TIME > TREND ----
    def evaluate_signal(self, pos: PositionView, ctx: ExitContext) -> str | None:
        if pos.quantity <= 0:
            return None
        if self.hit_stop_loss(pos):
            return STOP_LOSS
        if self.hit_take_profit(pos):
            return TAKE_PROFIT
        if self.hit_time_exit(pos, ctx):
            return TIME_EXIT
        if self.hit_trend_break(pos):
            return TREND_BREAK
        return None

    # ---- 전체 평가(우선순위): SL > TP > TIME > (TREND) > EOD ----
    def evaluate(self, pos: PositionView, ctx: ExitContext) -> str | None:
        if pos.quantity <= 0:
            return None
        reason = self.evaluate_signal(pos, ctx)
        if reason is not None:
            return reason
        if ctx.is_eod_window and self.eod_applies(pos.leverage_factor):
            return EOD_FLATTEN
        return None

    # ---- 보유 포지션 목록 → 청산 OrderIntent 목록 (paper/live) ----
    def generate_intents(self, positions: list[PositionView],
                         ctx: ExitContext) -> list[OrderIntent]:
        out: list[OrderIntent] = []
        for pos in positions:
            if pos.quantity <= 0:
                continue
            reason = self.evaluate(pos, ctx)
            if reason is None:
                continue
            # 사유(=kind)가 journal→FIFO 로 exit_reason 전파. SELL 로 전량 청산 시도.
            out.append(OrderIntent(
                symbol=pos.symbol, kind=reason, side=SELL,
                quantity=pos.quantity, price=pos.current_price,
                leverage_factor=pos.leverage_factor, risk_flags=[reason]))
        return out
