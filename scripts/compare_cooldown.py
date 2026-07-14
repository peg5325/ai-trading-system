"""Re-entry Cooldown A/B 비교 (Backtest 기반, read-only).

동일 캔들에 대해 cooldown OFF vs ON 두 config 로 BacktestEngine 을 돌려 안전장치로서의
손익 트레이드오프를 정량 비교한다. Paper 와 동일한 RiskManager 정책을 공유.

출력: total return · trade count(회전율) · win rate · profit factor · MDD ·
total cost(추정) · cost ratio(추정) · missed opportunity(cooldown 이 차단한 진입의 사후 손익).

주의: cooldown 은 수익 개선 기능이 아니라 과매매/whipsaw 방지 안전장치다. 사용 전 이 A/B 로
"수익률을 크게 훼손하지 않으면서 회전/비용을 줄이는지"를 확인하는 용도.

사용:
    python scripts/compare_cooldown.py <market_db> <symbol> [stop_loss_bars] [interval]
예:
    python scripts/compare_cooldown.py data/market_us_exit_test_2000usd.sqlite SOXL 6 1m
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest.engine import BacktestEngine
from src.backtest.models import BacktestConfig
from src.risk.reentry_cooldown import ReentryCooldownConfig
from src.risk.risk_manager import RiskConfig


def _cost(trades, slippage_bps: float, fee_bps: float) -> tuple[float, float, float]:
    """실행모델 bps 로 왕복 마찰비용/네트/cost_ratio 추정(백테스트 Trade 기준).

    ⚠ 추정치: 각 Trade.pnl 은 FIFO 가 이미 슬리피지/수수료를 반영한 net 이라, 여기서 bps 로
    역산한 total_cost/cost_ratio 는 진단용 근사치다(정확한 분해는 RealizedTrade 필요)."""
    bps = (slippage_bps + fee_bps) / 10_000.0
    total_cost = 0.0
    net = 0.0
    for t in trades:
        notional = abs(t.quantity * t.entry_price) + abs(t.quantity * t.exit_price)
        total_cost += notional * bps
        net += t.pnl
    gross = net + total_cost
    cost_ratio = (total_cost / gross) if gross > 1e-9 else None    # gross<=0 → unstable
    return round(total_cost, 4), round(net, 4), (round(cost_ratio, 4) if cost_ratio is not None else None)


def _missed_opportunity(off_trades, on_trades) -> dict:
    """OFF 에는 있으나 ON 에는 없는 진입(=cooldown 이 차단) 의 OFF 실현손익 집계(근사).

    양수 합 = 놓친 수익(opportunity cost), 음수 합 = 회피한 손실(whipsaw 방지).
    entry_time 매칭 기반 근사(cooldown 이 이후 시퀀스를 바꾸므로 정확한 1:1 은 아님)."""
    on_entries = {t.entry_time for t in on_trades}
    blocked = [t for t in off_trades if t.entry_time not in on_entries]
    missed_profit = sum(t.pnl for t in blocked if t.pnl > 0)
    avoided_loss = sum(t.pnl for t in blocked if t.pnl < 0)
    return {
        "blocked_entries": len(blocked),
        "missed_profit": round(missed_profit, 4),      # cooldown 이 놓친 이익
        "avoided_loss": round(avoided_loss, 4),        # cooldown 이 피한 손실(음수)
        "net_missed": round(missed_profit + avoided_loss, 4),
    }


def _run(candles, symbol, leverage_factor, cooldown_bars):
    risk = RiskConfig(reentry_cooldown=ReentryCooldownConfig(
        enabled=cooldown_bars > 0, mode="hard", scope="symbol",
        unit="trading_bars", timeframe_minutes=5.0, reset_on_new_session=True,
        stop_loss=cooldown_bars))
    cfg = BacktestConfig(
        stop_loss_pct=0.01, take_profit_pct=0.015, exit_on_trend_break=False,
        entry_notional_pct=0.20, min_trades=1, risk=risk)
    report = BacktestEngine(cfg).run(candles, symbol, leverage_factor)
    return cfg, report


def _summary_row(label, report, cfg) -> dict:
    s = report.summary
    total_cost, net, cost_ratio = _cost(report.trades, cfg.execution.slippage_bps,
                                        cfg.execution.fee_bps)
    return {
        "run": label,
        "trades": len(report.trades),
        "total_return_pct": round(s.get("total_return_pct", 0.0), 4),
        "win_rate": round(s.get("win_rate", 0.0), 4),
        "profit_factor": s.get("profit_factor"),
        "max_drawdown_pct": round(s.get("max_drawdown_pct", 0.0), 4),
        "net_pnl": net,
        "total_cost_est": total_cost,
        "cost_ratio_est": cost_ratio,
    }


def compare(candles, symbol: str, *, leverage_factor: int = 1,
            cooldown_bars: int = 6) -> dict:
    """cooldown OFF/ON A/B 결과(dict). candles = list[Candle] (read-only)."""
    off_cfg, off = _run(candles, symbol, leverage_factor, 0)
    on_cfg, on = _run(candles, symbol, leverage_factor, cooldown_bars)
    return {
        "symbol": symbol,
        "cooldown_bars": cooldown_bars,
        "off": _summary_row("cooldown_off", off, off_cfg),
        "on": _summary_row(f"cooldown_on({cooldown_bars})", on, on_cfg),
        "missed_opportunity": _missed_opportunity(off.trades, on.trades),
    }


def _print(result: dict) -> None:
    print(f"\n=== Re-entry Cooldown A/B — {result['symbol']} "
          f"(stop_loss cooldown = {result['cooldown_bars']} bars) ===")
    cols = ["run", "trades", "total_return_pct", "win_rate", "profit_factor",
            "max_drawdown_pct", "net_pnl", "total_cost_est", "cost_ratio_est"]
    print(" | ".join(cols) + "     (*_est = bps 추정치)")
    for row in (result["off"], result["on"]):
        print(" | ".join(str(row.get(c)) for c in cols))
    mo = result["missed_opportunity"]
    print(f"\nmissed opportunity: blocked_entries={mo['blocked_entries']} · "
          f"missed_profit={mo['missed_profit']} · avoided_loss={mo['avoided_loss']} · "
          f"net_missed={mo['net_missed']}")
    print("\n주: cooldown 은 안전장치(과매매/whipsaw 방지)이지 수익 개선 기능이 아니다. "
          "회전/비용 감소 대비 수익 훼손 여부로 판단할 것.")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    market_db, symbol = argv[1], argv[2]
    cooldown_bars = int(argv[3]) if len(argv) > 3 else 6
    interval = argv[4] if len(argv) > 4 else "1m"
    from src.storage.candle_store import CandleStore
    with CandleStore(market_db) as store:
        candles = store.query(symbol, interval)
    if not candles:
        print(f"[compare_cooldown] no candles for {symbol}@{interval} in {market_db}")
        return 1
    _print(compare(candles, symbol, cooldown_bars=cooldown_bars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
