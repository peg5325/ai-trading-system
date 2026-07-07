"""BacktestEngine — 과거 캔들로 long-only 전략을 검증.

원칙:
- Provider/Broker 미접근. 저장된 Canonical Candle 과 설정만 사용.
- live 와 동일한 IndicatorEngine / UniverseScreener / RiskManager 재사용.
- 지표는 인과적(causal)이므로 전체 시계열에 IndicatorEngine 을 1번 계산해도
  t 행 값은 t 시점에 확정된 값과 동일 → look-ahead 없음.
- 판단은 t 봉 종가(확정)로, 체결은 t+1 봉 시가로 (신호 봉 종가 체결 금지).
- 레버리지 ETF 는 세션(ET date) 마감 종가에 EOD flatten (오버나잇 금지).
- 결정론적: 현재 시각/난수 미사용.
"""
from __future__ import annotations

from src.engines.indicator_engine import IndicatorEngine
from src.models import ET, classify_session
from src.pnl.fifo import FifoMatcher
from src.pnl.models import FillEvent
from src.risk.models import BUY, NEW_ENTRY, SELL, OrderIntent, PortfolioSnapshot, Position
from src.risk.risk_manager import RiskManager
from src.universe.screener import SymbolData, UniverseScreener
from src.backtest.models import (
    SAMPLE_INSUFFICIENT,
    SAMPLE_OK,
    BacktestConfig,
    BacktestReport,
    Trade,
)


