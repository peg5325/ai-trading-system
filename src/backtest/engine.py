"""BacktestEngine — 과거 캔들로 long-only 전략을 검증.

원칙:
- Provider/Broker 미접근. 저장된 Canonical Candle 과 설정만 사용.
- live 와 동일한 IndicatorEngine / UniverseScreener / RiskManager 재사용.
- 지표는 인과적(causal)이므로 전체 시계열에 IndicatorEngine 을 1번 계산해도
  t 행 값은 t 시점에 확정된 값과 동일 → look-ahead 없음.
- 판단은 t 봉 종가(확정)로, 체결은 t+1 봉 시가로 (신호 봉 종가 체결 금지).
- 레버리지 ETF 는 거래소 로컬 거래일 마감 종가에 EOD flatten (오버나잇 금지).
- 세션/거래일은 canonical Candle 의 market/session/ts_exchange 를 상속한다.
  단일 run 에 복수 market 이 섞이면 임의 timezone 을 고르지 않고 fail-closed 한다.
- 결정론적: 현재 시각/난수 미사용.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src.engines.indicator_engine import (
    IndicatorEngine,
    candles_to_frame,
    frame_to_candles,
)
from src.exits.generator import ExitConfig, ExitContext, ExitSignalGenerator, PositionView
from src.models import (
    MARKET_KR,
    MARKET_US,
    SESSION_CLOSED,
    SESSION_OVERNIGHT,
    SESSION_POST,
    SESSION_PRE,
    SESSION_REGULAR,
    exchange_tz,
)
from src.pnl.fifo import FifoMatcher
from src.pnl.models import FillEvent
from src.portfolio.performance import performance_summary
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

_SESSIONS_BY_MARKET = {
    MARKET_US: frozenset({
        SESSION_PRE, SESSION_REGULAR, SESSION_POST, SESSION_OVERNIGHT,
    }),
    MARKET_KR: frozenset({
        SESSION_PRE, SESSION_REGULAR, SESSION_POST, SESSION_CLOSED,
    }),
}


def _validate_backtest_candles(canonical, symbol: str) -> str:
    """단일 symbol·market 계약과 canonical timestamp/session 불변식을 검증한다."""
    markets = {c.market for c in canonical}
    unsupported = sorted(markets - _SESSIONS_BY_MARKET.keys())
    if unsupported:
        raise ValueError(
            f"BacktestEngine 이 지원하지 않는 market: {unsupported}; "
            f"허용={sorted(_SESSIONS_BY_MARKET)}")
    if len(markets) != 1:
        raise ValueError(
            "BacktestEngine.run 은 단일 market 시계열만 허용한다; "
            f"입력 markets={sorted(markets)}")
    market = next(iter(markets))

    allowed_sessions = _SESSIONS_BY_MARKET[market]
    for candle in canonical:
        if candle.symbol != symbol:
            raise ValueError(
                "BacktestEngine.run 은 단일 symbol 시계열만 허용한다; "
                f"요청={symbol!r}, 입력={candle.symbol!r}")
        if candle.session not in allowed_sessions:
            raise ValueError(
                f"{market} market 에 허용되지 않는 session={candle.session!r}; "
                f"허용={sorted(allowed_sessions)}")
        if candle.ts_utc.tzinfo is None or candle.ts_exchange.tzinfo is None:
            raise ValueError("backtest candle timestamp 는 timezone-aware 여야 한다")
        if candle.ts_utc.astimezone(timezone.utc) != \
                candle.ts_exchange.astimezone(timezone.utc):
            raise ValueError(
                "candle ts_utc 와 ts_exchange 가 같은 순간을 가리켜야 한다")
    return market


def _prepare_backtest_input(
        candles, symbol: str,
) -> tuple[pd.DataFrame, list[datetime], list[str], str | None]:
    """입력 OHLCV와 canonical market clock을 같은 정렬·dedup 결과로 준비한다.

    `BacktestEngine.run` 은 단일 symbol·단일 market 시계열 API다. 혼합 market을
    대표 timezone 하나로 해석하면 EOD/일손실/cooldown 경계가 조용히 틀리므로 거부한다.
    DataFrame 하위호환 경로도 먼저 Candle 로 정규화한 뒤 같은 검증을 적용한다.
    """
    if isinstance(candles, pd.DataFrame):
        canonical = frame_to_candles(candles, symbol)
    elif isinstance(candles, list):
        canonical = candles
    else:
        canonical = list(candles)
    if not canonical:
        return candles_to_frame([]), [], [], None

    market = _validate_backtest_candles(canonical, symbol)
    source = candles_to_frame(canonical)
    # OHLCV와 metadata 모두 candles_to_frame이 선택한 동일 dedup 행에서 읽는다.
    aligned_times = [
        ts.astimezone(exchange_tz(market)) for ts in source["ts_exchange"]
    ]
    aligned_sessions = source["session"].tolist()
    return source, aligned_times, aligned_sessions, market


def _exit_config_from_backtest(cfg) -> ExitConfig:
    """BacktestConfig → 공유 ExitConfig 매핑(청산 로직 단일화).
    allow_overnight_non_leverage=False 는 전종목 EOD flatten 과 동치."""
    return ExitConfig(
        stop_loss_pct=cfg.stop_loss_pct or 0.0,
        take_profit_pct=cfg.take_profit_pct or 0.0,
        # PR 3-B: BacktestConfig.time_exit_* 배선(Time 스윕 가능). 기본 None → TIME_EXIT off
        # 이므로 미설정 시 기존 백테스트 동작 무변경. live/paper 와 동일 ExitConfig 계약.
        time_exit_bars=cfg.time_exit_bars,
        time_exit_minutes=cfg.time_exit_minutes,
        enable_all_symbols_eod_flatten=not cfg.allow_overnight_non_leverage,
        leverage_eod_flatten=cfg.leverage_eod_flatten,
        exit_on_trend_break=cfg.exit_on_trend_break)


class ScreenerEntryStrategy:
    """기본 진입 전략: live 와 동일한 UniverseScreener 를 재사용.
    t 행까지 슬라이스해 후보(LONG_SETUP)면 진입 신호."""

    def __init__(self, screener: UniverseScreener | None = None,
                 market: str = MARKET_US):
        self._screener = screener or UniverseScreener()
        self._market = market

    def signal(self, symbol, indicators, t, leverage_factor):
        window = indicators.iloc[: t + 1]
        cands = self._screener.screen([
            SymbolData(symbol, window, leverage_factor, self._market)
        ])
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
        exe = cfg.execution
        gen = ExitSignalGenerator(_exit_config_from_backtest(cfg))   # live/paper 와 동일 엔진

        source, local_times, sessions, market = _prepare_backtest_input(candles, symbol)
        strat = entry_strategy or ScreenerEntryStrategy(market=market or MARKET_US)
        ind = IndicatorEngine().compute(source)   # live 와 동일 모듈
        n = len(ind)
        if n == 0:
            return self._report(symbol, None, None, [], [], 0, cfg)
        if not ind.index.equals(source.index):
            raise ValueError("indicator 결과와 canonical market clock 정렬에 실패했다")

        idx = list(ind.index)
        trading_dates = [t.date() for t in local_times]
        opens = ind["open"].tolist()
        closes = ind["close"].tolist()
        ema20 = ind["ema20"].tolist()

        def is_trading_day_end(i: int) -> bool:
            # 거래소의 예정 마감시각이 아니라 입력 시계열에 존재하는 로컬 거래일의 마지막 봉.
            # 데이터 완전성은 상위 history manifest 계약의 책임이며 기존 체결 의미를 보존한다.
            return i == n - 1 or trading_dates[i + 1] != trading_dates[i]

        st = {"cash": cfg.initial_capital, "pos": None, "consec": 0,
              "last_exit_time": None, "last_exit_reason": None}   # 재진입 쿨다운(Paper 와 동일 정책)
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
                "entry_time": idx[i], "entry_bar": i, "reasons": reasons,
                "risk_decision": risk_decision, "leverage": leverage_factor,
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
            st["last_exit_time"] = exit_time                    # 재진입 쿨다운용(timestamp)
            st["last_exit_reason"] = reasons[0] if reasons else None
            st["pos"] = None

        for i in range(n):
            o, c = opens[i], closes[i]

            # 0) 재진입 쿨다운 세션 리셋 — 거래일 전환 시 '직전 봉 pending 체결보다 먼저' 초기화.
            #    paper(_maybe_reset_session 이 _apply_fills 보다 선행)와 순서를 일치시켜, 새 세션
            #    첫 봉 시가에 체결되는 오버나잇 청산의 쿨다운은 보존한다(Paper/Backtest 동일 정책).
            if (cfg.risk.reentry_cooldown.reset_on_new_session and i > 0
                    and trading_dates[i] != trading_dates[i - 1]):
                st["last_exit_time"] = None
                st["last_exit_reason"] = None

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
            if trading_dates[i] != cur_date:
                cur_date = trading_dates[i]
                day_start_equity = equity          # 쿨다운 세션 리셋은 위 0) 에서 처리(순서 일치)
            equity_curve.append(equity)
            peak = max(peak, equity)
            cur_dd = (equity - peak) / peak if peak > 0 else 0.0
            daily_pnl_pct = ((equity - day_start_equity) / day_start_equity
                             if day_start_equity else 0.0)

            # 3) EOD flatten: 거래소 로컬 거래일 마감 종가에 청산 (공유 generator 의 eod_applies).
            #    레버리지 ETF(>1x) 또는 전종목 옵션(=not allow_overnight_non_leverage) 대상.
            #    실행 타이밍은 마감봉 종가(같은 봉) — 백테스트 체결 모델 보존.
            #    ⚠ 의도된 예외: 백테스트는 마감봉에서 EOD 를 신호청산보다 우선한다(마감가 체결
            #    충실도 유지). live/paper 는 generator.evaluate() 로 SL>TP>TIME>EOD 우선순위를
            #    따른다(다음봉 시가 개념 없음). 두 경로는 마감봉에서만 사유 라벨/체결시점이 갈릴 수
            #    있으며, 그 외 모든 봉에서는 동일한 공유 규칙(evaluate_signal)을 쓴다.
            if st["pos"] and is_trading_day_end(i) and gen.eod_applies(leverage_factor):
                close_position(i, c, ["EOD_FLATTEN"], idx[i])
                continue

            # 4) 신호 청산(SL/TP/TIME/TREND) — 다음 봉 시가 체결. t 행까지만 사용(look-ahead 없음).
            if st["pos"]:
                pv = PositionView(
                    symbol=symbol, quantity=st["pos"]["qty"],
                    entry_price=st["pos"]["entry_price"], current_price=c,
                    leverage_factor=leverage_factor, entry_time=st["pos"]["entry_time"],
                    entry_bar=st["pos"]["entry_bar"], ema20=ema20[i])
                # now_time=idx[i] 전달로 time_exit_minutes 도 백테스트에서 발화(live/paper 대칭).
                # time_exit_minutes=None 이면 minutes 분기는 건너뛰므로 기존 동작 무변경.
                reason = gen.evaluate_signal(pv, ExitContext(now_bar=i, now_time=idx[i]))
                if reason is not None:
                    pending_exit = {"reasons": [reason]}
            elif not is_trading_day_end(i):   # 오버나잇으로 넘어가는 진입 금지
                sig = strat.signal(symbol, ind, i, leverage_factor)
                if sig is not None:
                    decision = self._size_and_check(
                        symbol, c, leverage_factor, equity, sessions[i],
                        is_trading_day_end(i), daily_pnl_pct, cur_dd, st["consec"],
                        now_time=idx[i], last_exit_time=st["last_exit_time"],
                        last_exit_reason=st["last_exit_reason"])
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
    def _size_and_check(self, symbol, ref_price, lev, equity, session,
                        eod_window, daily_pnl_pct, cur_dd, consec,
                        now_time=None, last_exit_time=None, last_exit_reason=None):
        if ref_price <= 0 or equity <= 0:
            return None
        desired_notional = equity * self.cfg.entry_notional_pct
        qty = desired_notional / ref_price
        intent = OrderIntent(symbol, NEW_ENTRY, BUY, qty, ref_price, lev)
        snap = PortfolioSnapshot(
            account_value=equity, positions={},
            daily_pnl_pct=daily_pnl_pct, drawdown_pct=cur_dd,
            consecutive_losses=consec, kill_switch=False,
            session=session, is_eod_flatten_window=eod_window,
            now_time=now_time,
            last_exit_time=({symbol: last_exit_time} if last_exit_time is not None else {}),
            last_exit_reason=({symbol: last_exit_reason} if last_exit_reason else {}))
        return self.risk.evaluate(intent, snap)

    def _report(self, symbol, start, end, trades, equity_curve, bars_in_pos, cfg):
        summary = self._metrics(trades, equity_curve, bars_in_pos, cfg)
        return BacktestReport(symbol, start, end, summary, trades)

    def _metrics(self, trades, equity_curve, bars_in_pos, cfg) -> dict:
        n = len(trades)
        summary = performance_summary(cfg.initial_capital, equity_curve, trades)
        bars_total = len(equity_curve)
        summary["exposure_time"] = round(bars_in_pos / bars_total, 4) if bars_total else 0.0
        summary["sample_status"] = SAMPLE_INSUFFICIENT if n < cfg.min_trades else SAMPLE_OK
        return summary

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
