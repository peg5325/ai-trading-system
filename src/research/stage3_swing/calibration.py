"""Calibration gate (PREREG section 3): runs before any strategy is computed."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import contract as C
from .loading import Panel
from .products import expense_ratio, synthetic_return


def weekly_returns(r: pd.DataFrame) -> pd.DataFrame:
    """Compound daily returns into calendar weeks ending Friday (v2 section 3)."""
    weeks = (1.0 + r).resample(C.CAL_WEEK_RULE)
    return (weeks.prod() - 1.0)[weeks.size() > 0]  # weeks with no trading day are not weeks of data


def _compare(syn: np.ndarray, act: np.ndarray, dates: pd.DatetimeIndex) -> dict:
    ok = ~np.isnan(act) & ~np.isnan(syn)
    s, a = syn[ok], act[ok]
    n = int(ok.sum())
    ann_s = float(np.prod(1.0 + s) ** (252.0 / n) - 1.0)
    ann_a = float(np.prod(1.0 + a) ** (252.0 / n) - 1.0)
    weekly = weekly_returns(pd.DataFrame({"s": s, "a": a}, index=dates[ok]))
    return {
        "days": n,
        "weeks": len(weekly),
        "ann_synthetic": ann_s,
        "ann_actual": ann_a,
        "ann_diff": ann_s - ann_a,
        "corr_daily": float(np.corrcoef(s, a)[0, 1]),
        "corr_weekly": float(weekly["s"].corr(weekly["a"])),
    }


def run_calibration(panel: Panel) -> dict:
    rows = {}
    passed = True
    for etf, idx, lev in C.CALIBRATION_PAIRS:
        syn = synthetic_return(panel.ru[idx], panel.rf, lev, expense_ratio(idx, lev))
        row = _compare(syn, panel.etf_tr[etf], panel.dates)
        row["pass"] = bool(
            abs(row["ann_diff"]) <= C.CAL_MAX_ANN_DIFF and row["corr_weekly"] >= C.CAL_MIN_CORR
        )
        passed = passed and row["pass"]
        rows[etf] = row
    spy_plus_er = panel.etf_tr["SPY"] + C.ER["sp_1x"] / 252.0
    return {
        "pass": bool(passed),
        "thresholds": {
            "max_abs_ann_diff": C.CAL_MAX_ANN_DIFF,
            "min_corr": C.CAL_MIN_CORR,
            "corr_basis": f"weekly ({C.CAL_WEEK_RULE}, compounded)",
        },
        "etfs": rows,
        "sp500tr_vs_spy_plus_er": _compare(panel.ru["sp"], spy_plus_er, panel.dates),
    }
