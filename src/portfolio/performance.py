"""performance_summary — Backtest / Paper 공용 성과 지표(수익률/승률/PF/MDD/Sharpe).

원칙:
- 순수 함수, 결정론적(now()/난수 미사용).
- BacktestEngine._metrics 의 기존 공식과 수치가 정확히 일치해야 한다(parity).
- realized_trades 는 RealizedTrade(.net_pnl) 또는 backtest Trade(.pnl) 어느 쪽이든
  받아들인다(둘 다 순손익 1개 필드를 갖는다 — 속성명만 다르다).
"""
from __future__ import annotations

import math

_EPS = 1e-9


def _pnl_of(trade) -> float:
    v = getattr(trade, "net_pnl", None)
    return v if v is not None else trade.pnl


def performance_summary(initial_capital: float, equity_curve: list[float],
                        realized_trades: list, *,
                        periods_per_year: float | None = None) -> dict:
    pnls = [_pnl_of(t) for t in realized_trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    final_equity = equity_curve[-1] if equity_curve else initial_capital
    total_return_pct = (round((final_equity - initial_capital) / initial_capital * 100.0, 4)
                        if initial_capital else 0.0)

    win_rate = round(len(wins) / n, 4) if n else 0.0
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > _EPS else None
    avg_win = round(gross_win / len(wins), 6) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 6) if losses else 0.0
    expectancy = round(sum(pnls) / n, 6) if n else 0.0

    # max drawdown
    peak = -float("inf")
    mdd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, (e - peak) / peak)
    max_drawdown_pct = round(mdd * 100.0, 4)

    # max consecutive losses
    mcl = cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            mcl = max(mcl, cur)
        else:
            cur = 0

    sharpe_ratio = _sharpe(equity_curve, periods_per_year)

    return {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe_ratio": sharpe_ratio,
        "total_trades": n,
        "max_consecutive_losses": mcl,
    }


def _sharpe(equity_curve: list[float], periods_per_year: float | None) -> float | None:
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev != 0:
            returns.append((equity_curve[i] - prev) / prev)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance)
    if std == 0:
        return None
    sharpe = mean / std
    if periods_per_year is not None:
        sharpe *= math.sqrt(periods_per_year)
    return round(sharpe, 4)
