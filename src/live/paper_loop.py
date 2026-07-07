"""PaperTradingLoop — 실시간 시세 수집 → 5분봉 마감 시 run_once() 반복 (Paper 체결).

- MarketDataProvider(실제 TossMarketDataProvider)로 1분봉 수집.
- CandleStore 에 1분봉 저장 후 5분봉 리샘플.
- 새로 닫힌 5분봉에만 run_once() 실행, 동일 5분봉 중복 실행 방지.
- PaperBrokerProvider 만 사용(OrderManager 주입). 실주문 금지.
- 세션(PRE/REGULAR/POST/OVERNIGHT) 구분, 레버리지 ETF EOD flatten.
- 데이터/파이프라인 오류는 해당 사이클 SKIP/ERROR 로 처리, 루프는 계속(생존 우선).
- stop/pause/resume, kill switch 지원. 결과는 result_log 에 저장.
- tick() = 1회 폴링(테스트가 직접 호출). run() = tick 반복 드라이버(sleep 주입 가능).
"""
from __future__ import annotations

import time
from typing import Callable

from src.engines.indicator_engine import candles_to_frame, resample
from src.models import MARKET_US, SESSION_OVERNIGHT, SESSION_POST, Candle
from src.pipeline.models import SystemState
from src.ai.models import MarketContext
from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.risk.models import BUY, EOD_FLATTEN, SELL, OrderIntent
from src.universe.screener import leverage_for
from src.live.models import (
    R_DATA_ERROR,
    R_NO_CLOSED_5M,
    R_NO_NEW_BAR,
    R_OK,
    R_PAUSED,
    R_PIPELINE_ERROR,
    R_RATE_LIMITED,
    R_STOPPED,
    ST_ERROR,
    ST_RAN,
    ST_SKIPPED,
    ST_STOPPED,
    CycleReport,
    InMemoryResultLog,
    LoopConfig,
)


def frame_to_candles(df, symbol: str) -> list[Candle]:
    out: list[Candle] = []
    for ts, row in df.iterrows():
        out.append(Candle.from_moment(
            symbol, "5m", ts.to_pydatetime(),
            float(row["open"]), float(row["high"]), float(row["low"]),
            float(row["close"]), float(row["volume"]),
            str(row.get("currency", "USD")), True))
    return out