class ScreenerEntryStrategy:
    """기본 진입 전략: live 와 동일한 UniverseScreener 를 재사용.
    t 행까지 슬라이스해 후보(LONG_SETUP)면 진입 신호."""

    def __init__(self, screener: UniverseScreener | None = None):
        self._screener = screener or UniverseScreener()

    def signal(self, symbol, indicators, t, leverage_factor):
        window = indicators.iloc[: t + 1]
        cands = self._screener.screen([SymbolData(symbol, window, leverage_factor)])
        if cands and cands[0].symbol == symbol:
            return list(cands[0].reasons)
        return None


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None,
                 risk_manager: RiskManager | None = None):
        self.cfg = config or BacktestConfig()
        self.risk = risk_manager or RiskManager(self.cfg.risk)

    def run(self, candles, symbol, leverage_factor=1,
            entry_strategy=None) -> BacktestReport:
        cfg = self.cfg
        strat = entry_strategy or ScreenerEntryStrategy()
        exe = cfg.execution

        ind = IndicatorEngine().compute(candles)   # live 와 동일 모듈
        n = len(ind)
        if n == 0:
            return self._report(symbol, None, None, [], [], 0, cfg)

        idx = list(ind.index)
        ts_et = [t.tz_convert(ET) for t in idx]
        sessions = [classify_session(t) for t in ts_et]
        et_dates = [t.date() for t in ts_et]
        opens = ind["open"].tolist()
        closes = ind["close"].tolist()
        ema20 = ind["ema20"].tolist()

        def is_session_end(i: int) -> bool:
            return i == n - 1 or et_dates[i + 1] != et_dates[i]

        st = {"cash": cfg.initial_capital, "pos": None, "consec": 0}
        matcher = FifoMatcher(bar_minutes=5.0)     # 실현손익: live/paper 와 동일 엔진
        trades: list[Trade] = []
        equity_curve: list[float] = []
        bars_in_pos = 0
        pending_entry = None   # {qty, reasons, risk_decision}
        pending_exit = None    # {reasons}
        peak = cfg.initial_capital
        cur_date = None
        day_start_equity = cfg.initial_capital

        def open_position(i, qty, reasons, risk_decision):
            fill = exe.execute(qty, opens[i], BUY)
            st["cash"] -= fill.quantity * fill.price + fill.fee
            matcher.ingest(FillEvent(                       # 진입 체결 → FIFO 엔진
                symbol, BUY, fill.quantity, fill.price, fee=fill.fee,
                ref_price=opens[i], timestamp=idx[i], bar_index=i, reason=tuple(reasons)))
            st["pos"] = {
                "qty": fill.quantity, "entry_price": fill.price, "entry_fee": fill.fee,
                "entry_time": idx[i], "reasons": reasons, "risk_decision": risk_decision,
                "leverage": leverage_factor,
            }

        def close_position(i, ref_price, reasons, exit_time):
            p = st["pos"]
            fill = exe.execute(p["qty"], ref_price, SELL)
            st["cash"] += fill.quantity * fill.price - fill.fee
            rt = matcher.ingest(FillEvent(                   # 청산 체결 → FIFO 엔진(실현손익 계산)
                symbol, SELL, fill.quantity, fill.price, fee=fill.fee,
                ref_price=ref_price, timestamp=exit_time, bar_index=i,
                reason=tuple(reasons)))[0]
            trades.append(Trade(
                symbol, p["entry_time"], exit_time, rt.entry_price, rt.exit_price,
                rt.quantity, rt.net_pnl, rt.realized_return_pct, p["reasons"], reasons,
                p["risk_decision"]))
            st["consec"] = st["consec"] + 1 if rt.net_pnl < 0 else 0
            st["pos"] = None

        for i in range(n):
            o, c = opens[i], closes[i]

            # 1) 직전 봉에서 결정된 주문을 이번 봉 시가로 체결
            if pending_exit and st["pos"] is not None:
                close_position(i, o, pending_exit["reasons"], idx[i])
            pending_exit = None
            if pending_entry and st["pos"] is None:
                open_position(i, pending_entry["qty"], pending_entry["reasons"],
                              pending_entry["risk_decision"])
            pending_entry = None

            # 2) 종가 기준 마킹 / 자본 곡선
            if st["pos"]:
                bars_in_pos += 1
            equity = st["cash"] + (st["pos"]["qty"] * c if st["pos"] else 0.0)
            if et_dates[i] != cur_date:
                cur_date = et_dates[i]
                day_start_equity = equity
            equity_curve.append(equity)
            peak = max(peak, equity)
            cur_dd = (equity - peak) / peak if peak > 0 else 0.0
            daily_pnl_pct = ((equity - day_start_equity) / day_start_equity
                             if day_start_equity else 0.0)

            # 3) EOD flatten: 레버리지 ETF 는 세션 마감 종가에 청산 (오버나잇 금지)
            if (st["pos"] and cfg.leverage_eod_flatten
                    and leverage_factor > 1 and is_session_end(i)):
                close_position(i, c, ["EOD_FLATTEN"], idx[i])
                continue
            # 일반주 오버나잇 미허용 옵션이면 동일 처리
            if (st["pos"] and not cfg.allow_overnight_non_leverage
                    and is_session_end(i)):
                close_position(i, c, ["EOD_FLATTEN"], idx[i])
                continue

            # 4) 다음 봉 체결을 위한 판단 (t 행까지만 사용 → look-ahead 없음)
            if st["pos"]:
                reasons = self._exit_reasons(c, st["pos"]["entry_price"], ema20[i], cfg)
                if reasons:
                    pending_exit = {"reasons": reasons}
            elif not is_session_end(i):   # 오버나잇으로 넘어가는 진입 금지
                sig = strat.signal(symbol, ind, i, leverage_factor)
                if sig is not None:
                    decision = self._size_and_check(
                        symbol, c, leverage_factor, equity, sessions[i],
                        is_session_end(i), daily_pnl_pct, cur_dd, st["consec"])
                    if decision is not None and decision.allowed and decision.adjusted_quantity > 0:
                        pending_entry = {"qty": decision.adjusted_quantity,
                                         "reasons": sig, "risk_decision": decision.to_dict()}

        # 루프 종료 후 잔여 포지션 강제 청산(마지막 종가)
        if st["pos"]:
            close_position(n - 1, closes[n - 1], ["END_OF_DATA"], idx[n - 1])
            equity_curve[-1] = st["cash"]

        return self._report(symbol, idx[0], idx[-1], trades, equity_curve,
                            bars_in_pos, cfg)

    # ---- helpers ----
    def _exit_reasons(self, close, entry_price, ema20_val, cfg) -> list[str]:
        reasons = []
        if cfg.stop_loss_pct and close <= entry_price * (1 - cfg.stop_loss_pct):
            reasons.append("STOP_LOSS")
        if cfg.take_profit_pct and close >= entry_price * (1 + cfg.take_profit_pct):
            reasons.append("TAKE_PROFIT")
        if cfg.exit_on_trend_break and ema20_val == ema20_val and close < ema20_val:
            reasons.append("TREND_BREAK")
        return reasons

    def _size_and_check(self, symbol, ref_price, lev, equity, session,
                        eod_window, daily_pnl_pct, cur_dd, consec):
        if ref_price <= 0 or equity <= 0:
            return None
        desired_notional = equity * self.cfg.entry_notional_pct
        qty = desired_notional / ref_price
        intent = OrderIntent(symbol, NEW_ENTRY, BUY, qty, ref_price, lev)
        snap = PortfolioSnapshot(
            account_value=equity, positions={},
            daily_pnl_pct=daily_pnl_pct, drawdown_pct=cur_dd,
            consecutive_losses=consec, kill_switch=False,
            session=session, is_eod_flatten_window=eod_window)
        return self.risk.evaluate(intent, snap)

    def _report(self, symbol, start, end, trades, equity_curve, bars_in_pos, cfg):
        summary = self._metrics(trades, equity_curve, bars_in_pos, cfg)
        return BacktestReport(symbol, start, end, summary, trades)

    def _metrics(self, trades, equity_curve, bars_in_pos, cfg) -> dict:
        n = len(trades)
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        initial = cfg.initial_capital
        final = equity_curve[-1] if equity_curve else initial

        # max drawdown
        peak = -float("inf")
        mdd = 0.0
        for e in equity_curve:
            peak = max(peak, e)
            if peak > 0:
                mdd = min(mdd, (e - peak) / peak)
        # 연속 손실
        mcl = cur = 0
        for p in pnls:
            if p < 0:
                cur += 1
                mcl = max(mcl, cur)
            else:
                cur = 0
        bars_total = len(equity_curve)
        return {
            "total_return_pct": round((final - initial) / initial * 100.0, 4) if initial else 0.0,
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "avg_win": round(gross_win / len(wins), 6) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
            "expectancy": round(sum(pnls) / n, 6) if n else 0.0,
            "max_drawdown_pct": round(mdd * 100.0, 4),
            "max_consecutive_losses": mcl,
            "exposure_time": round(bars_in_pos / bars_total, 4) if bars_total else 0.0,
            "total_trades": n,
            "sample_status": SAMPLE_INSUFFICIENT if n < cfg.min_trades else SAMPLE_OK,
        }

    def run_in_out(self, candles, symbol, leverage_factor=1, ratio=0.7, entry_strategy=None):
        """in-sample / out-of-sample 분리 실행."""
        from src.backtest.models import split_in_out
        in_c, out_c = split_in_out(candles, ratio)
        return (self.run(in_c, symbol, leverage_factor, entry_strategy),
                self.run(out_c, symbol, leverage_factor, entry_strategy))

    def walk_forward(self, *args, **kwargs):
        """Walk-forward 검증 — MVP 미구현. 인터페이스만 예약(TODO).

        TODO: 구간 분할 → 각 in-sample 로 파라미터 최적화 → 다음 out-of-sample 로 검증 →
        롤링 반복하여 과최적화 저항성 평가.
        """
        raise NotImplementedError("walk-forward validation is reserved for a later phase (TODO)")
