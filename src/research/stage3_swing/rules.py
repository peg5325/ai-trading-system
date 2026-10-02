"""The 15 public rules per index (PREREG section 4).

Every indicator at row t uses only rows <= t. Indicator rules (3-10) decide at the
close of t and execute at the close of t+1 (one-day lag, unless mode="same").
Calendar rules (11, 12) execute on the flagged date. Positions are (product code,
weight-of-account) arrays: the position held after the close of each row.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import contract as C

Position = tuple[np.ndarray, np.ndarray]  # (product code int8, weight float)


@dataclass
class Series:
    """One index's decision inputs, aligned row by row."""

    px: np.ndarray  # signal-index level (cumprod of price returns; only ratios matter)
    ru: np.ndarray  # index total-return daily return
    rf: np.ndarray
    vix: np.ndarray
    is_me: np.ndarray
    td_of_month: np.ndarray
    td_from_end: np.ndarray
    month: np.ndarray


def series_from_arrays(pr, ru, rf, vix, is_me, td_of_month, td_from_end, month) -> Series:
    return Series(
        px=np.cumprod(1.0 + np.asarray(pr, dtype=float)),
        ru=np.asarray(ru, dtype=float),
        rf=np.asarray(rf, dtype=float),
        vix=np.asarray(vix, dtype=float),
        is_me=np.asarray(is_me, dtype=bool),
        td_of_month=np.asarray(td_of_month),
        td_from_end=np.asarray(td_from_end),
        month=np.asarray(month),
    )


def sma(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).rolling(n).mean().to_numpy()


def wilder_rsi(px: np.ndarray, n: int = 2) -> np.ndarray:
    """Wilder RSI: SMA-seeded smoothing with alpha=1/n."""
    out = np.full(len(px), np.nan)
    if len(px) <= n:
        return out
    delta = np.diff(px, prepend=np.nan)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[: n + 1] = np.nan
    loss[: n + 1] = np.nan
    d = delta[1 : n + 1]
    gain[n] = np.where(d > 0, d, 0.0).mean()
    loss[n] = np.where(d < 0, -d, 0.0).mean()
    ag = pd.Series(gain).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    al = pd.Series(loss).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100.0 - 100.0 / (1.0 + ag / al)
    rsi = np.where((al == 0) & (ag > 0), 100.0, rsi)
    rsi = np.where((al == 0) & (ag == 0), 50.0, rsi)
    rsi[: n] = np.nan
    return rsi


def ffill(values: np.ndarray, fill: float = 0.0) -> np.ndarray:
    return pd.Series(values).ffill().fillna(fill).to_numpy()


def _lag(prod: np.ndarray, w: np.ndarray) -> Position:
    lp = np.zeros_like(prod)
    lw = np.zeros_like(w)
    lp[1:] = prod[:-1]
    lw[1:] = w[:-1]
    return lp, lw


def _pos(code: int, w: np.ndarray) -> Position:
    w = np.asarray(w, dtype=float)
    return np.where(w > 0, code, C.CASH).astype(np.int8), w


