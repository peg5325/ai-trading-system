"""Realized PnL 엔진 데이터 모델.

한 곳(FIFO 엔진)에서만 실현손익을 계산하고, 실거래/Paper/Backtest 가 모두 이 모델을 공유한다.

핵심 불변식(항상 성립):
    net_pnl == gross_pnl - slippage - commission
    net_pnl == (exit_price - entry_price) * quantity - commission   (long 기준)
여기서 gross_pnl 은 의도가(ref) 기준, slippage 는 체결가-의도가 차이, commission 은 수수료.
ref_price 가 없으면 ref := 체결가 로 두어 slippage=0, gross=net+commission 이 된다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class FillEvent:
    """단일 체결. 실거래/Paper/Backtest 가 공통으로 엔진에 넣는 입력."""
    symbol: str
    side: str                         # BUY / SELL
    quantity: float
    price: float                      # 실제 체결가(슬리피지 반영)
    fee: float = 0.0                  # 이 체결의 수수료(절대액)
    ref_price: float | None = None    # 의도가(슬리피지 분해용). 없으면 슬리피지 미분리.
    timestamp: datetime | None = None
    bar_index: int | None = None      # 봉 인덱스(holding_bars 정확 계산용)
    reason: tuple[str, ...] = ()      # 진입/청산 사유(감사)


@dataclass(frozen=True)
class RealizedTrade:
    """FIFO 매칭으로 확정된 실현 거래(부분청산 시 매칭 단위 1건)."""
    symbol: str
    quantity: float
    entry_time: datetime | None
    exit_time: datetime | None
    entry_bar: int | None
    exit_bar: int | None
    entry_price: float                # 체결가
    exit_price: float
    entry_ref: float                  # 의도가
    exit_ref: float
    gross_pnl: float                  # (exit_ref - entry_ref) * qty
    commission: float                 # 진입+청산 수수료(매칭 수량 비례)
    slippage: float                   # (entry_price-entry_ref + exit_ref-exit_price) * qty
    net_pnl: float                    # gross_pnl - slippage - commission
    realized_pnl: float               # = net_pnl (명시적 별칭)
    realized_return_pct: float        # net_pnl / (entry_price*qty) * 100
    holding_minutes: float | None
    holding_bars: int | None
    entry_reason: tuple[str, ...] = ()
    exit_reason: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = _iso(self.entry_time)
        d["exit_time"] = _iso(self.exit_time)
        d["entry_reason"] = list(self.entry_reason)
        d["exit_reason"] = list(self.exit_reason)
        for k in ("entry_price", "exit_price", "entry_ref", "exit_ref",
                  "gross_pnl", "commission", "slippage", "net_pnl", "realized_pnl",
                  "quantity"):
            d[k] = round(d[k], 6)
        d["realized_return_pct"] = round(d["realized_return_pct"], 4)
        if d["holding_minutes"] is not None:
            d["holding_minutes"] = round(d["holding_minutes"], 3)
        return d


@dataclass(frozen=True)
class OpenLot:
    """미청산 잔여 롯(진입 후 아직 안 팔린 수량)."""
    symbol: str
    quantity: float
    entry_price: float
    entry_ref: float
    entry_time: datetime | None
    entry_bar: int | None
    entry_reason: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "quantity": round(self.quantity, 6),
                "entry_price": round(self.entry_price, 6),
                "entry_ref": round(self.entry_ref, 6),
                "entry_time": _iso(self.entry_time), "entry_bar": self.entry_bar,
                "entry_reason": list(self.entry_reason)}


@dataclass
class PnlSummary:
    """실현 거래 집계(Runtime Metrics / Backtest / Review 공용)."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    total_net_pnl: float = 0.0        # 누적 손익(net)
    total_gross_pnl: float = 0.0
    total_commission: float = 0.0
    total_slippage: float = 0.0
    gross_profit: float = 0.0         # 이긴 거래 net 합
    gross_loss: float = 0.0           # 진 거래 net 절대값 합
    avg_pnl: float = 0.0              # 평균 손익 = expectancy
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    expectancy: float = 0.0
    avg_holding_minutes: float | None = None
    avg_holding_bars: float | None = None
    open_positions: int = 0
    open_quantity: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("win_rate", "total_net_pnl", "total_gross_pnl", "total_commission",
                  "total_slippage", "gross_profit", "gross_loss", "avg_pnl", "avg_win",
                  "avg_loss", "expectancy", "open_quantity"):
            d[k] = round(d[k], 6)
        if d["profit_factor"] is not None:
            d["profit_factor"] = round(d["profit_factor"], 4)
        for k in ("avg_holding_minutes", "avg_holding_bars"):
            if d[k] is not None:
                d[k] = round(d[k], 3)
        return d


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