class PaperTradingLoop:
    def __init__(self, market, candle_store, pipeline, position_store,
                 config: LoopConfig | None = None, result_log=None,
                 journal=None, sleep: Callable[[float], None] = time.sleep):
        self.market = market
        self.store = candle_store
        self.pipeline = pipeline
        self.positions = position_store
        self.cfg = config or LoopConfig()
        self.results = result_log or InMemoryResultLog()
        self.journal = journal          # 선택: JournalStore (감사 로그)
        self._sleep = sleep
        # 자금관리(Portfolio): journal 이 있으면 이전 체결로 재구성(재시작 안전)
        self.account = PortfolioAccount(self.cfg.initial_capital)
        if journal is not None:
            try:
                from src.pnl.journal_pnl import fills_from_journal
                self.account = PortfolioAccount.from_fills(
                    self.cfg.initial_capital, fills_from_journal(journal))
            except Exception:
                self.account = PortfolioAccount(self.cfg.initial_capital)
        # 제어 플래그
        self.stopped = False
        self.paused = False
        self.kill_switch = False
        # 상태
        self._last_processed = None      # 마지막 처리한 5분봉 ts_utc
        self._calls = 0
        self._session_day = None

    # ---- 제어 (요구사항 13, 14) ----
    def stop(self) -> None: self.stopped = True
    def pause(self) -> None: self.paused = True
    def resume(self) -> None: self.paused = False
    def set_kill_switch(self, on: bool) -> None: self.kill_switch = on

    def run(self) -> None:
        while not self.stopped:
            if not self.paused:
                self.tick()
            self._sleep(self.cfg.polling_interval_s)

    # ---- 1회 폴링 사이클 ----
    def tick(self) -> CycleReport:
        if self.stopped:
            return self._save(CycleReport(None, ST_STOPPED, R_STOPPED))
        if self.paused:
            return self._save(CycleReport(None, ST_SKIPPED, R_PAUSED))

        # 1) 시세 수집 — 종목별 격리(한 종목 실패가 전체 사이클을 막지 않음)
        raw: dict[str, list[Candle]] = {}
        data_errors: dict[str, str] = {}
        for s in self.cfg.universe:
            try:
                raw[s] = self.market.recent(s, "1m", self.cfg.recent_1m_count)
            except Exception as e:
                raw[s] = []
                data_errors[s] = f"{type(e).__name__}: {e}"
            if self.cfg.fetch_spacing_s:            # 429 완화용 종목간 간격
                self._sleep(self.cfg.fetch_spacing_s)
        # 전 종목 실패면 사이클 자체를 SKIP
        if data_errors and all(not raw[s] for s in self.cfg.universe):
            return self._save(CycleReport(None, ST_SKIPPED, R_DATA_ERROR,
                                          error="; ".join(f"{k}:{v}" for k, v in data_errors.items())))

        # 2) 1분봉 저장 + 5분봉 리샘플
        #    (저장소 예외는 runtime 이 처리: SQLite lock 재시도 / 기타 예외 흡수+카운트)
        candles_5m: dict[str, list[Candle]] = {}
        for sym, cs in raw.items():
            if cs:
                self.store.upsert_many(cs)
            allc = self.store.query(sym, "1m")
            df5 = resample(candles_to_frame(allc), "5min")
            candles_5m[sym] = frame_to_candles(df5, sym)

        # 3) 새로 닫힌 5분봉 판정 (가장 최근 candle 자체가 market-correct 세션을 안다)
        newest_candle = None
        for cs in candles_5m.values():
            if cs and (newest_candle is None or cs[-1].ts_utc > newest_candle.ts_utc):
                newest_candle = cs[-1]
        if newest_candle is None:
            return self._save(CycleReport(None, ST_SKIPPED, R_NO_CLOSED_5M))
        newest = newest_candle.ts_utc
        cycle_id = newest.strftime("%Y%m%dT%H%M%SZ")

        # 4) 중복 실행 방지
        if self._last_processed is not None and newest <= self._last_processed:
            return self._save(CycleReport(cycle_id, ST_SKIPPED, R_NO_NEW_BAR))

        session = newest_candle.session                        # market-correct (US/KR)
        self._maybe_reset_session(newest_candle.ts_exchange)

        # 5) rate limit 보호
        if (self.cfg.max_calls_per_session is not None
                and self._calls >= self.cfg.max_calls_per_session):
            self._last_processed = newest
            return self._save(CycleReport(cycle_id, ST_SKIPPED, R_RATE_LIMITED, session))

        # 6) 포트폴리오/EOD flatten/시스템상태 구성
        last_prices = {s: cs[-1].close for s, cs in candles_5m.items() if cs}
        eod = self._is_eod_flatten_window(newest_candle)
        portfolio = self._portfolio(last_prices, session, eod)
        exit_intents = self._eod_exits(last_prices) if eod else []
        system_state = SystemState(kill_switch=self.kill_switch)
        market = MarketContext(session=session, is_eod_flatten_window=eod)

        # 7) run_once (파이프라인 내부 오류는 WAIT/SKIP, 예외는 여기서 흡수)
        try:
            result = self.pipeline.run_once(
                cycle_id=cycle_id, universe=list(self.cfg.universe),
                candles_by_symbol=candles_5m, portfolio=portfolio,
                system_state=system_state, market=market, exit_intents=exit_intents)
        except Exception as e:
            self._last_processed = newest
            self._journal_error(cycle_id, session, str(e))
            return self._save(CycleReport(cycle_id, ST_ERROR, R_PIPELINE_ERROR, session, error=str(e)))

        self._calls += 1
        self._last_processed = newest
        err = "; ".join(f"{k}:{v}" for k, v in data_errors.items()) or None
        self._apply_fills(cycle_id, result)
        self._journal_cycle(cycle_id, session, result, portfolio, market, err)
        return self._save(CycleReport(cycle_id, ST_RAN, R_OK, session,
                                      pipeline_result=result.to_dict(), error=err))

    def _apply_fills(self, cycle_id, result) -> None:
        """이번 사이클의 FILLED 체결을 자금관리 계좌(cash/FIFO)에 반영(다음 사이클 스냅샷 정확도)."""
        from src.journal.models import ts_from_cycle_id
        ts_str = ts_from_cycle_id(cycle_id)
        ts = None
        if ts_str:
            from datetime import datetime, timezone
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
        for o in result.outcomes:
            rec = o.order_record
            if not rec or rec.get("status") != "FILLED":
                continue
            side = rec.get("side")
            qty = rec.get("filled_quantity") or 0.0
            if side not in (BUY, SELL) or qty <= 0:
                continue
            self.account.apply_fill(FillEvent(
                symbol=rec.get("symbol"), side=side, quantity=float(qty),
                price=float(rec.get("avg_fill_price") or 0.0), fee=float(rec.get("fee") or 0.0),
                ref_price=rec.get("ref_price"), timestamp=ts, reason=(rec.get("kind"),) if rec.get("kind") else ()))

    def _journal_cycle(self, cycle_id, session, result, portfolio, market, error=None) -> None:
        if self.journal is None:
            return
        from src.journal.models import JournalCycle
        self.journal.save_cycle(JournalCycle.from_pipeline(
            cycle_id, session, list(self.cfg.universe), result,
            portfolio=portfolio, market=market, error=error))

    def _journal_error(self, cycle_id, session, error) -> None:
        if self.journal is None:
            return
        from src.journal.models import JournalCycle
        self.journal.save_cycle(JournalCycle.error_cycle(
            cycle_id, list(self.cfg.universe), error, session=session))

    # ---- helpers ----
    def _save(self, report: CycleReport) -> CycleReport:
        self.results.save(report)
        return report

    def _maybe_reset_session(self, ts_et) -> None:
        day = ts_et.date()
        if self._session_day != day:
            self._session_day = day
            self._calls = 0

    def _is_eod_flatten_window(self, candle) -> bool:
        # 레버리지 EOD flatten 은 US 종목 정책. KR 등 타 시장은 대상 아님.
        if candle.market != MARKET_US:
            return False
        ts_et, session = candle.ts_exchange, candle.session
        if session == SESSION_OVERNIGHT:
            return True
        if session == SESSION_POST:
            mins_to_close = self.cfg.extended_close_et_min - (ts_et.hour * 60 + ts_et.minute)
            return 0 <= mins_to_close <= self.cfg.eod_flatten_before_min
        return False

    def _portfolio(self, last_prices, session, eod):
        from src.risk.models import PortfolioSnapshot, Position
        self.account.mark_equity(last_prices, day_key=None)
        positions = {}
        for sym, p in self.account.positions().items():
            lev = leverage_for(sym)
            positions[sym] = Position(sym, p["quantity"], p["avg_price"],
                                      last_prices.get(sym, p["avg_price"]), lev)
        return PortfolioSnapshot(
            account_value=self.account.last_equity, positions=positions,
            cash=self.account.cash, drawdown_pct=self.account.last_drawdown_pct,
            daily_pnl_pct=self.account.last_daily_return_pct,
            kill_switch=self.kill_switch, session=session, is_eod_flatten_window=eod)

    def _eod_exits(self, last_prices) -> list[OrderIntent]:
        out = []
        for sym, p in self.positions.all().items():
            lev = leverage_for(sym)
            if lev > 1 and p["quantity"] > 0:
                out.append(OrderIntent(sym, EOD_FLATTEN, SELL, p["quantity"],
                                       last_prices.get(sym, p["avg_price"]), lev))
        return out
