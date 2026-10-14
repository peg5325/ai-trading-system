"""Overnight drift V2 ablation (spec docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.md §3, §4, §5).

Five configurations run on the same prepared QQQ days and the same scored start as V1: V1_BASE and NO_FILTER_BENCH
reproduce V1 and its benchmark; V2A / V2B use the prior-day daily EMA20 alone; V2C uses a prior-day daily EMA50 built
the same way as the engine's EMA20 (restart after any incomplete day).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date as Date

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY
from src.explore_orb_vwap_v0.universe import SymbolDay

from .engine import (
    ENTRY_MIN,
    EXIT_MIN,
    SIGNAL_MIN,
    STOP_EXIT_MIN,
    bar,
    entry_status,
    run_backtest,
)


def daily_ema_series(qqq: dict[str, SymbolDay], days: list[str], period: int) -> dict[str, float | None]:
    """Prior-day daily EMA(period) per day from complete sessions' last close: seeded with the simple mean of the
    first `period` closes, then ema += 2 / (period + 1) * (close - ema). Restarts after any incomplete day and a day
    gets a value only if the previous trading day was complete (same rule as `prepare_symbol` for EMA20)."""
    out: dict[str, float | None] = {}
    seed: list[float] = []
    ema: float | None = None
    prev_complete = False
    for d in days:
        sd = qqq[d]
        if not sd.complete:
            seed, ema, prev_complete = [], None, False
            out[d] = None
            continue
        if not prev_complete:
            seed, ema = [], None
        out[d] = ema if prev_complete else None
        close = sd.bars[-1].close
        if ema is None:
            seed.append(close)
            if len(seed) == period:
                ema = sum(seed) / period
        else:
            ema += 2.0 / (period + 1) * (close - ema)
        prev_complete = True
    return out


def trend_entry(ema: dict[str, float | None]):
    """Entry rule `c > prior-day EMA` only (spec V2 §4), with the V1 no-entry days."""
    def status(sd: SymbolDay, nxt: SymbolDay | None) -> str:
        if not sd.complete:
            return "excluded_incomplete"
        e = ema.get(sd.day)
        if e is None:
            return "daily_not_ready"
        if sd.close_minute <= ENTRY_MIN:
            return "early_close"
        if nxt is None or nxt.day > DEV_LAST_DAY.isoformat():
            return "no_next_day"
        if not nxt.complete:
            return "next_incomplete"
        if not bar(sd, SIGNAL_MIN).close > e:
            return "below_ema"
        return "enter"
    return status


@dataclass(frozen=True)
class Config:
    name: str
    filter: str  # v1 | ema20 | ema50 | none
    exit_minute: int
    gap_stop: bool


CONFIGS = (
    Config("V1_BASE", "v1", EXIT_MIN, True),
    Config("V2A_EMA20_ONLY", "ema20", EXIT_MIN, True),
    Config("V2B_EMA20_OPEN_EXIT", "ema20", STOP_EXIT_MIN, False),
    Config("V2C_EMA50_OPEN_EXIT", "ema50", STOP_EXIT_MIN, False),
    Config("NO_FILTER_BENCH", "none", EXIT_MIN, True),
)


def entry_for(cfg: Config, ema20: dict, ema50: dict):
    if cfg.filter == "v1":
        return lambda sd, nxt: entry_status(sd, nxt, True)
    if cfg.filter == "none":
        return lambda sd, nxt: entry_status(sd, nxt, False)
    return trend_entry(ema20 if cfg.filter == "ema20" else ema50)


def run_all(qqq: dict[str, SymbolDay], trading_days: list[Date]) -> dict:
    """{name: (BacktestResult, holds)} for the five configs, plus the EMA series used."""
    days = [d.isoformat() for d in trading_days]
    ema20 = daily_ema_series(qqq, days, 20)
    ema50 = daily_ema_series(qqq, days, 50)
    mismatch = [d for d in days if qqq[d].complete and ema20[d] != qqq[d].daily_ema]
    if mismatch:
        raise AssertionError(f"EMA20 series differs from prepare_symbol on {mismatch[:3]}")
    out = {}
    for cfg in CONFIGS:
        out[cfg.name] = run_backtest(qqq, trading_days, entry=entry_for(cfg, ema20, ema50),
                                     exit_minute=cfg.exit_minute, gap_stop=cfg.gap_stop)
    return {"results": out, "ema20": ema20, "ema50": ema50}


def year_mdd(days, start_equity: float) -> dict[str, dict]:
    """Per calendar year: max drawdown (USD, fraction) of the end-of-day equity with the year's start equity as the
    first peak (spec V2 §5)."""
    out: dict[str, dict] = {}
    for y in sorted({d.day[:4] for d in days}):
        yd = [d for d in days if d.day[:4] == y]
        peak = yd[0].start_equity
        mdd = mdd_pct = 0.0
        for d in yd:
            peak = max(peak, d.end_equity)
            mdd = max(mdd, peak - d.end_equity)
            mdd_pct = max(mdd_pct, (peak - d.end_equity) / peak if peak > 0 else 0.0)
        out[y] = {"mdd_usd": mdd, "mdd_pct": mdd_pct}
    return out


def gross_pct(t) -> float:
    return sum((leg.raw - t.entry_raw) * leg.shares for leg in t.legs) / (t.shares * t.entry_raw)


def _diff_stats(diffs: list[float]) -> dict:
    n = len(diffs)
    mean = statistics.mean(diffs) if diffs else None
    sd = statistics.stdev(diffs) if n > 1 else None
    ties = sum(x == 0 for x in diffs)
    return {"n": n, "mean_pct": mean, "sd_pct": sd, "t": mean / (sd / math.sqrt(n)) if sd else None,
            "ties": ties, "b_better_share_of_non_ties": (sum(x > 0 for x in diffs) / (n - ties)) if n > ties else None}


def paired_exit_diff(a_trades, b_trades) -> dict:
    """Spec V2 §5 Q2: trades with the same entry day in both lists; per trade (b exit raw - a exit raw) / entry raw.
    Positive means b's exit was higher (better for a long). `all` covers every pair; `a_normal_exit` excludes pairs
    where a left on its disaster stop (same 09:31 bar as b, so the difference is exactly 0): that block is the
    09:35-vs-09:31 comparison proper."""
    a = {t.day: t for t in a_trades}
    every, normal = [], []
    disaster = 0
    for t in b_trades:
        s = a.get(t.day)
        if s is None:
            continue
        x = (t.legs[-1].raw - s.legs[-1].raw) / s.entry_raw
        every.append(x)
        if s.exit_reason == "disaster_stop":
            disaster += 1
        else:
            normal.append(x)
    n = len(every)
    return {"pairs": n, "unpaired_a": len(a_trades) - n, "unpaired_b": len(b_trades) - n,
            "a_disaster_stop_pairs": disaster, "all": _diff_stats(every), "a_normal_exit": _diff_stats(normal)}
