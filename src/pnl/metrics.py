"""실현 거래 → PnlSummary 집계 (Runtime Metrics / Backtest / Review 공용).

누적손익 / 평균손익 / Profit Factor / Expectancy / 평균 보유시간 / 평균 Holding Bar 등.
결정론적: 순수 집계 함수.
"""
from __future__ import annotations

from src.pnl.models import OpenLot, PnlSummary, RealizedTrade

_EPS = 1e-9


def summarize(trades: list[RealizedTrade],
              open_lots: list[OpenLot] | None = None) -> PnlSummary:
    s = PnlSummary()
    open_lots = open_lots or []
    s.open_positions = len({lot.symbol for lot in open_lots})
    s.open_quantity = sum(lot.quantity for lot in open_lots)
    n = len(trades)
    s.trades = n
    if n == 0:
        return s

    wins = [t for t in trades if t.net_pnl > _EPS]
    losses = [t for t in trades if t.net_pnl < -_EPS]
    s.wins = len(wins)
    s.losses = len(losses)
    s.breakeven = n - s.wins - s.losses
    s.win_rate = s.wins / n

    s.total_net_pnl = sum(t.net_pnl for t in trades)
    s.total_gross_pnl = sum(t.gross_pnl for t in trades)
    s.total_commission = sum(t.commission for t in trades)
    s.total_slippage = sum(t.slippage for t in trades)
    s.gross_profit = sum(t.net_pnl for t in wins)
    s.gross_loss = abs(sum(t.net_pnl for t in losses))

    s.avg_pnl = s.total_net_pnl / n
    s.expectancy = s.avg_pnl
    s.avg_win = (s.gross_profit / s.wins) if s.wins else 0.0
    s.avg_loss = (-s.gross_loss / s.losses) if s.losses else 0.0
    s.profit_factor = (s.gross_profit / s.gross_loss) if s.gross_loss > _EPS else None

    hm = [t.holding_minutes for t in trades if t.holding_minutes is not None]
    hb = [t.holding_bars for t in trades if t.holding_bars is not None]
    s.avg_holding_minutes = (sum(hm) / len(hm)) if hm else None
    s.avg_holding_bars = (sum(hb) / len(hb)) if hb else None
    return s
