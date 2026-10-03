"""Regime labels, combo positions, account paths and selection (PREREG sections 2-4).

Conventions follow the stage-3 swing modules: a position is (product code, weight) held
after the close of each row; the account factor of row t is 1 + w[t-1] * R[prod[t-1], t]
times (1 - cost * turnover at the close of t) (`account.log_factors`). Exposure -1 is the
inverse product at full weight, exposure 0 is cash (0% return).

Timing ("lag", preregistered): regime at the close of t -> position set at the close of
t+1 -> that position earns the returns of t+2 onward. "same" (reference only): position
set at the close of t itself.
"""
from __future__ import annotations

from itertools import product as cartesian

import numpy as np
import pandas as pd

from src.research.stage3_swing.account import log_factors
from src.research.stage3_swing.rules import sma, wilder_rsi

from . import contract as C

COMBOS: tuple[tuple[int, int, int, int], ...] = tuple(cartesian(C.EXPOSURES, repeat=4))
_RANK = {e: i for i, e in enumerate(C.EXPOSURES)}


def classify(px: np.ndarray, vix: np.ndarray) -> np.ndarray:
    """Regime code per row (0..3 = A..D) from the close of that row only; -1 during warm-up."""
    px = np.asarray(px, dtype=float)
    vix = np.asarray(vix, dtype=float)
    ma = sma(px, C.SMA_N)
    rsi = wilder_rsi(px, C.RSI_N)
    with np.errstate(invalid="ignore"):
        above = px > ma
        lab = np.where(
            above,
            np.where(vix < C.VIX_LEVEL, 0, 1),
            np.where(rsi >= C.RSI_LEVEL, 2, 3),
        )
    undefined = np.isnan(ma) | np.isnan(vix) | (~above & np.isnan(rsi))
    return np.where(undefined, C.UNDEFINED, lab).astype(np.int8)


def positions(labels: np.ndarray, combo, mode: str = "lag"):
    """(prod, w, pos_label): position held after each close and the regime that decided it."""
    if mode not in ("lag", "same"):
        raise ValueError(mode)
    labels = np.asarray(labels)
    exp = np.where(labels >= 0, np.asarray(combo)[np.maximum(labels, 0)], 0)  # warm-up -> cash
    prod = _product_codes(exp)
    w = (exp != 0).astype(float)
    lab = labels.astype(np.int8)
    if mode == "lag":
        prod, w, lab = _shift1(prod, C.EXPOSURE_PRODUCT[0]), _shift1(w, 0.0), _shift1(lab, C.UNDEFINED)
    return prod, w, lab


def _product_codes(exp: np.ndarray) -> np.ndarray:
    out = np.zeros(len(exp), dtype=np.int8)
    for e, code in C.EXPOSURE_PRODUCT.items():
        out[exp == e] = code
    return out


def _shift1(x: np.ndarray, fill) -> np.ndarray:
    out = np.empty_like(x)
    out[0] = fill
    out[1:] = x[:-1]
    return out


def window_bounds(dates: pd.DatetimeIndex, lo, hi) -> tuple[int, int]:
    """(first row on/after lo, last row on/before hi)."""
    a = int(np.searchsorted(dates.values, pd.Timestamp(lo).to_datetime64(), side="left"))
    b = int(np.searchsorted(dates.values, pd.Timestamp(hi).to_datetime64(), side="right")) - 1
    if not 0 < a <= b < len(dates):
        raise ValueError(f"window {lo}..{hi} not inside the data")
    return a, b


def window_factors(prod, w, rmat, a: int, b: int, cost: float):
    """(v0, lf): opening value after the initial buy at close a, and per-row log factors for rows a..b.

    lf[0] is 0 (row a); lf[k] is row a+k's return on the position held after close a+k-1,
    net of the trade cost at close a+k. Only rows a..b are read.
    """
    v0 = C.START_EQUITY * (1.0 - cost * w[a])
    lf = log_factors(prod[a : b + 1], w[a : b + 1], rmat[:, a : b + 1], cost)
    return v0, lf