def _event_state(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """1 after entry, 0 after exit; exit wins when both fire on the same row."""
    ev = np.full(len(entry), np.nan)
    ev[entry] = 1.0
    ev[exit_] = 0.0
    return ffill(ev)


def _shift(mask: np.ndarray, k: int) -> np.ndarray:
    out = np.zeros_like(mask)
    out[k:] = mask[:-k]
    return out


def _month_end_value(n: int, me_idx: np.ndarray, values: np.ndarray) -> np.ndarray:
    arr = np.full(n, np.nan)
    arr[me_idx] = values
    return arr


def _monthly_trend_and_return(s: Series, me_idx: np.ndarray):
    """Month-end booleans for rule 5 (10-month mean) and rule 6 (12-month return vs rf)."""
    n = len(s.px)
    closes = s.px[me_idx]
    avg10 = pd.Series(closes).rolling(10).mean().to_numpy()
    r5 = np.where(np.isnan(avg10), np.nan, (closes > avg10).astype(float))

    anchors_u = np.concatenate([[0.0], np.cumsum(np.log1p(s.ru))[me_idx]])
    anchors_f = np.concatenate([[0.0], np.cumsum(np.log1p(s.rf))[me_idx]])
    r6 = np.full(len(me_idx), np.nan)
    if len(me_idx) >= 12:
        du = anchors_u[12:] - anchors_u[:-12]
        df = anchors_f[12:] - anchors_f[:-12]
        r6[11:] = (du > df).astype(float)
    return _month_end_value(n, me_idx, r5), _month_end_value(n, me_idx, r6)


def _monthly_exposure(s: Series, me_idx: np.ndarray) -> np.ndarray:
    """e = min(3, 0.20/sigma) at each month-end; sigma from that month's daily returns."""
    n = len(s.px)
    e = np.full(len(me_idx), 0.0)
    if len(me_idx):
        x = s.ru[: me_idx[-1] + 1]
        starts = np.concatenate([[0], me_idx[:-1] + 1])
        cnt = (me_idx - starts + 1).astype(float)
        s1 = np.add.reduceat(x, starts)
        s2 = np.add.reduceat(x * x, starts)
        with np.errstate(divide="ignore", invalid="ignore"):
            var = (s2 - s1 * s1 / cnt) / (cnt - 1.0)
            sigma = np.sqrt(np.maximum(var, 0.0)) * np.sqrt(252.0)
            e = np.minimum(3.0, 0.20 / sigma)
        e = np.where(cnt < 2, 0.0, e)
    return _month_end_value(n, me_idx, e)


def rule_positions(s: Series, mode: str = "lag") -> dict[str, Position]:
    """Executed positions of all 15 rules. mode: "lag" (prereg) or "same" (sensitivity)."""
    if mode not in ("lag", "same"):
        raise ValueError(mode)
    n = len(s.px)
    me_idx = np.flatnonzero(s.is_me)
    above = s.px > sma(s.px, 200)
    decided: dict[str, Position] = {}
    ones = np.ones(n)

    decided["1"] = (np.full(n, C.X1, dtype=np.int8), ones)
    decided["2"] = (np.full(n, C.X2, dtype=np.int8), ones)
    decided["3"] = _pos(C.X2, above.astype(float))
    p3i = np.where(above, C.X2, C.INV).astype(np.int8)
    decided["3i"] = (p3i, ones)
    decided["4"] = _pos(C.X3, above.astype(float))

    r5, r6 = _monthly_trend_and_return(s, me_idx)
    sig5, sig6 = ffill(r5), ffill(r6)
    decided["5"] = _pos(C.X2, sig5)
    decided["5i"] = (np.where(sig5 > 0, C.X2, C.INV).astype(np.int8), ones)
    decided["6"] = _pos(C.X2, sig6)
    decided["6i"] = (np.where(sig6 > 0, C.X2, C.INV).astype(np.int8), ones)

    w_month = ffill(_monthly_exposure(s, me_idx)) / 3.0
    decided["7"] = _pos(C.X3, w_month)
    decided["8"] = _pos(C.X3, np.where(above, w_month, 0.0))

    rsi = wilder_rsi(s.px, 2)
    with np.errstate(invalid="ignore"):
        sma5_exit = s.px > sma(s.px, 5)
        entry9 = above & (rsi < 5)
        spike = s.vix >= 1.05 * sma(s.vix, 10)
        entry10 = above & spike & _shift(spike, 1) & _shift(spike, 2)
        exit10 = rsi > 65
    decided["9"] = _pos(C.X2, _event_state(entry9 & ~sma5_exit, sma5_exit))
    decided["10"] = _pos(C.X2, _event_state(entry10 & ~exit10, exit10))

    calendar: dict[str, Position] = {
        "11": _pos(C.X2, _event_state(s.td_from_end == 1, s.td_of_month == 3)),
        "12": _pos(
            C.X2, _event_state((s.month == 10) & s.is_me, (s.month == 4) & s.is_me)
        ),
    }
    if mode == "lag":
        decided = {k: _lag(*v) for k, v in decided.items()}
    decided.update(calendar)
    return {k: decided[k] for k in C.RULE_IDS}


def rebalance_flags(s: Series, mode: str = "lag") -> dict[str, np.ndarray]:
    """Executed rebalance rows of the partial-weight rules.

    Rule 7 rebalances at each month-end trade; rule 8 at each month-end trade and at each
    200-day-SMA switch. Decided at the close of the flagged row, executed one row later in
    "lag" mode.
    """
    above = s.px > sma(s.px, 200)
    flip = np.zeros(len(above), dtype=bool)
    flip[1:] = above[1:] != above[:-1]
    flags = {"7": s.is_me.copy(), "8": s.is_me | flip}
    if mode == "lag":
        flags = {k: _shift(v, 1) for k, v in flags.items()}
    return flags
