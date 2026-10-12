"""Quality metrics for the long-history data set (pure functions; inputs are already-loaded rows).

Coverage metrics use dates only. Price-value checks (OHLC consistency, Toss ratio, leveraged-ETF
fidelity) are meant to be fed rows strictly before the sealed period; the callers enforce that via
`load_daily(purpose="performance", end < 2025-07-21)`. The fidelity metric compares same-day
open->close moves of a 3x ETF with 3x its reference; it is a data-fidelity measure for a later
synthetic-3x approximation, NOT a strategy return.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter


def coverage(dates: list[str], calendar: list[str]) -> dict:
    """Missing calendar sessions and extra (non-session) rows, both counted inside the calendar span.

    Rows dated before the calendar's first session are counted separately (`before_calendar`).
    """
    ds = sorted(dates)
    if not ds:
        return {"rows": 0, "first": None, "last": None, "missing": 0, "extra": 0, "before_calendar": 0,
                "duplicates": 0, "missing_sample": [], "extra_sample": []}
    cal = [d for d in calendar if ds[0] <= d <= ds[-1]]
    have, cset = set(ds), set(calendar)
    missing = [d for d in cal if d not in have]
    extra = [d for d in ds if calendar[0] <= d <= calendar[-1] and d not in cset]
    return {"rows": len(ds), "first": ds[0], "last": ds[-1], "calendar_sessions": len(cal),
            "missing": len(missing), "extra": len(extra), "duplicates": len(ds) - len(have),
            "before_calendar": len([d for d in ds if d < calendar[0]]),
            "missing_sample": missing[:10], "extra_sample": extra[:10]}


def ohlc_checks(rows: list[dict]) -> dict:
    """Suspicious bars: inconsistent/null OHLC, non-positive, flat (O=H=L=C), open == prior close.

    `rows` carry date, open, high, low, close in date order. First/last dates are given for the flat
    and open==prior-close conditions so a later study can see which period is affected.
    """
    bad = nonpos = 0
    flat, open_eq_prev = [], []
    prev_close = None
    for r in rows:
        o, h, lo, c = r["open"], r["high"], r["low"], r["close"]
        if None in (o, h, lo, c) or any(isinstance(x, float) and math.isnan(x) for x in (o, h, lo, c)):
            bad += 1
            prev_close = c
            continue
        if min(o, h, lo, c) <= 0:
            nonpos += 1
        if lo > min(o, c) + 1e-9 or h < max(o, c) - 1e-9 or lo > h:
            bad += 1
        if o == h == lo == c:
            flat.append(r["date"])
        if prev_close is not None and o == prev_close:
            open_eq_prev.append(r["date"])
        prev_close = c
    return {"rows": len(rows), "ohlc_inconsistent_or_null": bad, "non_positive": nonpos,
            "flat_bars": len(flat), "flat_first": flat[0] if flat else None, "flat_last": flat[-1] if flat else None,
            "open_equals_prior_close": len(open_eq_prev),
            "open_eq_prev_first": open_eq_prev[0] if open_eq_prev else None,
            "open_eq_prev_last": open_eq_prev[-1] if open_eq_prev else None,
            "open_eq_prev_since_2010": len([d for d in open_eq_prev if d >= "2010-01-01"])}


def ratio_deviation(pairs: list[tuple[float, float]]) -> dict:
    """|a/b - 1| median and max over (a, b) price pairs."""
    devs = [abs(a / b - 1.0) for a, b in pairs if a and b]
    if not devs:
        return {"n": 0, "median_abs_dev": None, "max_abs_dev": None, "n_over_0_1pct": 0}
    return {"n": len(devs), "median_abs_dev": statistics.median(devs), "max_abs_dev": max(devs),
            "n_over_0_1pct": sum(1 for x in devs if x > 0.001)}


def fidelity(etf: dict[str, tuple[float, float]], ref: dict[str, tuple[float, float]], mult: float) -> dict:
    """Corr and mean |diff| between ETF open->close move and mult x reference open->close move.

    etf/ref map date -> (open, close). Only dates present in both with positive prices are used.
    """
    xs, ys = [], []
    for d in sorted(set(etf) & set(ref)):
        (eo, ec), (ro, rc) = etf[d], ref[d]
        if not (eo and ec and ro and rc) or min(eo, ec, ro, rc) <= 0:
            continue
        xs.append(ec / eo - 1.0)
        ys.append(mult * (rc / ro - 1.0))
    n = len(xs)
    if n < 3:
        return {"n": n, "corr": None, "mean_abs_diff": None}
    return {"n": n, "corr": statistics.correlation(xs, ys),
            "mean_abs_diff": sum(abs(a - b) for a, b in zip(xs, ys)) / n,
            "first": min(set(etf) & set(ref)), "last": max(set(etf) & set(ref))}


def per_year_counts(dates: list[str], first_year: int, last_year: int) -> dict[int, int]:
    c = Counter(int(d[:4]) for d in dates)
    return {y: c.get(y, 0) for y in range(first_year, last_year + 1)}


def compare_dates(ours: list[str], theirs: list[str], start: str, end: str) -> dict:
    a = {d for d in ours if start <= d <= end}
    b = {d for d in theirs if start <= d <= end}
    return {"window": f"{start}..{end}", "ours": len(a), "reference_count": len(b), "matched": len(a & b),
            "only_ours": sorted(a - b), "only_reference": sorted(b - a)}
