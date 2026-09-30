"""PortfolioAccount — Paper Trading 의 현금/포지션/실현손익 단일 계좌.

원칙:
- FifoMatcher(src.pnl)를 재사용해 실현손익을 계산한다(계산 코드 단일화).
- 결정론적: now()/난수 미사용. 체결(FillEvent) 반영 순서만으로 상태가 정해진다.
- cash 는 체결 시점에 즉시 반영(BUY: 차감, SELL: 가산). 미실현손익/시가평가는
  mark(현재가) 를 별도로 주입받아 계산한다(계좌 자체는 가격을 모른다).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.pnl.fifo import FifoMatcher
from src.pnl.models import FillEvent
from src.risk.models import BUY

_EPS = 1e-9


@dataclass
class PortfolioValue:
    cash: float
    market_value: float
    realized_pnl: float
    unrealized_pnl: float
    total_equity: float          # == cash + market_value

    def to_dict(self) -> dict:
        return {
            "cash": round(self.cash, 6),
            "market_value": round(self.market_value, 6),
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl, 6),
            "total_equity": round(self.total_equity, 6),
        }


class PortfolioAccount:
    def __init__(self, initial_capital: float, bar_minutes: float = 1.0):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self._matcher = FifoMatcher(bar_minutes=bar_minutes)
        self._peak_equity = initial_capital
        self._day_start_equity = initial_capital
        self._day = None
        self._last_equity = initial_capital
        self._last_drawdown = 0.0
        self._last_daily_return = 0.0
        self._equity_curve: list[float] = []

    # ---- 체결 반영 ----
    def apply_fill(self, fill: FillEvent) -> None:
        notional = fill.quantity * fill.price
        if fill.side == BUY:
            self.cash -= notional + fill.fee
        else:
            self.cash += notional - fill.fee
        self._matcher.ingest(fill)

    # ---- 포지션/손익 조회 ----
    def positions(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for sym in {lot.symbol for lot in self._matcher.open_lots()}:
            lots = self._matcher.open_lots(sym)
            qty = sum(lot.quantity for lot in lots)
            if qty <= _EPS:
                continue
            notional = sum(lot.quantity * lot.entry_price for lot in lots)
            out[sym] = {"quantity": qty, "avg_price": notional / qty}
        return out

    def open_symbol_count(self) -> int:
        return len(self.positions())

    def realized_pnl(self) -> float:
        return sum(t.net_pnl for t in self._matcher.trades)

    def realized_trades(self) -> list:
        return self._matcher.trades

    def open_lots(self):
        return self._matcher.open_lots()

    def market_value(self, marks: dict) -> float:
        return sum(p["quantity"] * marks.get(sym, p["avg_price"])
                   for sym, p in self.positions().items())

    def unrealized_pnl(self, marks: dict) -> float:
        return sum((marks.get(lot.symbol, lot.entry_price) - lot.entry_price) * lot.quantity
                   for lot in self._matcher.open_lots())

    def equity(self, marks: dict) -> float:
        return self.cash + self.market_value(marks)

    def portfolio_value(self, marks: dict) -> PortfolioValue:
        mv = self.market_value(marks)
        return PortfolioValue(
            cash=self.cash, market_value=mv,
            realized_pnl=self.realized_pnl(), unrealized_pnl=self.unrealized_pnl(marks),
            total_equity=self.cash + mv)

    def can_afford(self, notional: float) -> bool:
        return self.cash + _EPS >= notional

    # ---- 마킹(자산곡선/낙폭/일일수익률) ----
    def mark_equity(self, marks: dict, day_key=None, *,
                    day_start_equity: float | None = None) -> float:
        """day_start_equity 를 주면 일일수익률 기준을 그 값으로 고정한다(직전 마감 자산 등 명시 앵커).
        None 이면 기존대로 day_key 가 바뀔 때의 첫 평가값을 기준으로 삼는다."""
        eq = self.equity(marks)
        if day_start_equity is not None:
            self._day = day_key
            self._day_start_equity = float(day_start_equity)
        elif day_key != self._day:
            self._day = day_key
            self._day_start_equity = eq
        self._peak_equity = max(self._peak_equity, eq)
        self._last_equity = eq
        self._last_drawdown = ((eq - self._peak_equity) / self._peak_equity
                               if self._peak_equity > 0 else 0.0)
        self._last_daily_return = ((eq - self._day_start_equity) / self._day_start_equity
                                   if self._day_start_equity > 0 else 0.0)
        self._equity_curve.append(eq)
        return eq

    def restore_peak(self, peak: float) -> None:
        """재시작 복원: 저장된 운용 고점을 되살린다. 고점은 낮아지지 않는다(재시작으로 초기화 금지)."""
        self._peak_equity = max(self._peak_equity, float(peak))

    # ---- read-only props ----
    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def last_equity(self) -> float:
        return self._last_equity

    @property
    def last_drawdown_pct(self) -> float:
        return self._last_drawdown

    @property
    def last_daily_return_pct(self) -> float:
        return self._last_daily_return

    @property
    def equity_curve(self) -> list[float]:
        return self._equity_curve

    @classmethod
    def from_fills(cls, initial_capital: float, fills, bar_minutes: float = 1.0) -> "PortfolioAccount":
        a = cls(initial_capital, bar_minutes)
        for f in fills:
            a.apply_fill(f)
        return a
