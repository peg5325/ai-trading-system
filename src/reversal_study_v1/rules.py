"""Reversal signals T1..T4 (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §3). Pure functions on the reference symbol's bars.

Same conventions as `entry_study_v1.rules`: times are bar START minutes (ET); a decision "at t" uses ref bars with
start <= t only (bar t is complete at t+1) and the fill is the open of ETF bar t+1. Every loop below reads bar t
only after deciding that t is the minute under test, so later bars cannot leak in. Comparisons use float64 values
as computed (no rounding). Parameters come from config/reversal_study_v1.json.
"""
from __future__ import annotations

from collections.abc import Iterator

from src.daytrade_paper.core import Bar
from src.entry_study_v1.bars import InvalidRun
from src.entry_study_v1.config import hhmm
from src.entry_study_v1.rules import (
    OPEN,
    RefDay,
    Result,
    Signal,
    e3,
    gap_of,
    sigma_at,
    visible,
)

__all__ = ["RULE_FUNCS", "RefDay", "Result", "Signal", "bands", "evaluate", "opening_range", "t1", "t2", "t3",
           "t4", "vwap_iter", "vwap_series"]


def _none(reason: str, **info) -> Result:
    return Result(None, reason, info)


def _bar(day: RefDay, t: int) -> Bar:
    b = day.bars.get(t)
    if b is None:
        raise InvalidRun(f"{day.day}: reference bar {t} is missing")
    return b


# ------------------------------------------------------------------ T1: gap fill


def t1(day: RefDay, p: dict) -> Result:
    first, last = hhmm(p["window_first"]), hhmm(p["window_last"])
    seen = visible(day.bars, OPEN + 1)  # the gap needs the 09:30 open (and PRE bars) only
    if OPEN not in seen:
        raise InvalidRun(f"{day.day}: T1 needs ref bar {OPEN}")
    g, source = gap_of(day, seen, p)
    info = {"gap_source": source, "pre_substituted": not source.startswith("PRE"), "gap": g}
    if g is None:
        return _none("no_gap", **info)
    if abs(g) < p["gap_min_abs"]:
        return _none("gap_small", **info)
    pc = day.pc_bar.close
    up = g > 0  # gap up: filled when C <= PC (equality counts as filled); gap down: C >= PC

    def filled(c: float) -> bool:
        return c <= pc if up else c >= pc

    o = day.bars[OPEN].open
    info["open_already_filled"] = filled(o)
    info["filled_before_window"] = any(filled(day.bars[t].close) for t in range(OPEN, first) if t in day.bars)
    for t in range(first, last + 1):
        if filled(_bar(day, t).close):
            return Result(Signal("short" if up else "long", t + 1, f"g={g!r} C({t})={day.bars[t].close!r} PC={pc!r}"),
                          "signal", info)
    return _none("not_filled", **info)


# ------------------------------------------------------------------ T2: opening-range breakout failure


def opening_range(bars: dict[int, Bar], p: dict) -> tuple[float, float]:
    """(ORH, ORL) over the 15 bars 09:30..09:44 only."""
    ts = range(hhmm(p["or_first"]), hhmm(p["or_last"]) + 1)
    missing = [t for t in ts if t not in bars]
    if missing:
        raise InvalidRun(f"opening range lacks ref bars {missing}")
    return max(bars[t].high for t in ts), min(bars[t].low for t in ts)


def t2(day: RefDay, p: dict) -> Result:
    orh, orl = opening_range(day.bars, p)
    b_first, b_last = hhmm(p["breakout_first"]), hhmm(p["breakout_last"])
    b, up = None, None
    for t in range(b_first, b_last + 1):
        c = _bar(day, t).close
        if c > orh:
            b, up = t, True
            break
        if c < orl:
            b, up = t, False
            break
    if b is None:
        return _none("no_breakout", orh=orh, orl=orl)
    info = {"breakout_t": b, "breakout_dir": "up" if up else "down", "orh": orh, "orl": orl}
    end = min(b + p["fail_within_min"], hhmm(p["fail_last"]))
    for u in range(b + 1, end + 1):
        c = _bar(day, u).close
        if (up and c <= orh) or (not up and c >= orl):  # back inside, or through the far side
            return Result(Signal("short" if up else "long", u + 1, f"b={b} C({u})={c!r} ORH={orh!r} ORL={orl!r}"),
                          "signal", info)
    return _none("no_failure", **info)


# ------------------------------------------------------------------ T3: VWAP reclaim


