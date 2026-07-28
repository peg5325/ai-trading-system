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

import logging
import time
from typing import Callable

from collections import defaultdict

from src.engines.indicator_engine import (candles_to_frame,
                                          frame_to_candles as _frame_to_candles,
                                          resample)
from src.exits.generator import ExitContext, ExitSignalGenerator, PositionView
from src.market_context.builder import build_benchmark_context
from src.shadow.decider import build_shadow_record
from src.models import Candle, in_eod_flatten_window
from src.pipeline.models import SystemState
from src.ai.models import MarketContext
from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.risk.models import BUY, SELL
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


logger = logging.getLogger(__name__)

_BAR_SECONDS = 300      # 5분봉. time_exit_bars 를 타임스탬프에서 유도(재시작 안전).


def _bar_index(ts) -> int | None:
    """UTC 타임스탬프 → 결정론적 5분봉 인덱스(절대시간 기반). time_exit_bars 용."""
    return None if ts is None else int(ts.timestamp() // _BAR_SECONDS)


def frame_to_candles(df, symbol: str) -> list[Candle]:
    """리샘플 프레임 → 결정봉 Candle.

    구현은 `indicator_engine.frame_to_candles` 단일 소스로 이관됐다(이슈 #76). 연구
    (`candidate_stream`)·replay 도 같은 함수를 쓰므로 세 경로의 변환이 구조적으로 동일하다.
    이 이름은 기존 호출부 호환을 위해 유지한다.
    """
    return _frame_to_candles(df, symbol)


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
        # 청산 신호 생성기(순수 코드, AI 무관). live/paper/backtest 공유 엔진.
        self.exit_gen = ExitSignalGenerator(self.cfg.exit_config)
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
        self._observed_bars: list = []   # 이번 세션 관측한 5분봉 ts_exchange(coverage용, 관측만)
        # 재진입 쿨다운용 종목별 마지막 청산(시각/사유). 시작 시 계좌 실현거래로 복원(재시작 안전).
        self._last_exit_time: dict = {}
        self._last_exit_reason: dict[str, str] = {}
        self._rebuild_last_exit()

    def _rebuild_last_exit(self) -> None:
        """계좌 실현거래에서 종목별 '마지막' 청산(시각/사유) 복원(재진입 쿨다운, 재시작 안전).
        timestamp 기반이라 세션내 재시작 시 쿨다운이 그대로 이어진다(세션 전환 시 초기화)."""
        try:
            for t in self.account.realized_trades():
                et = getattr(t, "exit_time", None)
                reason = t.exit_reason[0] if getattr(t, "exit_reason", None) else None
                if et is None or reason is None:
                    continue
                prev = self._last_exit_time.get(t.symbol)
                if prev is None or et >= prev:
                    self._last_exit_time[t.symbol] = et
                    self._last_exit_reason[t.symbol] = reason
        except Exception:
            pass

    # ---- session coverage 관측 (매매 무관, PR 1-B) ----
    def bar_minutes(self) -> float:
        return _BAR_SECONDS / 60.0

    def observed_bar_exchange_times(self) -> list:
        """이번 세션에서 관측한 5분봉의 거래소 로컬 시각(coverage 계산 입력)."""
        return list(self._observed_bars)

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
        latest_1m: dict[str, Candle] = {}
        for sym, cs in raw.items():
            if cs:
                self.store.upsert_many(cs)
            allc = self.store.query(sym, "1m")
            if allc:
                latest_1m[sym] = allc[-1]
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
        # 새로 닫힌 봉을 관측봉으로 기록(데이터 가용성 기준 — 이후 rate-limit/error 여부 무관)
        self._observed_bars.append(newest_candle.ts_exchange)

        # 5) rate limit 보호
        if (self.cfg.max_calls_per_session is not None
                and self._calls >= self.cfg.max_calls_per_session):
            self._last_processed = newest
            return self._save(CycleReport(cycle_id, ST_SKIPPED, R_RATE_LIMITED, session))

        # 6) 포트폴리오/EOD flatten/시스템상태 구성
        last_prices = {s: cs[-1].close for s, cs in candles_5m.items() if cs}
        mins_before = self.cfg.exit_config.eod_flatten_minutes_before_close
        # 포트폴리오 스냅샷/MarketContext 용 대표 eod(최근 캔들 기준, market-correct).
        # 청산 판단은 아래 _exit_intents 에서 symbol 별 candle 로 개별 계산한다(혼합 유니버스 정확).
        eod = in_eod_flatten_window(newest_candle, mins_before)
        portfolio = self._portfolio(last_prices, session, eod, now_time=newest)
        # 청산 신호는 매 사이클 평가(SL/TP/Time 은 마감 창 밖에서도 발동, EOD 는 각 시장 마감 창에서).
        exit_intents = self._exit_intents(
            candles_5m, newest_candle, timing_candles_1m=latest_1m)
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
        # 국면 관측(PR 2-B) — run_once '이후'에만 만든다(구조적으로 매매에 영향 불가).
        benchmark_ctx = self._benchmark_context(candles_5m, newest)
        # Shadow 결정(PR 4-A) — 역시 run_once '이후', benchmark_ctx 재사용(가상 결정만 기록).
        shadow = self._shadow_decision(benchmark_ctx, result, newest)
        self._journal_cycle(cycle_id, session, result, portfolio, market, err,
                            benchmark_ctx, shadow)
        return self._save(CycleReport(cycle_id, ST_RAN, R_OK, session,
                                      pipeline_result=result.to_dict(), error=err))

    def _benchmark_context(self, candles_5m, decision_time) -> dict | None:
        """벤치마크 국면 관측(PR 2-B). config off / benchmark 미보유 / 실패 → None.
        이미 수집한 candle 만 재사용(추가 fetch 0 → 호출예산 불변). 실패해도 매매/저널 무중단."""
        mc = self.cfg.market_context
        if not getattr(mc, "enabled", False):
            return None
        candles = candles_5m.get(mc.benchmark_symbol)
        if not candles:                     # universe 에 없으면 관측 생략(별도 fetch 금지)
            return None
        try:
            ctx = build_benchmark_context(
                candles, decision_time=decision_time, symbol=mc.benchmark_symbol,
                timeframe=mc.timeframe, ema_period=mc.ema_period)
        except Exception:
            return None                     # 관측 실패가 거래를 게이팅하지 않는다
        return ctx.to_dict() if ctx is not None else None

    def _shadow_decision(self, benchmark_ctx, result, decision_time) -> dict | None:
        """Online Shadow Decision(PR 4-A). config off → None. run_once '이후' 생성이라
        구조적으로 매매 무영향. benchmark_ctx(2-B 산출)를 그대로 소비 → Paper=Backtest 국면
        패리티. 실패해도 매매/저널 무중단(관측이 거래를 게이팅 금지). 미래 outcome 미기록."""
        sh = getattr(self.cfg, "shadow", None)
        if not getattr(sh, "enabled", False):
            return None
        try:
            cand_ids = [o.candidate_id for o in result.outcomes if o.candidate_id]
            return build_shadow_record(benchmark_ctx, decision_time=decision_time,
                                       candidate_ids=cand_ids)
        except Exception as e:
            # 관측 유실 진단용 로그(생존 우선 — 거래는 게이팅하지 않는다).
            logger.debug("shadow decision skipped: %s", e)
            return None

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
            # 청산(SELL) 체결 → 종목별 마지막 청산(시각/사유) 갱신(재진입 쿨다운용).
            if side == SELL and ts is not None and rec.get("kind"):
                self._last_exit_time[rec.get("symbol")] = ts
                self._last_exit_reason[rec.get("symbol")] = rec.get("kind")

    def _journal_cycle(self, cycle_id, session, result, portfolio, market,
                       error=None, benchmark_context=None, shadow=None) -> None:
        if self.journal is None:
            return
        from src.journal.models import JournalCycle
        self.journal.save_cycle(JournalCycle.from_pipeline(
            cycle_id, session, list(self.cfg.universe), result,
            portfolio=portfolio, market=market, error=error,
            benchmark_context=benchmark_context, shadow=shadow))

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
            prev = self._session_day
            self._session_day = day
            self._calls = 0
            self._observed_bars = []          # 새 세션 → 관측봉 초기화
            # 재진입 쿨다운: '실제' 세션 전환(첫 확립 제외)에서만 초기화 → 재시작 복원은 보존.
            if prev is not None and self.cfg.reentry_cooldown.reset_on_new_session:
                self._last_exit_time.clear()
                self._last_exit_reason.clear()

    def _portfolio(self, last_prices, session, eod, now_time=None):
        from src.risk.models import PortfolioSnapshot, Position
        self.account.mark_equity(last_prices, day_key=None)
        positions = {}
        for sym, p in self.account.positions().items():
            lev = leverage_for(sym)
            positions[sym] = Position(sym, p["quantity"], p["avg_price"],
                                      last_prices.get(sym, p["avg_price"]), lev)
        # now_time 은 유니버스 전역 최신봉 시각(단일 스냅샷). 자기 캔들이 stale 한 종목은 경과가
        # 소폭 과대 → 쿨다운이 조금 일찍 만료될 수 있으나 fail-open(진입 허용)이고 봉/fetch 간격으로 유계.
        return PortfolioSnapshot(
            now_time=now_time, last_exit_time=dict(self._last_exit_time),
            last_exit_reason=dict(self._last_exit_reason),
            account_value=self.account.last_equity, positions=positions,
            cash=self.account.cash, drawdown_pct=self.account.last_drawdown_pct,
            daily_pnl_pct=self.account.last_daily_return_pct,
            kill_switch=self.kill_switch, session=session, is_eod_flatten_window=eod)

    def _open_positions_view(
            self,
            candles_5m,
            timing_candles_1m: dict[str, Candle] | None = None,
    ) -> list[PositionView]:
        """자금관리 계좌(FIFO 롯)를 종목별 PositionView 로 집계.
        진입가는 롯 가중평균, 보유시간은 가장 오래된(FIFO) 롯 기준. account 는 재시작 시
        journal 로 복원되므로 재시작 후에도 청산 대상 포지션을 정확히 인식한다.
        현재가는 각 종목의 마지막 닫힌 5분봉을 사용한다. EOD 창은 가격과 분리해 각 종목의
        최신 1분봉 시각으로 계산한다(#44). 이로써 대표 종목의 5분봉은 닫혔지만 보유 종목의
        같은 버킷은 아직 미완결인 skew 에서도 청산창을 놓치지 않는다. 직접 helper 를 호출하는
        기존 경로는 1분봉 시계가 없으면 종목별 최신 5분봉으로 하위호환한다."""
        mins_before = self.cfg.exit_config.eod_flatten_minutes_before_close
        lots_by_sym: dict[str, list] = defaultdict(list)
        for lot in self.account.open_lots():
            lots_by_sym[lot.symbol].append(lot)
        views: list[PositionView] = []
        for sym, lots in lots_by_sym.items():
            qty = sum(l.quantity for l in lots)
            if qty <= 0:
                continue
            entry_price = sum(l.quantity * l.entry_price for l in lots) / qty
            entry_times = [l.entry_time for l in lots if l.entry_time is not None]
            oldest_entry = min(entry_times) if entry_times else None
            cs = candles_5m.get(sym) or []
            sym_candle = cs[-1] if cs else None
            current_price = sym_candle.close if sym_candle is not None else entry_price
            timing_candle = (
                timing_candles_1m.get(sym)
                if timing_candles_1m is not None
                else sym_candle
            )
            is_eod = (
                in_eod_flatten_window(timing_candle, mins_before)
                if timing_candle is not None
                else False
            )
            views.append(PositionView(
                symbol=sym, quantity=qty, entry_price=entry_price,
                current_price=current_price,
                leverage_factor=leverage_for(sym),
                entry_time=oldest_entry,
                # bar_index 는 fill 에 저장되지 않으므로 진입 시각에서 5분봉 인덱스를 유도
                # (time_exit_bars 가 paper 에서도 동작하도록; 절대시간 기반이라 재시작 안전).
                entry_bar=_bar_index(oldest_entry),
                is_eod_window=is_eod))          # symbol별 market 마감 창
        return views

    def _exit_intents(
            self,
            candles_5m,
            newest_candle,
            timing_candles_1m: dict[str, Candle] | None = None,
    ):
        """보유 포지션에 대한 청산 OrderIntent 생성(SL/TP/Time/EOD). AI 우회, Risk→Order 경로.
        EOD 창은 종목별로 PositionView.is_eod_window 에 담겨 있으므로 ctx 는 시간정보만 전달."""
        views = self._open_positions_view(
            candles_5m, timing_candles_1m=timing_candles_1m)
        if not views:
            return []
        ctx = ExitContext(is_eod_window=False, now_time=newest_candle.ts_utc,
                          now_bar=_bar_index(newest_candle.ts_utc))
        return self.exit_gen.generate_intents(views, ctx)
