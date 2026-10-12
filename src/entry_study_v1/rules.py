"""Entry rules E1..E5 (PREREG §3). Pure functions on the reference symbol's bars.

Times are bar START minutes (ET). A decision at T sees only ref bars with start < T; the fill is the open of
the ETF bar T. Every rule filters its input with `visible(..., T)` itself, so bars at or after T cannot leak in.
Comparisons use float64 values as computed (no rounding). Parameters come from config/entry_study_v1.json.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar, PriorSession
from src.daytrade_paper.core import preopen_inputs as paper_preopen_inputs
from src.entry_study_v1.bars import InvalidRun
from src.entry_study_v1.config import hhmm

OPEN = R.OPEN


@dataclass(frozen=True)
class RefDay:
    """Reference-symbol inputs for one scored day. All prior-session data are fixed before the day starts."""

    day: str
    bars: dict[int, Bar]  # today's REGULAR bars by start minute
    pre: tuple[Bar, ...]  # today's PRE bars
    prev_date: str
    pc_bar: Bar  # previous session's bar starting at regular_close_minute - 1
    sigma_days: tuple[dict[int, Bar], ...]  # the 14 prior normal sessions' REGULAR bars (oldest first)


@dataclass(frozen=True)
class Signal:
    side: str  # "long" | "short"
    entry_t: int  # start minute of the ETF bar whose open is the fill
    reason: str


@dataclass(frozen=True)
class Result:
    signal: Signal | None
    reason: str
    info: dict = field(default_factory=dict)


def visible(bars: dict[int, Bar], t: int) -> dict[int, Bar]:
    """Bars completed before time t (start < t) - the look-ahead guard."""
    return {k: b for k, b in bars.items() if k < t}


def _none(reason: str, **info) -> Result:
    return Result(None, reason, info)


def e1(day: RefDay, p: dict) -> Result:
    entry = hhmm(p["entry"])
    sig_t = hhmm(p["signal_bar_start"])
    seen = visible(day.bars, entry)
    if OPEN not in seen or sig_t not in seen:
        raise InvalidRun(f"{day.day}: E1 needs ref bars {OPEN} and {sig_t}")
    c, o = seen[sig_t].close, seen[OPEN].open
    if c > o:
        return Result(Signal("long", entry, f"C({p['signal_bar_start']})={c!r} > O={o!r}"), "signal")
    if c < o:
        return Result(Signal("short", entry, f"C({p['signal_bar_start']})={c!r} < O={o!r}"), "signal")
    return _none("equal")


def gap_of(day: RefDay, seen: dict[int, Bar], p: dict) -> tuple[float | None, str]:
    """v1.0 gap exactly as the live Paper computes it (`core.preopen_inputs`)."""
    prior = [PriorSession(day.prev_date, (day.pc_bar,))]
    inp = paper_preopen_inputs(day.day, prior, list(day.pre), sorted(seen.values(), key=lambda b: b.t), {})
    return inp["gap"], inp["gap_source"]


def e2(day: RefDay, p: dict) -> Result:
    entry = hhmm(p["entry"])
    flow_t = hhmm(p["flow_bar_start"])
    seen = visible(day.bars, entry)
    if OPEN not in seen or flow_t not in seen:
        raise InvalidRun(f"{day.day}: E2 needs ref bars {OPEN} and {flow_t}")
    g, source = gap_of(day, seen, p)
    substituted = not source.startswith("PRE")
    info = {"gap_source": source, "pre_substituted": substituted, "gap": g}
    if g is None:
        return _none("no_gap", **info)
    f = seen[flow_t].close - seen[OPEN].open
    if abs(g) < p["gap_min_abs"]:
        return _none("gap_small", **info)
    if g > 0 and f > 0:
        return Result(Signal("long", entry, f"g={g!r} f={f!r}"), "signal", info)
    if g < 0 and f < 0:
        return Result(Signal("short", entry, f"g={g!r} f={f!r}"), "signal", info)
    return _none("flow_disagrees_or_zero", **info)


def sigma_at(sigma_days: tuple[dict[int, Bar], ...], t: int, n: int) -> float | None:
    """Mean over the prior sessions of |C_d(t) / O_d - 1|; None when any session lacks bar t or its 09:30 bar."""
    if len(sigma_days) != n:
        raise InvalidRun(f"sigma needs {n} prior sessions, got {len(sigma_days)}")
    vals = []
    for s in sigma_days:
        if OPEN not in s or t not in s:
            return None
        vals.append(abs(s[t].close / s[OPEN].open - 1))
    return sum(vals) / n


def sigma_undefined_minutes(day: RefDay, p: dict) -> list[int]:
    """Check minutes 10:00..15:00 where sigma(t) is undefined (uses prior sessions only)."""
    n = p["sigma_sessions"]
    return [t for t in range(hhmm(p["first_check_bar_start"]), hhmm(p["last_check_bar_start"]) + 1)
            if sigma_at(day.sigma_days, t, n) is None]


def e3(day: RefDay, p: dict) -> Result:
    n, mult = p["sigma_sessions"], p["multiplier"]
    pc = day.pc_bar.close
    undefined = 0
    for t in range(hhmm(p["first_check_bar_start"]), hhmm(p["last_check_bar_start"]) + 1):
        seen = visible(day.bars, t + 1)  # bar t is complete at t+1; the fill is the open of bar t+1
        if OPEN not in seen or t not in seen:
            raise InvalidRun(f"{day.day}: E3 needs ref bars {OPEN} and {t}")
        sig = sigma_at(day.sigma_days, t, n)
        if sig is None:
            undefined += 1
            continue
        o = seen[OPEN].open
        upper = max(o, pc) * (1 + mult * sig)
        lower = min(o, pc) * (1 - mult * sig)
        c = seen[t].close
        if c > upper:
            return Result(Signal("long", t + 1, f"C({t})={c!r} > U={upper!r}"), "signal",
                          {"sigma_undefined": undefined})
        if c < lower:
            return Result(Signal("short", t + 1, f"C({t})={c!r} < L={lower!r}"), "signal",
                          {"sigma_undefined": undefined})
    return _none("no_breakout", sigma_undefined=undefined)


def e4(day: RefDay, p: dict) -> Result:
    entry = hhmm(p["entry"])
    sig_t = hhmm(p["signal_bar_start"])
    seen = visible(day.bars, entry)
    if sig_t not in seen:
        raise InvalidRun(f"{day.day}: E4 needs ref bar {sig_t}")
    r1 = seen[sig_t].close / day.pc_bar.close - 1
    if r1 > 0:
        return Result(Signal("long", entry, f"r1={r1!r}"), "signal")
    if r1 < 0:
        return Result(Signal("short", entry, f"r1={r1!r}"), "signal")
    return _none("zero")


def e5(day: RefDay, p: dict, threshold: float | None = None) -> Result:
    entry = hhmm(p["entry"])
    sig_t = hhmm(p["signal_bar_start"])
    thr = p["threshold"] if threshold is None else threshold
    seen = visible(day.bars, entry)
    if sig_t not in seen:
        raise InvalidRun(f"{day.day}: E5 needs ref bar {sig_t}")
    m = seen[sig_t].close / day.pc_bar.close - 1
    if m >= thr:
        return Result(Signal("long", entry, f"m={m!r}"), "signal")
    if m <= -thr:
        return Result(Signal("short", entry, f"m={m!r}"), "signal")
    return _none("below_threshold")


RULE_FUNCS = {"E1": e1, "E2": e2, "E3": e3, "E4": e4, "E5": e5}


def evaluate(rule: str, day: RefDay, params: dict) -> Result:
    return RULE_FUNCS[rule](day, params)


def decision_end(rule: str, params: dict) -> int:
    """The latest time T such that every decision of the rule uses only bars with start < T."""
    if rule == "E3":
        return hhmm(params["last_check_bar_start"]) + 1
    return hhmm(params["entry"])
