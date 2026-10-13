"""Session table -> features, start-date rule, exclusions and eligibility (PREREG §3, §4.1). Pure functions.

Price frames have columns date, open, high, low, close (Yahoo `close`, split-adjusted only; `adjclose` unused).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ZERO_RATIO_MAX = 0.05      # (가) share of rows with g == 0 must be < 5%
TINY_GAP = 0.0002          # |g| < 0.02%
TINY_RATIO_MAX = 0.10      # (나) ^SOX only: share of |g| < 0.02% must be < 10%
MEDIAN_FRAC_MIN = 0.5      # (나) ^SOX only: median |g| >= 50% of SMH's that year
MAX_ABS_GAP = 0.15         # exclusion (d)
LOOKBACK_SKIP = 2          # first 2 sessions from the start date are skipped
SOX = "^SOX"
EXCLUSIONS = ("a_open_eq_prev_close", "b_flat_bar", "c_data_gap", "d_gap_gt_15pct", "e_sox_tiny_gap")


def dividend_on(dates: pd.Series, divs: dict[str, float]) -> pd.Series:
    return dates.map(lambda d: divs.get(d, 0.0)).astype(float)


def gaps(df: pd.DataFrame, divs: dict[str, float]) -> pd.Series:
    """Dividend-adjusted gap g = O / (C_prev_row - div) - 1 (NaN on the first row)."""
    prev_close = df["close"].shift(1)
    return df["open"] / (prev_close - dividend_on(df["date"], divs)) - 1


def year_stats(df: pd.DataFrame, divs: dict[str, float]) -> pd.DataFrame:
    """Per calendar year over every row except the series' first row: n, share g == 0, share |g| < 0.02%,
    median |g| (§3 start-date rule; unrounded)."""
    g = gaps(df, divs)
    t = pd.DataFrame({"year": df["date"].str[:4].astype(int), "g": g}).iloc[1:]
    by = t.groupby("year")["g"]
    return pd.DataFrame({"n": by.size(), "zero_ratio": by.apply(lambda x: float((x == 0).mean())),
                         "tiny_ratio": by.apply(lambda x: float((x.abs() < TINY_GAP).mean())),
                         "median_abs_g": by.apply(lambda x: float(x.abs().median()))})


def year_passes(index: str, row: pd.Series, smh_median: float | None) -> bool:
    if not row["zero_ratio"] < ZERO_RATIO_MAX:
        return False
    if index != SOX:
        return True
    if not row["tiny_ratio"] < TINY_RATIO_MAX:
        return False
    return smh_median is None or row["median_abs_g"] >= MEDIAN_FRAC_MIN * smh_median


def start_date(index: str, df: pd.DataFrame, divs: dict[str, float], last_year: int,
               smh_stats: pd.DataFrame | None = None) -> str | None:
    """First session of the first year Y such that every year Y..last_year passes (§3). None if none does."""
    ys = year_stats(df, divs)
    years = [int(y) for y in ys.index if y <= last_year]
    if not years or years != list(range(years[0], years[-1] + 1)) or years[-1] != last_year:
        return None  # a missing year cannot pass
    ok = {}
    for y in years:
        smh = None
        if index == SOX and smh_stats is not None and y in smh_stats.index:
            smh = float(smh_stats.loc[y, "median_abs_g"])
        ok[y] = year_passes(index, ys.loc[y], smh)
    for y in years:
        if all(ok[z] for z in years if z >= y):
            return str(df[df["date"].str[:4] == str(y)]["date"].iloc[0])
    return None


def session_table(index: str, px: pd.DataFrame, divs: dict[str, float], calendar: list[str], start: str,
                  aux: dict[str, dict[str, float]] | None = None) -> pd.DataFrame:
    """One row per price row of `index` with features, exclusion flags and eligibility.

    aux maps "vix", "v9", "v3", "gex" to {date: value}; each feature takes the value dated p(d) (no fallback).
    A row is eligible when it is on/after the start date (after skipping the first LOOKBACK_SKIP sessions) and
    none of the exclusions (a)-(e) holds on d or on its previous row p(d)."""
    aux = aux or {}
    df = px[["date", "open", "high", "low", "close"]].sort_values("date").reset_index(drop=True).copy()
    cal_prev = dict(zip(calendar[1:], calendar[:-1]))
    df["p"] = df["date"].map(cal_prev)
    df["prev_row"] = df["date"].shift(1)
    o_p, h_p, l_p, c_p = (df[c].shift(1) for c in ("open", "high", "low", "close"))
    df["g"] = gaps(df, divs)
    df["i"] = df["close"] / df["open"] - 1
    df["poc"] = c_p / o_p - 1
    df["prng"] = (h_p - l_p) / o_p
    for name in ("vix", "v9", "v3", "gex"):
        values = aux.get(name, {})
        df[f"{name}_p"] = df["p"].map(lambda d, v=values: v.get(d, np.nan) if isinstance(d, str) else np.nan)
    own = {
        # (a) compares the RAW previous close (stale-open check, §3 ⓐ); the start-date rule's "open = previous
        # close" share instead counts the dividend-adjusted g == 0 (§3: year stats use the adjusted g).
        "a_open_eq_prev_close": df["open"] == c_p,
        "b_flat_bar": (df["open"] == df["high"]) & (df["open"] == df["low"]) & (df["open"] == df["close"]),
        "c_data_gap": df["prev_row"].isna() | (df["prev_row"] != df["p"]),
        "d_gap_gt_15pct": df["g"].abs() > MAX_ABS_GAP,
        "e_sox_tiny_gap": (df["g"].abs() < TINY_GAP) if index == SOX else pd.Series(False, index=df.index),
    }
    for name, flag in own.items():
        flag = flag.fillna(False).astype(bool)
        df[name] = flag | flag.shift(1, fill_value=False).astype(bool)
    pos = np.arange(len(df))
    # The skip counts sessions with a computable gap (a previous row): a series' very first row has none.
    first = max(int(np.searchsorted(df["date"].to_numpy(), start)), 1)
    df["in_range"] = pos >= first + LOOKBACK_SKIP
    df["excluded"] = df[list(EXCLUSIONS)].any(axis=1)
    df["eligible"] = df["in_range"] & ~df["excluded"]
    return df


def quantile(x, q: float) -> float:
    return float(np.quantile(np.asarray(x, dtype=float), q, method="linear"))


def thresholds(tables: dict[str, pd.DataFrame], dev_end: str) -> dict:
    """§4.1 thresholds from DEV eligible sessions only: VIX terciles (SPY VIX_p), per-index mg, qo, prng 20/80%."""
    dev = {k: t[t["eligible"] & (t["date"] <= dev_end)] for k, t in tables.items()}
    vix = dev["SPY"]["vix_p"].dropna()
    out = {"vix_t1": quantile(vix, 1 / 3), "vix_t2": quantile(vix, 2 / 3), "vix_n": len(vix), "per_index": {}}
    for k, t in dev.items():
        out["per_index"][k] = {"mg": quantile(t["g"].abs(), 0.5), "qo": quantile(t["poc"].abs(), 0.8),
                               "prng_q20": quantile(t["prng"], 0.2), "prng_q80": quantile(t["prng"], 0.8),
                               "n": len(t)}
    return out
