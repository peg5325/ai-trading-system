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
    eod_flatten_minutes_before_close: int = 10   # 마감 몇 분 전부터 청산 창을 여는지(market별 close 기준)
    exit_on_trend_break: bool = False     # close < ema20 → 청산(선택; backtest 패리티)
    # 청산 창 안에서 신규/추가 진입을 전 종목 차단(기본 off = 기존 행동). 청산 규칙이 아니라
    # RiskManager 가 소비하는 진입 정책이지만, 창의 정의(위 두 필드)와 짝이라 여기 둔다.
    # RiskConfig 로는 run_paper 가 명시 배선한다.
    block_all_entries_in_eod_window: bool = False


@dataclass(frozen=True)
class ExitFill:
    """봉 기반 청산 판정 결과: 사유(=OrderIntent.kind)와 슬리피지 전 참조 체결가."""
    reason: str
    reference_price: float


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
    is_eod_window: bool = False      # 이 종목(market)의 마감 청산 창 여부 — symbol별 주입(혼합 유니버스 정확)


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
        # EOD 창은 symbol별(pos.is_eod_window, 혼합 유니버스 정확) 또는 컨텍스트(ctx) 중 하나로 판단.
        if (pos.is_eod_window or ctx.is_eod_window) and self.eod_applies(pos.leverage_factor):
            return EOD_FLATTEN
        return None

    # ---- 완료 1분봉 기반 판정(가격 1점이 아닌 open/high/low 사용) ----
    def evaluate_intrabar(self, pos: PositionView, bar) -> ExitFill | None:
        """봉 안 손절/익절. bar 는 open/high/low 속성을 가진다(Candle 호환).

        손절가·목표가 기준은 pos.entry_price(체결가). 같은 봉에서 둘 다 닿으면 손절 우선(보수적).
        손절 참조가: 시가가 손절가 아래(갭다운)면 시가, 아니면 손절가.
        익절 참조가: 목표가(유리한 갭은 인정하지 않음).
        """
        if pos.quantity <= 0 or pos.entry_price <= 0:
            return None
        sl, tp = self.cfg.stop_loss_pct, self.cfg.take_profit_pct
        if sl:
            stop = pos.entry_price * (1.0 - sl)
            if bar.low <= stop:
                return ExitFill(STOP_LOSS, min(stop, bar.open))
        if tp:
            target = pos.entry_price * (1.0 + tp)
            if bar.high >= target:
                return ExitFill(TAKE_PROFIT, target)
        return None

    def evaluate_bar_open(self, pos: PositionView, ctx: ExitContext, *,
                          bar_open: float) -> ExitFill | None:
        """봉 시가 시점 청산: TIME_EXIT(분 단위, ctx.now_time=봉 시작) > EOD_FLATTEN. 참조가는 봉 시가."""
        if pos.quantity <= 0:
            return None
        if self.hit_time_exit(pos, ctx):
            return ExitFill(TIME_EXIT, bar_open)
        if (pos.is_eod_window or ctx.is_eod_window) and self.eod_applies(pos.leverage_factor):
            return ExitFill(EOD_FLATTEN, bar_open)
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
