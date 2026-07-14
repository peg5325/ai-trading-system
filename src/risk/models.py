"""Risk Manager 입출력 데이터 모델.

Broker/Provider 에 접근하지 않는다. RiskManager 는 오직 이 스냅샷 구조만 평가한다.
모든 판단은 결정론적이며 현재 시각이 아니라 snapshot 필드(session, is_eod_flatten_window 등)에 의존한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime   # PortfolioSnapshot 쿨다운 필드 타입(get_type_hints/타입체커용)

# ---- 주문 종류 ----
NEW_ENTRY = "NEW_ENTRY"      # 신규 진입
ADD = "ADD"                  # 추가 진입(분할매수)
EXIT = "EXIT"                # 청산(일반/명시적)
EOD_FLATTEN = "EOD_FLATTEN"  # 세션 마감 청산
# 청산 사유별 종류 — ExitSignalGenerator 가 생성. kind 가 곧 exit_reason 으로 journal→FIFO 전파.
STOP_LOSS = "STOP_LOSS"      # 고정 손절률 청산
TAKE_PROFIT = "TAKE_PROFIT"  # 고정 익절률 청산
TIME_EXIT = "TIME_EXIT"      # N봉/N분 보유 후 청산
TREND_BREAK = "TREND_BREAK"  # 추세 이탈 청산(선택)

# 트리거 청산(세션마감 아님) — 지표/리포트에서 EOD 와 구분해 exit 로 집계
TRIGGERED_EXITS = frozenset({EXIT, STOP_LOSS, TAKE_PROFIT, TIME_EXIT, TREND_BREAK})
# 리스크 축소 주문(항상 허용): 트리거 청산 + 세션마감 청산
RISK_REDUCING = TRIGGERED_EXITS | frozenset({EOD_FLATTEN})
ENTRY_KINDS = frozenset({NEW_ENTRY, ADD})

BUY = "BUY"
SELL = "SELL"


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float
    market_price: float
    leverage_factor: int = 1
    scale_in_count: int = 1   # 지금까지의 진입 횟수(최초 진입 포함)

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.market_price

    @property
    def effective_exposure(self) -> float:
        return self.notional * self.leverage_factor

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "quantity": self.quantity, "avg_price": self.avg_price,
            "market_price": self.market_price, "leverage_factor": self.leverage_factor,
            "scale_in_count": self.scale_in_count,
            "notional": self.notional, "effective_exposure": self.effective_exposure,
        }


@dataclass
class OrderIntent:
    symbol: str
    kind: str                 # NEW_ENTRY / ADD / EXIT / EOD_FLATTEN
    side: str                 # BUY / SELL
    quantity: float
    price: float
    leverage_factor: int = 1
    risk_flags: list[str] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    @property
    def effective(self) -> float:
        return self.notional * self.leverage_factor


@dataclass
class PortfolioSnapshot:
    account_value: float                       # 계좌 평가금액(equity) — 노출 비율 기준
    positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl_pct: float = 0.0                 # 당일 손익률(계좌 대비, 음수=손실)
    drawdown_pct: float = 0.0                  # 최근 고점 대비 낙폭(부호 무관, 크기로 판정)
    consecutive_losses: int = 0
    kill_switch: bool = False
    session: str = "REGULAR"                   # PRE / REGULAR / POST / OVERNIGHT (스냅샷 기준)
    is_eod_flatten_window: bool = False        # 세션 마감 임박(레버리지 flatten 창)
    cash: float | None = None                  # 가용 현금(자금관리) — None 이면 현금 체크 미적용
    # 재진입 쿨다운(timestamp 기반, clock-free): 현재 봉 시각 + 종목별 마지막 청산(시각/사유).
    # 미주입 시 쿨다운 미적용(기본 off 경로와 동일).
    now_time: datetime | None = None
    last_exit_time: dict[str, datetime] = field(default_factory=dict)
    last_exit_reason: dict[str, str] = field(default_factory=dict)

    @property
    def total_effective_exposure(self) -> float:
        return sum(p.effective_exposure for p in self.positions.values())

    def to_dict(self) -> dict:
        return {
            "account_value": self.account_value,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "daily_pnl_pct": self.daily_pnl_pct, "drawdown_pct": self.drawdown_pct,
            "consecutive_losses": self.consecutive_losses, "kill_switch": self.kill_switch,
            "session": self.session, "is_eod_flatten_window": self.is_eod_flatten_window,
            "total_effective_exposure": self.total_effective_exposure,
            "cash": self.cash,
            "now_time": self.now_time.isoformat() if self.now_time else None,
            "last_exit_time": {s: t.isoformat() for s, t in self.last_exit_time.items()},
            "last_exit_reason": dict(self.last_exit_reason),
        }


@dataclass
class RiskDecision:
    allowed: bool
    reason_codes: list[str]
    adjusted_quantity: float
    risk_flags: list[str]

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason_codes": self.reason_codes,
            "adjusted_quantity": self.adjusted_quantity,
            "risk_flags": self.risk_flags,
        }