def vwap_iter(bars: dict[int, Bar]) -> Iterator[tuple[int, float | None]]:
    """(t, VWAP(t)) for t = 09:30, 09:31, ...; VWAP(t) = sum TP*V / sum V over bars 09:30..t INCLUDING bar t,
    TP = (H+L+C)/3, plain Python float running sums in bar order. None while the volume sum is zero.
    Lazy: bar t+1 is read only when the next value is requested."""
    cum_pv = 0.0
    cum_v = 0.0
    t = OPEN
    while True:
        b = bars.get(t)
        if b is None:
            raise InvalidRun(f"VWAP needs ref bar {t}")
        tp = (b.high + b.low + b.close) / 3
        cum_pv += tp * b.volume
        cum_v += b.volume
        yield t, (cum_pv / cum_v if cum_v > 0 else None)
        t += 1


def vwap_series(bars: dict[int, Bar], last: int) -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    for t, v in vwap_iter(bars):
        out[t] = v
        if t >= last:
            return out
    return out  # unreachable (the generator never ends); keeps type checkers calm


def t3(day: RefDay, p: dict) -> Result:
    if p["tp"] != "hlc3":
        raise ValueError(f"unsupported typical price {p['tp']!r}")
    m_first, m_last = hhmm(p["morning_first"]), hhmm(p["morning_last"])
    if m_first != OPEN:
        raise ValueError("T3 morning must start at 09:30 (VWAP is cumulated from the open)")
    r_first, r_last, run = hhmm(p["reclaim_first"]), hhmm(p["reclaim_last"]), p["run_bars"]
    need = p["morning_frac"] * (m_last - m_first + 1)
    below = above = 0
    undefined = False
    hist: list[tuple[float, float | None]] = []
    morning: str | None = None
    for t, v in vwap_iter(day.bars):
        c = _bar(day, t).close
        hist.append((c, v))
        if t <= m_last:
            if v is None:
                undefined = True
            elif c < v:
                below += 1
            elif c > v:
                above += 1
        if t == m_last:
            if undefined:
                return _none("vwap_undefined", vwap_undefined=True)
            if below >= need:
                morning = "down"
            elif above >= need:
                morning = "up"
            else:
                return _none("no_trend_morning", below=below, above=above)
        if t >= r_first and morning is not None:
            tail = hist[-run:]
            if morning == "down" and all(vv is not None and cc > vv for cc, vv in tail):
                return Result(Signal("long", t + 1, f"down morning {below}/{m_last - m_first + 1}, {run} bars > VWAP"),
                              "signal", {"morning": morning, "below": below, "above": above})
            if morning == "up" and all(vv is not None and cc < vv for cc, vv in tail):
                return Result(Signal("short", t + 1, f"up morning {above}/{m_last - m_first + 1}, {run} bars < VWAP"),
                              "signal", {"morning": morning, "below": below, "above": above})
        if t >= r_last:
            break
    return _none("no_reclaim", morning=morning, below=below, above=above)


# ------------------------------------------------------------------ T4: opposite noise-band breakout


def bands(day: RefDay, sigma: float, multiplier: float) -> tuple[float, float]:
    """(U, L) exactly as `entry_study_v1.rules.e3` computes them."""
    o = day.bars[OPEN].open
    pc = day.pc_bar.close
    return max(o, pc) * (1 + multiplier * sigma), min(o, pc) * (1 - multiplier * sigma)


def t4(day: RefDay, p: dict) -> Result:
    first = e3(day, p)  # t1 = the E3 signal minute, direction as E3 (up -> "long")
    if first.signal is None:
        return _none("no_first_breakout", **first.info)
    t_first = first.signal.entry_t - 1
    up = first.signal.side == "long"
    n, mult = p["sigma_sessions"], p["multiplier"]
    undefined = first.info.get("sigma_undefined", 0)
    info = {"first_t": t_first, "first_dir": "up" if up else "down"}
    for t in range(t_first + 1, hhmm(p["last_check_bar_start"]) + 1):
        sig = sigma_at(day.sigma_days, t, n)
        if sig is None:
            undefined += 1
            continue
        c = _bar(day, t).close
        upper, lower = bands(day, sig, mult)
        if up and c < lower:
            return Result(Signal("short", t + 1, f"t1={t_first} C({t})={c!r} < L={lower!r}"), "signal",
                          {**info, "sigma_undefined": undefined})
        if not up and c > upper:
            return Result(Signal("long", t + 1, f"t1={t_first} C({t})={c!r} > U={upper!r}"), "signal",
                          {**info, "sigma_undefined": undefined})
    return _none("no_opposite", **info, sigma_undefined=undefined)


RULE_FUNCS = {"T1": t1, "T2": t2, "T3": t3, "T4": t4}


def evaluate(rule: str, day: RefDay, params: dict) -> Result:
    return RULE_FUNCS[rule](day, params)
