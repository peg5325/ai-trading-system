"""FifoMatcher — BUY/SELL 체결을 FIFO 로 매칭해 실현손익을 계산하는 단일 엔진.

원칙:
- 실거래 / Paper / Backtest 가 모두 이 엔진을 사용한다(계산 코드 단일화).
- Long-only 가정: SELL 은 앞선 BUY 롯을 오래된 것부터(FIFO) 소진한다.
- 부분청산/여러 번 매수/여러 번 매도 지원.
- 결정론적: now()/난수 미사용. 입력 체결의 순서(호출 순서)를 시간순으로 신뢰한다.
- 각 매칭 단위마다 RealizedTrade 1건 생성. 수수료는 매칭 수량에 비례 배분.

불변식: net_pnl == gross_pnl - slippage - commission == (exit_price-entry_price)*qty - commission
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

from src.pnl.models import FillEvent, OpenLot, RealizedTrade
from src.risk.models import BUY, SELL

_EPS = 1e-9


@dataclass
class _Lot:
    remaining: float
    price: float                  # 진입 체결가
    ref: float                    # 진입 의도가
    fee_per_unit: float           # 진입 수수료 / 진입 수량
    timestamp: datetime | None
    bar_index: int | None
    reason: tuple


class FifoMatcher:
    def __init__(self, bar_minutes: float = 5.0):
        self._bar_minutes = bar_minutes
        self._lots: dict[str, deque] = defaultdict(deque)
        self.trades: list[RealizedTrade] = []
        # long-only 위반(잔여 SELL: 보유 초과 매도) — 무시하되 감사 위해 기록
        self.unmatched_sells: list[tuple[str, float]] = []

    # ---- public ----
    def ingest(self, fill: FillEvent) -> list[RealizedTrade]:
        """체결 1건 반영. 이 체결로 확정된 RealizedTrade 리스트 반환(BUY 면 빈 리스트)."""
        if fill.quantity <= _EPS:
            return []
        if fill.side == BUY:
            self._open(fill)
            return []
        if fill.side == SELL:
            return self._close(fill)
        raise ValueError(f"unknown side: {fill.side}")

    def ingest_many(self, fills) -> list[RealizedTrade]:
        """여러 체결을 순서대로 반영. 호출자가 시간순으로 전달해야 한다."""
        out: list[RealizedTrade] = []
        for f in fills:
            out.extend(self.ingest(f))
        return out

    def open_quantity(self, symbol: str) -> float:
        return sum(lot.remaining for lot in self._lots.get(symbol, ()))

    def open_lots(self, symbol: str | None = None) -> list[OpenLot]:
        syms = [symbol] if symbol is not None else list(self._lots.keys())
        out = []
        for s in syms:
            for lot in self._lots.get(s, ()):
                out.append(OpenLot(s, lot.remaining, lot.price, lot.ref,
                                   lot.timestamp, lot.bar_index, lot.reason))
        return out

    # ---- internal ----
    def _open(self, fill: FillEvent) -> None:
        ref = fill.ref_price if fill.ref_price is not None else fill.price
        fpu = fill.fee / fill.quantity if fill.quantity else 0.0
        self._lots[fill.symbol].append(_Lot(
            remaining=fill.quantity, price=fill.price, ref=ref, fee_per_unit=fpu,
            timestamp=fill.timestamp, bar_index=fill.bar_index,
            reason=tuple(fill.reason)))

    def _close(self, fill: FillEvent) -> list[RealizedTrade]:
        lots = self._lots[fill.symbol]
        exit_ref = fill.ref_price if fill.ref_price is not None else fill.price
        exit_fpu = fill.fee / fill.quantity if fill.quantity else 0.0
        remaining = fill.quantity
        closed: list[RealizedTrade] = []

        while remaining > _EPS and lots:
            lot = lots[0]
            m = min(lot.remaining, remaining)
            commission = lot.fee_per_unit * m + exit_fpu * m
            gross = (exit_ref - lot.ref) * m
            slippage = (lot.price - lot.ref + exit_ref - fill.price) * m
            net = (fill.price - lot.price) * m - commission
            entry_notional = lot.price * m
            ret_pct = (net / entry_notional * 100.0) if entry_notional else 0.0
            hold_min = _hold_minutes(lot.timestamp, fill.timestamp)
            hold_bars = _hold_bars(lot.bar_index, fill.bar_index, hold_min, self._bar_minutes)

            closed.append(RealizedTrade(
                symbol=fill.symbol, quantity=m,
                entry_time=lot.timestamp, exit_time=fill.timestamp,
                entry_bar=lot.bar_index, exit_bar=fill.bar_index,
                entry_price=lot.price, exit_price=fill.price,
                entry_ref=lot.ref, exit_ref=exit_ref,
                gross_pnl=gross, commission=commission, slippage=slippage,
                net_pnl=net, realized_pnl=net, realized_return_pct=ret_pct,
                holding_minutes=hold_min, holding_bars=hold_bars,
                entry_reason=lot.reason, exit_reason=tuple(fill.reason)))

            lot.remaining -= m
            remaining -= m
            if lot.remaining <= _EPS:
                lots.popleft()

        if remaining > _EPS:                       # 보유 초과 매도(long-only 위반) → 방어적 기록
            self.unmatched_sells.append((fill.symbol, remaining))

        self.trades.extend(closed)
        return closed


def match_fills(fills, *, bar_minutes: float = 5.0):
    """편의 함수: 체결 시퀀스 → (실현 거래 리스트, 미청산 롯 리스트)."""
    m = FifoMatcher(bar_minutes=bar_minutes)
    m.ingest_many(fills)
    return m.trades, m.open_lots()


def _hold_minutes(t0: datetime | None, t1: datetime | None) -> float | None:
    if t0 is None or t1 is None:
        return None
    return (t1 - t0).total_seconds() / 60.0


def _hold_bars(b0: int | None, b1: int | None, hold_min: float | None,
               bar_minutes: float) -> int | None:
    if b0 is not None and b1 is not None:
        return b1 - b0
    if hold_min is not None and bar_minutes > 0:
        return int(round(hold_min / bar_minutes))
    return None
