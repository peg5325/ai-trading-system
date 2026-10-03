"""Bars, pre-open inputs and data quality for the exploratory day-trading Paper (rules v1.0).

Pure functions. Times are ET minute-of-day integers; a 1m bar labelled t covers [t, t+1),
so a decision "at T" may only use bars with t < T and fills at the open of bar T.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta

from src.daytrade_paper import rules as R


@dataclass(frozen=True)
class Bar:
    t: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PriorSession:
    """One earlier QQQ regular session (daily OHLC built from its REGULAR 1m bars)."""

    date: str
    bars: tuple[Bar, ...]

    @property
    def open(self) -> float:
        return self.bars[0].open

    @property
    def high(self) -> float:
        return max(b.high for b in self.bars)

    @property
    def low(self) -> float:
        return min(b.low for b in self.bars)

    @property
    def close(self) -> float:
        return self.bars[-1].close


def fmt_t(t: int) -> str:
    return f"{t // 60:02d}:{t % 60:02d}"


def before(bars: list[Bar], t: int) -> list[Bar]:
    """Bars completed before time t (look-ahead guard)."""
    return [b for b in bars if b.t < t]


def window(bars: list[Bar], start: int, end: int) -> list[Bar]:
    return [b for b in bars if start <= b.t < end]


def by_t(bars: list[Bar]) -> dict[int, Bar]:
    return {b.t: b for b in bars}


def vwap_through(bars: list[Bar], t: int) -> float | None:
    """Session VWAP from 09:30 through bar t inclusive; typical price (H+L+C)/3."""
    pv = vol = 0.0
    for b in bars:
        if R.OPEN <= b.t <= t:
            pv += (b.high + b.low + b.close) / 3.0 * b.volume
            vol += b.volume
    return pv / vol if vol > 0 else None


def five_min_close(bars_t: dict[int, Bar], bucket_start: int) -> float | None:
    """Close of the 5m bar [s, s+5) aligned to 09:30; None unless its last 1m bar exists."""
    last = bars_t.get(bucket_start + 4)
    return last.close if last is not None else None


def third_friday(year: int, month: int) -> Date:
    d = Date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def options_expiry_flags(day: str) -> dict:
    d = Date.fromisoformat(day)
    monthly = d == third_friday(d.year, d.month)
    return {"monthly_opex": monthly, "quarterly_opex": monthly and d.month in (3, 6, 9, 12)}


def atr_pct(prior: list[PriorSession]) -> float | None:
    """Mean true range of the last ATR_DAYS sessions divided by the previous close."""
    if len(prior) < R.ATR_DAYS + 1:
        return None
    trs = []
    for prev, cur in zip(prior[-R.ATR_DAYS - 1 : -1], prior[-R.ATR_DAYS :]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs) / prior[-1].close


def high_mode(vix: float | None, vix9d: float | None) -> tuple[str, bool]:
    """Return (mode, treat_as_high). Unknown data is treated as HIGH (half size)."""
    if vix is not None and vix >= R.VIX_HIGH:
        return "HIGH", True
    if vix is not None and vix9d is not None:
        return ("HIGH", True) if vix9d / vix > R.VIX9D_RATIO_HIGH else ("NORMAL", False)
    return "UNKNOWN_TREATED_AS_HIGH", True


def preopen_inputs(
    day: str,
    prior: list[PriorSession],
    qqq_pre: list[Bar],
    qqq_regular: list[Bar],
    external: dict,
) -> dict:
    """Inputs known before the open (plus the 09:30 open as gap fallback).

    prior: QQQ sessions strictly before `day`, ascending. external: prior-day values
    {"vix": {...}, "vix9d": {...}, "gex": {...}} where each has "value" (or None).
    """
    prev_close = prior[-1].close
    closes = [s.close for s in prior[-R.SMA_DAYS :]]
    sma = sum(closes) / len(closes) if len(closes) == R.SMA_DAYS else None
    pre = [b for b in qqq_pre if b.t < R.GAP_PRE_CUTOFF]
    if pre:
        gap_ref, gap_source = pre[-1].close, f"PRE {fmt_t(pre[-1].t)} 종가"
    else:
        first = by_t(qqq_regular).get(R.OPEN)
        gap_ref, gap_source = (first.open, "09:30 시가(PRE 봉 없음)") if first else (None, "없음")
    gap = gap_ref / prev_close - 1 if gap_ref is not None else None
    vix = (external.get("vix") or {}).get("value")
    vix9d = (external.get("vix9d") or {}).get("value")
    mode, is_high = high_mode(vix, vix9d)
    return {
        "prev_session": prior[-1].date,
        "prev_close": prev_close,
        "sma50": sma,
        "bull": sma is not None and prev_close > sma,
        "atr14_pct": atr_pct(prior),
        "gap": gap,
        "gap_ref_price": gap_ref,
        "gap_source": gap_source,
        "vix_prev_close": vix,
        "vix9d_prev_close": vix9d,
        "vix9d_vix_ratio": (vix9d / vix) if vix and vix9d is not None else None,
        "risk_mode": mode,
        "high": is_high,
        "gex_prev": (external.get("gex") or {}).get("value"),
        **options_expiry_flags(day),
    }


def data_quality(
    regular: dict[str, list[Bar]], prior: list[PriorSession], early_close: bool
) -> dict:
    """INCOMPLETE_DATA check: full regular grid per symbol and enough QQQ history."""
    need = R.EARLY_CLOSE_BARS if early_close else R.FULL_SESSION_BARS
    last = R.EARLY_CLOSE_LAST_BAR if early_close else R.LAST_BAR
    issues: list[str] = []
    counts = {}
    for sym in R.SYMBOLS:
        ts = {b.t for b in regular.get(sym, [])}
        counts[sym] = len(ts)
        if len(ts) < need:
            issues.append(f"{sym} 정규장 1분봉 {len(ts)}개 < {need}")
        if R.OPEN not in ts:
            issues.append(f"{sym} 09:30 봉 누락")
        if last not in ts:
            issues.append(f"{sym} {fmt_t(last)} 봉 누락")
    min_hist = max(R.SMA_DAYS, R.ATR_DAYS + 1, R.RVOL_LOOKBACK)
    if len(prior) < min_hist:
        issues.append(f"QQQ 과거 세션 {len(prior)}개 < {min_hist}")
    return {"ok": not issues, "issues": issues, "regular_bar_counts": counts,
            "prior_sessions": len(prior), "expected_bars": need}
