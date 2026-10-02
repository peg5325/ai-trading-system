"""Costs, account paths and scoring (PREREG sections 4-5).

Model: the account holds a target weight of one product (rest is 0%-return cash). The
daily account factor is 1 + w[t-1]*R[prod[t-1], t]; target weights are held constant
between trades (drift is not modelled). Each close-of-day trade charges cost * turnover
where turnover = |dw| for the same product and w_old + w_new when the product changes
(sell once + buy once). The initial buy charges cost * w.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import contract as C

_BIG = 10**9


def turnover(prod: np.ndarray, w: np.ndarray) -> np.ndarray:
    turn = np.zeros(len(w))
    same = prod[1:] == prod[:-1]
    turn[1:] = np.where(same, np.abs(w[1:] - w[:-1]), w[:-1] + w[1:])
    return turn


def order_legs(prod: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Number of buy/sell orders per row (a product switch is a sell plus a buy)."""
    legs = np.zeros(len(w), dtype=np.int64)
    p0, p1, w0, w1 = prod[:-1], prod[1:], w[:-1], w[1:]
    differ = p0 != p1
    legs[1:] = (
        (differ & (w0 > 0)).astype(np.int64)
        + (differ & (w1 > 0)).astype(np.int64)
        + ((~differ) & (w1 != w0)).astype(np.int64)
    )
    return legs


def log_factors(prod: np.ndarray, w: np.ndarray, rmat: np.ndarray, cost: float) -> np.ndarray:
    """Per-row log account factor (row 0 is 0), net of that row's trading cost. Cumsum it for a path."""
    n = len(w)
    gross = 1.0 + w[:-1] * rmat[prod[:-1], np.arange(1, n)]
    f = np.maximum(gross, 1e-12) * (1.0 - cost * turnover(prod, w)[1:])
    out = np.zeros(n)
    out[1:] = np.log(np.maximum(f, 1e-12))
    return out


def month_starts(dates: pd.DatetimeIndex) -> np.ndarray:
    """Row positions of the first trading day of each month, 1994-02 .. 2015-07."""
    ym = np.asarray(dates.year * 100 + dates.month)
    first = np.flatnonzero(np.r_[True, ym[1:] != ym[:-1]])
    lo = C.START_FIRST[0] * 100 + C.START_FIRST[1]
    hi = C.START_LAST[0] * 100 + C.START_LAST[1]
    starts = first[(ym[first] >= lo) & (ym[first] <= hi)]
    if len(starts) != C.N_STARTS:
        raise ValueError(f"expected {C.N_STARTS} monthly starts, got {len(starts)}")
    return starts


def score_paths(lc: np.ndarray, w: np.ndarray, starts: np.ndarray, cost: float) -> dict:
    """First-hit scoring: reach USD 4,000 before closing <= 0.65 x running peak."""
    n = len(lc)
    cols = np.arange(C.HORIZON_DAYS + 1)
    idx = starts[:, None] + cols[None, :]
    valid = idx < n
    v0 = C.START_EQUITY * (1.0 - cost * w[starts])
    path = v0[:, None] * np.exp(lc[np.minimum(idx, n - 1)] - lc[starts][:, None])
    peak = np.maximum.accumulate(np.where(valid, path, 0.0), axis=1)
    reach = (path >= C.TARGET_EQUITY) & valid
    stop = (path <= C.STOP_FRACTION * peak) & valid
    reach_j = np.where(reach.any(axis=1), reach.argmax(axis=1), _BIG)
    stop_j = np.where(stop.any(axis=1), stop.argmax(axis=1), _BIG)
    reached = reach_j < stop_j
    stopped = stop_j < reach_j
    return {"reached": reached, "stopped": stopped, "days": np.where(reached, reach_j, -1)}


def summarize_paths(res: dict) -> dict:
    days = res["days"][res["reached"]]
    return {
        "p_reach": float(res["reached"].mean()),
        "p_stop": float(res["stopped"].mean()),
        "p_pending": float((~res["reached"] & ~res["stopped"]).mean()),
        "median_days_to_reach": float(np.median(days)) if len(days) else None,
    }


