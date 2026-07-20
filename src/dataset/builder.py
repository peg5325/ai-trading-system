"""DatasetBuilder — Journal(+Candle) 을 읽어 학습/분석용 Dataset 을 만드는 batch processor.

원칙:
- read-only. Journal/Candle 을 절대 변경하지 않는다. 실시간 매매에 관여하지 않는다.
- Replay/Review/Runtime 은 Dataset 을 모른다. Dataset 은 오직 여기서 조립된다.
- 결정론적: 입력이 같으면 출력이 같다(난수/시각 미사용; built_at 은 외부에서 주입).

한 관찰 row = 한 cycle×symbol 의 진입 평가 지점.
  feature = 진입 시점 상태(지표/후보/AI/포트폴리오)
  label   = 사후 forward return / MFE / MAE / win-loss-neutral (Candle 로 계산)
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.dataset.models import (
    SCORE_COMPONENT_FIELDS,
    SCORE_COMPONENT_NAMES,
    Dataset,
    DatasetRecord,
    FeatureRecord,
    ForwardMetric,
    LabelRecord,
    MetaRecord,
)
from src.models import market_from_currency

_DEFAULT_STRATEGY_VERSION = "screener-1.0"


@dataclass
class LabelConfig:
    """forward label 계산 파라미터."""
    windows_min: tuple[int, ...] = (5, 15, 30, 60)   # forward return 창(분)
    primary_window_min: int = 30                     # 대표 라벨 창
    win_threshold: float = 0.002                     # +0.2% 이상 → WIN
    loss_threshold: float = 0.002                    # -0.2% 이하 → LOSS
    require_entry_price: bool = True                 # 진입가 없는 관찰은 제외
    drop_incomplete_labels: bool = False             # primary 창 미충족 row 제외 여부

    def sorted_windows(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.windows_min) | {self.primary_window_min}))


def _parse_cycle_ts(cycle_ts: str | None) -> datetime | None:
    """'2026-07-07T04:15:00Z' → aware UTC datetime."""
    if not cycle_ts:
        return None
    try:
        return datetime.strptime(cycle_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_entry_observation(o: dict) -> bool:
    """진입 평가가 일어난 관찰인가(청산/무데이터 outcome 제외)."""
    return any(o.get(k) is not None for k in ("indicator_snapshot", "candidate", "ai_decision"))


class DatasetBuilder:
    def __init__(self, journal_store, candle_store=None, *,
                 config: LabelConfig | None = None,
                 interval: str = "1m",
                 strategy_version: str = _DEFAULT_STRATEGY_VERSION,
                 built_at: str | None = None):
        self._journal = journal_store
        self._store = candle_store
        self.cfg = config or LabelConfig()
        self._interval = interval
        self._strategy_version = strategy_version
        self._built_at = built_at
        # symbol -> (ts_utc list asc, Candle list asc) 캐시
        self._candle_cache: dict[str, tuple[list[datetime], list]] = {}

    # ---- public ----
    def build(self, *, start: datetime | None = None, end: datetime | None = None,
              symbols: set[str] | None = None, source: str | None = None) -> Dataset:
        cycles = self._journal.get_by_time_range(start, end)   # cycle_ts 오름차순
        records: list[DatasetRecord] = []
        for cycle in cycles:
            as_of = _parse_cycle_ts(cycle.cycle_ts)
            for o in cycle.outcomes:
                sym = o.get("symbol")
                if not sym or (symbols is not None and sym not in symbols):
                    continue
                if not _is_entry_observation(o):
                    continue
                rec = self._record(cycle, o, sym, as_of, source)
                if rec is None:
                    continue
                records.append(rec)
        meta = MetaRecord(strategy_version=self._strategy_version,
                          built_at=self._built_at, source=source)
        return Dataset(records=records, meta=meta)

    # ---- per-observation ----
    def _record(self, cycle, o: dict, symbol: str, as_of: datetime | None,
                source: str | None) -> DatasetRecord | None:
        snap = o.get("indicator_snapshot") or {}
        cand = o.get("candidate") or {}
        feats = cand.get("features") or {}
        bench = getattr(cycle, "benchmark_context", None) or {}   # 사이클 국면(PR 2-B)
        ai = o.get("ai_decision") or {}
        risk = o.get("risk_decision") or {}
        order = o.get("order_record")
        ts_list, candles = self._candles(symbol)

        # 진입가: 지표 스냅샷 close 우선, 없으면 as_of 이하 최근 캔들 close
        entry_price = snap.get("close")
        market = session = None
        if candles and as_of is not None:
            idx = bisect_right(ts_list, as_of) - 1     # as_of 이하 마지막 캔들
            if idx >= 0:
                c = candles[idx]
                market = getattr(c, "market", None)
                session = c.session
                if entry_price is None:
                    entry_price = c.close
        if market is None:
            market = _market_from_cycle(cycle)
        if session is None:
            session = cycle.session

        if entry_price is None and self.cfg.require_entry_price:
            return None

        feature = FeatureRecord(
            cycle_id=cycle.cycle_id, cycle_ts=cycle.cycle_ts, symbol=symbol,
            market=market, session=session,
            action=o.get("action"), reason=o.get("reason"),
            close=_f(snap.get("close")), ema7=_f(snap.get("ema7")),
            ema20=_f(snap.get("ema20")), rsi14=_f(snap.get("rsi14")),
            vwap=_f(snap.get("vwap")), bb_upper=_f(snap.get("bb_upper")),
            bb_lower=_f(snap.get("bb_lower")), stochrsi_k=_f(snap.get("stochrsi_k")),
            stochrsi_d=_f(snap.get("stochrsi_d")),
            candidate_score=_f(cand.get("score")), candidate_type=cand.get("candidate_type"),
            candidate_rank=cand.get("rank"),
            risk_flags=list(cand.get("risk_flags") or []),
            ai_decision=ai.get("decision"), ai_confidence=ai.get("confidence_score"),
            ai_entry_quality=ai.get("entry_quality"), ai_risk_level=ai.get("risk_level"),
            risk_allowed=risk.get("allowed"),
            account_value=_f((cycle.portfolio or {}).get("account_value")),
            open_positions=_open_positions(cycle.portfolio),
            kill_switch=(cycle.portfolio or {}).get("kill_switch"),
            # v2.1 계측(§2-2): 조인키 + score 분해 + volume (없으면 None)
            candidate_id=o.get("candidate_id"), trade_id=o.get("trade_id"),
            weighted_component_sum=_f(feats.get("weighted_component_sum")),
            penalty_score=_f(feats.get("penalty_score")),
            pre_clamp_score=_f(feats.get("pre_clamp_score")),
            # PR A(H2): 컴포넌트 개별 점수. 하드 제외(features={}) 경로에서는 전부 None.
            **_score_component_fields(feats),
            volume=_f(feats.get("volume_last")), volume_sma20=_f(feats.get("volume_sma20")),
            volume_ratio=_f(feats.get("volume_ratio")),
            dollar_volume=_f(feats.get("dollar_volume")),
            # v2.1 PR 2-B: 사이클 benchmark 국면(없으면 None)
            benchmark_symbol=bench.get("benchmark_symbol"),
            benchmark_close=_f(bench.get("benchmark_close")),
            benchmark_ema20=_f(bench.get("benchmark_ema20")),
            benchmark_ema_period=bench.get("benchmark_ema_period"),
            benchmark_vs_ema20=_f(bench.get("benchmark_vs_ema20")),
            benchmark_ema20_slope=_f(bench.get("benchmark_ema20_slope")),
            benchmark_return_trailing_30m=_f(bench.get("benchmark_return_trailing_30m")),
            benchmark_return_trailing_60m=_f(bench.get("benchmark_return_trailing_60m")),
            benchmark_trend=bench.get("benchmark_trend"),
            benchmark_source_bar_end=bench.get("benchmark_source_bar_end"),
            benchmark_context_asof=bench.get("benchmark_context_asof"),
            benchmark_timeframe=bench.get("benchmark_timeframe"),
            market_context_version=bench.get("market_context_version"),
        )
        label = self._label(entry_price, as_of, ts_list, candles, order)
        if self.cfg.drop_incomplete_labels and not label.complete:
            return None

        row_meta = MetaRecord(
            prompt_version=ai.get("prompt_version") or (o.get("ai_log") or {}).get("prompt_version"),
            strategy_version=self._strategy_version,
            model_name=ai.get("model_name") or (o.get("ai_log") or {}).get("model_name"),
            built_at=self._built_at, source=source,
        )
        return DatasetRecord(feature=feature, label=label, meta=row_meta)

    # ---- forward label ----
    def _label(self, entry_price, as_of, ts_list, candles, order) -> LabelRecord:
        forwards: list[ForwardMetric] = []
        holding = 0
        for w in self.cfg.sorted_windows():
            fm = self._forward_metric(entry_price, as_of, ts_list, candles, w)
            forwards.append(fm)
            if fm.n_bars > 0:
                holding = max(holding, w)

        primary = next((f for f in forwards if f.window_min == self.cfg.primary_window_min), None)
        label_return = primary.forward_return if primary else None
        complete = bool(primary and primary.n_bars > 0)
        label_class = self._classify(label_return) if complete else None

        lab = LabelRecord(
            entry_price=_f(entry_price), forwards=forwards,
            label_window_min=self.cfg.primary_window_min,
            label_return=label_return, label_class=label_class,
            holding_minutes=holding if holding > 0 else None, complete=complete,
        )
        _apply_trade(lab, order)
        return lab

    def _forward_metric(self, entry_price, as_of, ts_list, candles, window_min) -> ForwardMetric:
        if entry_price is None or not entry_price or as_of is None or not candles:
            return ForwardMetric(window_min, 0, None, None, None, None)
        hi = as_of + timedelta(minutes=window_min)
        lo_idx = bisect_right(ts_list, as_of)          # as_of 초과 첫 캔들 (look-ahead 방지)
        hi_idx = bisect_right(ts_list, hi)             # as_of+w 이하 마지막 다음
        window = candles[lo_idx:hi_idx]
        if not window:
            return ForwardMetric(window_min, 0, None, None, None, None)
        end_price = window[-1].close
        max_high = max(c.high for c in window)
        min_low = min(c.low for c in window)
        return ForwardMetric(
            window_min=window_min, n_bars=len(window),
            forward_return=(end_price - entry_price) / entry_price,
            mfe=(max_high - entry_price) / entry_price,
            mae=(min_low - entry_price) / entry_price,
            end_price=end_price,
        )

    def _classify(self, r: float | None) -> str | None:
        if r is None:
            return None
        if r >= self.cfg.win_threshold:
            return "WIN"
        if r <= -self.cfg.loss_threshold:
            return "LOSS"
        return "NEUTRAL"

    # ---- candle cache ----
    def _candles(self, symbol: str):
        if symbol in self._candle_cache:
            return self._candle_cache[symbol]
        candles: list = []
        if self._store is not None:
            candles = self._store.query(symbol, self._interval)   # read-only, asc
        ts_list = [c.ts_utc for c in candles]
        self._candle_cache[symbol] = (ts_list, candles)
        return ts_list, candles


# ---- helpers ----
def _score_component_fields(feats: dict) -> dict:
    """features["score_components"] → FeatureRecord 의 <component>_<field> 평탄 매핑.

    저널에 컴포넌트가 없거나(구 스키마) 하드 제외로 features 가 비면 전부 None 이다.
    누락을 0.0 으로 채우지 않는다 — 0 은 "계산됐고 값이 0"과 구분되어야 한다.
    """
    comps = feats.get("score_components") or {}
    out: dict = {}
    for name in SCORE_COMPONENT_NAMES:
        comp = comps.get(name) or {}
        for fld in SCORE_COMPONENT_FIELDS:
            out[f"{name}_{fld}"] = _f(comp.get(fld))
    return out


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _market_from_cycle(cycle):
    m = cycle.market or {}
    if isinstance(m, dict) and m.get("market"):
        return m["market"]
    return None


def _open_positions(portfolio) -> int | None:
    if not portfolio:
        return None
    pos = portfolio.get("positions")
    if isinstance(pos, dict):
        return len(pos)
    return None


def _apply_trade(lab: LabelRecord, order: dict | None) -> None:
    """진입 체결이 있으면 trade 필드 반영. 실현손익/청산은 후속(관찰 라벨이 MVP 중심)."""
    if not order:
        return
    filled = order.get("filled_quantity") or 0
    if order.get("status") != "FILLED" or not filled:
        return
    lab.trade_taken = True
    lab.entry_fill_price = _f(order.get("avg_fill_price"))
    lab.entry_qty = _f(order.get("filled_quantity"))
    lab.entry_side = order.get("side")
    lab.entry_fee = _f(order.get("fee"))
