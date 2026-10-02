"""Costs, account paths and scoring (PREREG sections 4-5).

Model: the account holds a target weight of one product (rest is 0%-return cash). For
full-weight rules (w in {0, 1}) the daily account factor is 1 + w[t-1]*R[prod[t-1], t].
Rules 7 and 8 hold a partial 3x sleeve that drifts between rebalances (see Drift). Each
close-of-day trade charges cost * turnover where turnover = |dw| for the same product and
w_old + w_new when the product changes (sell once + buy once). The initial buy charges
cost * w.
"""
from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class Drift:
    """Rebalanced partial-weight rule (7, 8): the 3x sleeve drifts between rebalances.

    reb[t] marks an executed rebalance at the close of row t; l3 is the cumulative log of
    the 3x product's daily growth.
    """

    reb: np.ndarray
    l3: np.ndarray


def drift_l3(r3: np.ndarray) -> np.ndarray:
    return np.cumsum(np.log(np.maximum(1.0 + r3, 1e-12)))


def log_factors_drift(w: np.ndarray, reb: np.ndarray, r3: np.ndarray, cost: float) -> np.ndarray:
    """Per-row log account factor with sleeve/cash tracked separately.

    The sleeve (V3) grows with the 3x product and cash stays flat. On a rebalance row the
    sleeve is reset to w[t]*V and cost*|w[t]*V - V3_drifted| is charged (as a fraction of V).
    The target weight in force during a segment is w at the segment's rebalance row.
    """
    n = len(w)
    t = np.arange(1, n)
    l3 = drift_l3(r3)
    base = np.maximum.accumulate(np.where(reb, np.arange(n), 0))
    b = base[:-1]  # segment base active during row t
    wb = w[b]
    g = np.exp(l3[t] - l3[b])
    gp = np.exp(l3[t - 1] - l3[b])
    q = (1.0 - wb) + wb * g
    qp = (1.0 - wb) + wb * gp
    x_before = wb * g / q
    trade_cost = np.where(reb[t], cost * np.abs(w[t] - x_before), 0.0)
    out = np.zeros(n)
    out[1:] = np.log(np.maximum(q / qp * (1.0 - trade_cost), 1e-12))
    return out


def account_values(lc, w, drift, starts, idx, v0, cost) -> np.ndarray:
    """Account value at rows idx (shape (s, m)) for fresh accounts opened at close `starts`.

    v0 is each account's opening value (after the initial buy cost). With drift, the first
    segment (until the first rebalance after the start) is rebuilt from the start weight.
    """
    n = len(lc)
    idxc = np.minimum(idx, n - 1)
    plain = v0[:, None] * np.exp(lc[idxc] - lc[starts][:, None])
    if drift is None:
        return plain
    reb_rows = np.append(np.flatnonzero(drift.reb), n)  # sentinel n: no further rebalance
    r1 = reb_rows[np.searchsorted(reb_rows, starts, side="right")]
    r1c = np.minimum(r1, n - 1)
    ws = w[starts]
    l3 = drift.l3
    first = (1.0 - ws)[:, None] + ws[:, None] * np.exp(l3[idxc] - l3[starts][:, None])
    g1 = np.exp(l3[r1c] - l3[starts])
    q1 = (1.0 - ws) + ws * g1
    after = q1 * (1.0 - cost * np.abs(w[r1c] - ws * g1 / q1))
    rest = after[:, None] * np.exp(lc[idxc] - lc[r1c][:, None])
    return v0[:, None] * np.where(idx < r1[:, None], first, rest)


def score_paths(lc: np.ndarray, w: np.ndarray, starts: np.ndarray, cost: float, drift=None) -> dict:
    """First-hit scoring: reach USD 4,000 before closing <= 0.65 x running peak."""
    n = len(lc)
    cols = np.arange(C.HORIZON_DAYS + 1)
    idx = starts[:, None] + cols[None, :]
    valid = idx < n
    v0 = C.START_EQUITY * (1.0 - cost * w[starts])
    path = account_values(lc, w, drift, starts, idx, v0, cost)
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


def p_reach(lc: np.ndarray, w: np.ndarray, starts: np.ndarray, cost: float, drift=None) -> float:
    return float(score_paths(lc, w, starts, cost, drift)["reached"].mean())


def window_growth(lc: np.ndarray, w: np.ndarray, a: int, b: int, cost: float, drift=None) -> float:
    """Growth multiple of a fresh account opened at close a and held to close b."""
    starts = np.array([a])
    v0 = np.array([1.0 - cost * w[a]])
    return float(account_values(lc, w, drift, starts, np.array([[b]]), v0, cost)[0, 0])


def full_period_stats(
    prod, w, lc, a: int, b: int, cost: float, dates: pd.DatetimeIndex, drift=None
) -> dict:
    growth = window_growth(lc, w, a, b, cost, drift)
    years = (dates[b] - dates[a]).days / 365.25
    starts = np.array([a])
    path = account_values(
        lc, w, drift, starts, np.arange(a, b + 1)[None, :], np.array([1.0 - cost * w[a]]), cost
    )[0]
    legs = order_legs(prod, w)
    if drift is not None:
        legs = np.where(drift.reb & (w > 0) & (legs == 0), 1, legs)
    legs = legs[a + 1 : b + 1]
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


def score_paths_after_tax(
    prod, w, rmat, starts, cost: float, year_end: np.ndarray, reb: np.ndarray | None = None
) -> dict:
    """Like score_paths with 22% Korean overseas-stock tax, applied approximately.

    Gains realise only when a position is sold (sleeve value x*V against its cost basis);
    tax on the calendar-year realised total above USD 1,786 is paid from the account on
    the year's last trading day; same-year losses offset gains, nothing carries over, and
    unrealised gains are not taxed. The sleeve weight x drifts with returns and is reset to
    the target on trade rows (a product/weight change, or a rebalance row in `reb`).
    """
    n = len(w)
    s = len(starts)
    v = C.START_EQUITY * (1.0 - cost * w[starts])
    x = w[starts].copy()
    basis = x * v
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
        p0, p1, w1 = prod[d - 1], prod[d], w[d]
        r = rmat[p0, d]
        v = v * (1.0 + x * r)
        xb = x * (1.0 + r) / (1.0 + x * r)  # weight before the close-of-day trade
        same = p0 == p1
        trade = (~same) | (w1 != w[d - 1])
        if reb is not None:
            trade = trade | reb[d]
        with np.errstate(divide="ignore", invalid="ignore"):
            part = np.where(xb > 0, np.clip((xb - w1) / xb, 0.0, 1.0), 0.0)
        sold = np.where(trade, np.where(same, part, (xb > 0).astype(float)), 0.0)
        gains += np.where(live, sold * (xb * v - basis), 0.0)
        bought = np.where(same, np.maximum(w1 - xb, 0.0), w1) * v
        basis = basis * (1.0 - sold) + np.where(trade, bought, 0.0)
        turn = np.where(same, np.abs(w1 - xb), xb + w1)
        v = v * (1.0 - cost * np.where(trade, turn, 0.0))
        x = np.where(trade, w1, xb)
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