def p_reach(lc: np.ndarray, w: np.ndarray, starts: np.ndarray, cost: float) -> float:
    return float(score_paths(lc, w, starts, cost)["reached"].mean())


def window_growth(lc: np.ndarray, w: np.ndarray, a: int, b: int, cost: float) -> float:
    """Growth multiple of a fresh account opened at close a and held to close b."""
    return float((1.0 - cost * w[a]) * np.exp(lc[b] - lc[a]))


def full_period_stats(prod, w, lc, a: int, b: int, cost: float, dates: pd.DatetimeIndex) -> dict:
    growth = window_growth(lc, w, a, b, cost)
    years = (dates[b] - dates[a]).days / 365.25
    path = (1.0 - cost * w[a]) * np.exp(lc[a : b + 1] - lc[a])
    legs = order_legs(prod, w)[a + 1 : b + 1]
    return {
        "twr_annualized": float(growth ** (1.0 / years) - 1.0),
        "growth_multiple": growth,
        "max_drawdown": float((path / np.maximum.accumulate(path)).min() - 1.0),
        "orders": int(legs.sum() + (1 if w[a] > 0 else 0)),
        "trade_days": int((legs > 0).sum() + (1 if w[a] > 0 else 0)),
    }


def inverse_contribution(prod, w, rmat, a: int, b: int) -> dict:
    """Sum of daily log contributions on days an inverse position is held."""
    t = np.arange(a + 1, b + 1)
    held = prod[t - 1] == C.INV
    contrib = np.log1p(w[t - 1][held] * rmat[C.INV, t][held])
    return {"inverse_days": int(held.sum()), "inverse_log_contribution": float(contrib.sum())}


def score_paths_after_tax(prod, w, rmat, starts, cost: float, year_end: np.ndarray) -> dict:
    """Like score_paths with 22% Korean overseas-stock tax, applied approximately.

    Gains realise only when a position is sold (sleeve value w*V against its cost basis);
    tax on the calendar-year realised total above USD 1,786 is paid from the account on
    the year's last trading day; same-year losses offset gains, nothing carries over, and
    unrealised gains are not taxed.
    """
    n = len(w)
    s = len(starts)
    v = C.START_EQUITY * (1.0 - cost * w[starts])
    basis = w[starts] * v
    gains = np.zeros(s)
    peak = v.copy()
    reach_j = np.full(s, _BIG)
    stop_j = np.full(s, _BIG)
    for j in range(1, C.HORIZON_DAYS + 1):
        d = starts + j
        live = (d < n) & (reach_j == _BIG) & (stop_j == _BIG)
        if not live.any():
            break
        d = np.minimum(d, n - 1)
        p0, w0, p1, w1 = prod[d - 1], w[d - 1], prod[d], w[d]
        v = v * (1.0 + w0 * rmat[p0, d])
        same = p0 == p1
        with np.errstate(divide="ignore", invalid="ignore"):
            part = np.where(w0 > 0, np.clip((w0 - w1) / w0, 0.0, 1.0), 0.0)
        sold = np.where(same, part, (w0 > 0).astype(float))
        gains += np.where(live, sold * (w0 * v - basis), 0.0)
        basis = basis * (1.0 - sold) + np.where(same, np.maximum(w1 - w0, 0.0), w1) * v
        v = v * (1.0 - cost * np.where(same, np.abs(w1 - w0), w0 + w1))
        ye = year_end[d] & live
        tax = np.where(ye, np.maximum(gains - C.TAX_DEDUCTION_USD, 0.0) * C.TAX_RATE, 0.0)
        v = v - tax
        gains = np.where(ye, 0.0, gains)
        peak = np.maximum(peak, v)
        reach_j = np.where(live & (v >= C.TARGET_EQUITY), j, reach_j)
        stop_j = np.where(live & (reach_j == _BIG) & (v <= C.STOP_FRACTION * peak), j, stop_j)
    reached = reach_j < stop_j
    stopped = stop_j < reach_j
    return {"reached": reached, "stopped": stopped, "days": np.where(reached, reach_j, -1)}
