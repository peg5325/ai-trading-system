"""ExitDatasetBuilder — Journal(청산) + Candle 을 읽어 청산 사후 관측 Dataset 을 만드는 batch.

Runtime 과 완전히 분리된 read-only 배치. 매매 로직(Entry/Exit/Risk/AI/Loop/Pipeline)에
일절 관여하지 않는다. RealizedTrade(FIFO 청산 단위)마다 청산 이후 시장 움직임을 관측한다.

Look-ahead 방지(핵심):
- 모든 forward/eval/label 계산은 exit_time '초과'(strict) 봉만 사용한다(bisect_right).
- 세션(거래일) 경계를 넘는 봉은 사용하지 않는다 — 같은 거래소 날짜(ts_exchange.date())로 clip.
- horizon 이 세션 종료를 넘으면 price=None + complete=False(INCOMPLETE).

결정론적: 입력이 같으면 출력이 같다(난수/현재시각 미사용; built_at 은 외부 주입).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.dataset.exit_models import (
    ExitDatasetMeta,
    ExitFollowupDataset,
    ExitFollowupRecord,
    ExitForwardPoint,
    iso,
)

_EPS = 1e-12
_SL = "STOP_LOSS"
_TP = "TAKE_PROFIT"


@dataclass
class ExitLabelConfig:
    """청산 사후 관측 파라미터."""
    horizons_min: tuple[int, ...] = (5, 15, 30, 60)   # forward price/return horizon(분)
    eval_window_min: int = 60                          # MFE/MAE/best/worst 창(분, 세션말로 clip)
    recover_window_min: int = 30                       # STOP_LOSS 회복 판정 창(분)
    recover_threshold: float = 0.0                     # '진입가' 대비 상방 임계(0=진입가 회복)
    continue_window_min: int = 30                      # TAKE_PROFIT 추가상승 판정 창(분)
    continue_threshold: float = 0.003                  # '청산가' 대비 유의미 추가상승(기본 +0.3%)

    def sorted_horizons(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.horizons_min)))


class ExitDatasetBuilder:
    def __init__(self, candle_store=None, *,
                 config: ExitLabelConfig | None = None,
                 interval: str = "1m",
                 built_at: str | None = None):
        self._store = candle_store
        self.cfg = config or ExitLabelConfig()
        self._interval = interval
        self._built_at = built_at
        # symbol -> (ts_utc list asc, Candle list asc) 캐시
        self._candle_cache: dict[str, tuple[list[datetime], list]] = {}

    # ---- public ----
    def build(self, trades, *, session_id: str | None = None,
              source: str | None = None) -> ExitFollowupDataset:
        """RealizedTrade(청산 단위) 리스트 → ExitFollowupDataset. read-only."""
        records: list[ExitFollowupRecord] = []
        for t in trades:
            if getattr(t, "exit_time", None) is None:
                continue
            rec = self._record(t, session_id)
            if rec is not None:
                records.append(rec)
        meta = ExitDatasetMeta(
            session_id=session_id, interval=self._interval,
            horizons_min=self.cfg.sorted_horizons(),
            eval_window_min=self.cfg.eval_window_min,
            source=source, built_at=self._built_at)
        return ExitFollowupDataset(records=records, meta=meta)

    # ---- per-exit ----
    def _record(self, t, session_id: str | None) -> ExitFollowupRecord | None:
        symbol = t.symbol
        exit_time: datetime = t.exit_time
        exit_price = _f(t.exit_price)
        reason = _reason(t.exit_reason)
        ts_list, candles = self._candles(symbol)

        # 청산 봉 → 거래소 날짜/market 확정 (look-ahead clip 기준)
        exit_idx = bisect_right(ts_list, exit_time) - 1
        exit_bar = candles[exit_idx] if 0 <= exit_idx < len(candles) else None
        exit_date: date | None = exit_bar.ts_exchange.date() if exit_bar else None
        market = getattr(exit_bar, "market", None) if exit_bar else None

        # 세션(거래일) 마감 봉 + 청산 이후 같은 날 봉만(look-ahead & 세션 clip)
        day_bars = [c for c in candles if exit_date is not None
                    and c.ts_exchange.date() == exit_date]
        session_end_bar = day_bars[-1] if day_bars else None
        post = [c for c in day_bars if c.ts_utc > exit_time]     # strict: 청산 이후만
        sess_end_ts = session_end_bar.ts_utc if session_end_bar else None

        rec = ExitFollowupRecord(
            session_id=session_id, symbol=symbol, market=market, side="SELL",
            entry_time=iso(getattr(t, "entry_time", None)), exit_time=iso(exit_time),
            holding_minutes=_f(getattr(t, "holding_minutes", None)),
            holding_bars=getattr(t, "holding_bars", None),
            entry_price=_f(getattr(t, "entry_price", None)), exit_price=exit_price,
            exit_reason=reason, eval_window_min=self.cfg.eval_window_min,
        )

        # forward price/return (+5/+15/+30/+60), 세션 초과 horizon = INCOMPLETE(None)
        for h in self.cfg.sorted_horizons():
            rec.forwards.append(self._forward_point(exit_time, exit_price, post,
                                                    sess_end_ts, h))

        # session end
        if session_end_bar is not None and exit_price:
            rec.session_end_time = iso(session_end_bar.ts_utc)
            rec.session_end_price = _f(session_end_bar.close)
            rec.session_end_return = (session_end_bar.close - exit_price) / exit_price
            # 청산 이후 관측된 마감가인가(청산이 마지막 봉이면 확인 불가 → False)
            rec.session_end_complete = session_end_bar.ts_utc > exit_time

        # MFE/MAE/best/worst (eval 창, 세션말로 clip)
        self._apply_eval(rec, exit_time, exit_price, post, sess_end_ts)

        # Replay AI labels (사유별로만 채움; 나머지는 None=N/A)
        self._apply_labels(rec, exit_time, exit_price, _f(getattr(t, "entry_price", None)),
                           reason, post)
        return rec

    def _forward_point(self, exit_time, exit_price, post, sess_end_ts,
                       horizon_min) -> ExitForwardPoint:
        target = exit_time + timedelta(minutes=horizon_min)
        # 세션 종료를 넘는 horizon → INCOMPLETE
        if (exit_price is None or not exit_price or sess_end_ts is None
                or target > sess_end_ts):
            return ExitForwardPoint(horizon_min, iso(target), None, None, False)
        win = [c for c in post if c.ts_utc <= target]          # (exit, target] 같은 날
        if not win:
            return ExitForwardPoint(horizon_min, iso(target), None, None, False)
        price = win[-1].close                                  # target 이하 마지막 close
        return ExitForwardPoint(
            horizon_min=horizon_min, target_time=iso(target), price=_f(price),
            forward_return=(price - exit_price) / exit_price, complete=True)

    def _apply_eval(self, rec, exit_time, exit_price, post, sess_end_ts) -> None:
        if exit_price is None or not exit_price:
            return
        eval_end = exit_time + timedelta(minutes=self.cfg.eval_window_min)
        win = [c for c in post if c.ts_utc <= eval_end]        # 같은 날 + eval 창
        rec.eval_n_bars = len(win)
        if not win:
            return
        best = max(c.high for c in win)
        worst = min(c.low for c in win)
        rec.best_price_after_exit = _f(best)
        rec.worst_price_after_exit = _f(worst)
        rec.mfe = (best - exit_price) / exit_price             # 상방(놓친 이익)
        rec.mae = (worst - exit_price) / exit_price            # 하방(피한 손실)
        # eval 창 전체가 세션 안에서 완결되었는가
        rec.eval_complete = sess_end_ts is not None and eval_end <= sess_end_ts

    def _apply_labels(self, rec, exit_time, exit_price, entry_price, reason, post) -> None:
        if exit_price is None or not exit_price:
            return
        if reason == _SL:
            # 손절이 너무 일렀는가 = 청산 후 가격이 '진입가' 수준까지 되돌렸는가.
            base = entry_price if entry_price else exit_price
            rec.did_price_recover_after_stop_loss = self._high_exceeds(
                exit_time, post, self.cfg.recover_window_min,
                base * (1.0 + self.cfg.recover_threshold))
        elif reason == _TP:
            # 익절이 너무 일렀는가 = 청산 후 '청산가' 대비 유의미(+threshold) 추가상승했는가.
            rec.did_price_continue_after_take_profit = self._high_exceeds(
                exit_time, post, self.cfg.continue_window_min,
                exit_price * (1.0 + self.cfg.continue_threshold))

    def _high_exceeds(self, exit_time, post, window_min, bar_price) -> bool | None:
        """청산 후 window 내 고가가 bar_price 를 초과했는가. 데이터 없으면 None."""
        end = exit_time + timedelta(minutes=window_min)
        win = [c for c in post if c.ts_utc <= end]
        if not win:
            return None
        return any(c.high > bar_price + _EPS for c in win)

    # ---- candle cache (read-only) ----
    def _candles(self, symbol: str):
        if symbol in self._candle_cache:
            return self._candle_cache[symbol]
        candles: list = []
        if self._store is not None:
            candles = self._store.query(symbol, self._interval)   # asc, read-only
        ts_list = [c.ts_utc for c in candles]
        self._candle_cache[symbol] = (ts_list, candles)
        return ts_list, candles


# ---- convenience: journal → exit dataset ----
def build_exit_dataset_from_journal(journal, candle_store, *, session_id: str | None = None,
                                    config: ExitLabelConfig | None = None,
                                    interval: str = "1m", bar_minutes: float = 5.0,
                                    built_at: str | None = None,
                                    source: str | None = None) -> ExitFollowupDataset:
    """Journal(FILLED) → FIFO 청산 → ExitFollowupDataset. read-only 배치 진입점."""
    from src.pnl.journal_pnl import realized_from_journal
    trades, _, _ = realized_from_journal(journal, bar_minutes=bar_minutes)
    builder = ExitDatasetBuilder(candle_store, config=config, interval=interval,
                                 built_at=built_at)
    return builder.build(trades, session_id=session_id, source=source)


# ---- helpers ----
def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reason(exit_reason) -> str | None:
    """RealizedTrade.exit_reason(tuple) → 대표 사유 문자열."""
    if not exit_reason:
        return None
    if isinstance(exit_reason, (tuple, list)):
        return exit_reason[0] if exit_reason else None
    return str(exit_reason)