def drawdown(log_path: np.ndarray) -> float:
    """Max drawdown from the running peak of a close-based log value path (negative number)."""
    return float(np.exp((log_path - np.maximum.accumulate(log_path)).min()) - 1.0)


def path_stats(prod, w, rmat, a: int, b: int, cost: float, dates: pd.DatetimeIndex) -> dict:
    v0, lf = window_factors(prod, w, rmat, a, b, cost)
    value = v0 * np.exp(np.cumsum(lf))
    years = (dates[b] - dates[a]).days / 365.25
    growth = value[-1] / C.START_EQUITY
    hit = np.flatnonzero(value >= C.TARGET_EQUITY)
    return {
        "start": str(dates[a].date()),
        "end": str(dates[b].date()),
        "final_usd": float(value[-1]),
        "net_log_growth": float(np.log(growth)),
        "twr_annualized": float(growth ** (1.0 / years) - 1.0),
        "max_drawdown": drawdown(np.log(value)),
        "reached_4000": bool(len(hit)),
        "reached_4000_date": str(dates[a + hit[0]].date()) if len(hit) else None,
    }


def evaluate_combos(labels, rmat, a: int, b: int, cost: float, mode: str = "lag"):
    """Net log growth and max drawdown (from USD 2,000 incl. initial buy) of all 625 combos."""
    need = labels[a - 1 : b + 1] if mode == "lag" else labels[a : b + 1]
    if (need < 0).any():
        raise ValueError("regime undefined inside the evaluation window (warm-up too short)")
    growth = np.empty(len(COMBOS))
    mdd = np.empty(len(COMBOS))
    for i, combo in enumerate(COMBOS):
        prod, w, _ = positions(labels, combo, mode)
        v0, lf = window_factors(prod, w, rmat, a, b, cost)
        lp = np.log(v0) + np.cumsum(lf)
        growth[i] = lp[-1] - np.log(C.START_EQUITY)
        mdd[i] = drawdown(lp)
    return growth, mdd


def tie_key(combo) -> tuple:
    """Tie-break: smaller sum of |exposure|, then lexicographic (A,B,C,D) in EXPOSURES order."""
    return (sum(abs(e) for e in combo), tuple(_RANK[e] for e in combo))


def select(growth: np.ndarray, mdd: np.ndarray, combos=COMBOS):
    """Index of the chosen combo, or None (NO_CANDIDATE) if no combo has max drawdown < 35%."""
    ok = np.flatnonzero(-mdd < C.MDD_LIMIT)
    if not len(ok):
        return None
    best = growth[ok].max()
    tied = [int(i) for i in ok if growth[i] >= best - C.TIE_ATOL]
    return min(tied, key=lambda i: tie_key(combos[i]))


def regime_breakdown(lf_strat, lf_1x, held_label) -> dict:
    """Per regime: days, summed net log return of the strategy and of 1x on the same days.

    Arrays cover window rows a..b; row 0 (the opening close) holds no position yet and is
    excluded. held_label[k] is the regime that decided the position earning row k.
    """
    out = {}
    for code, name in enumerate(C.REGIMES):
        mask = np.zeros(len(lf_strat), dtype=bool)
        mask[1:] = held_label[1:] == code
        s, o = float(lf_strat[mask].sum()), float(lf_1x[mask].sum())
        out[name] = {
            "days": int(mask.sum()),
            "log_return": s,
            "log_return_1x": o,
            "excess_vs_1x": s - o,
            "beats_1x": bool(mask.any() and s > o),
        }
    return out


def held_labels(pos_label: np.ndarray, a: int, b: int) -> np.ndarray:
    """For window rows a..b: the regime label of the position held during that row (pos_label[t-1])."""
    return pos_label[a - 1 : b]
