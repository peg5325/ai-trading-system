"""Calibration gate (PREREG section 3): runs before any strategy is computed."""
from __future__ import annotations

import numpy as np

from . import contract as C
from .loading import Panel
from .products import expense_ratio, synthetic_return


def _compare(syn: np.ndarray, act: np.ndarray) -> dict:
    ok = ~np.isnan(act) & ~np.isnan(syn)
    s, a = syn[ok], act[ok]
    n = int(ok.sum())
    ann_s = float(np.prod(1.0 + s) ** (252.0 / n) - 1.0)
    ann_a = float(np.prod(1.0 + a) ** (252.0 / n) - 1.0)
    return {
        "days": n,
        "ann_synthetic": ann_s,
        "ann_actual": ann_a,
        "ann_diff": ann_s - ann_a,
        "corr": float(np.corrcoef(s, a)[0, 1]),
    }


def run_calibration(panel: Panel) -> dict:
    rows = {}
    passed = True
    for etf, idx, lev in C.CALIBRATION_PAIRS:
        syn = synthetic_return(panel.ru[idx], panel.rf, lev, expense_ratio(idx, lev))
        row = _compare(syn, panel.etf_tr[etf])
        row["pass"] = bool(
            abs(row["ann_diff"]) <= C.CAL_MAX_ANN_DIFF and row["corr"] >= C.CAL_MIN_CORR
        )
        passed = passed and row["pass"]
        rows[etf] = row
    spy_plus_er = panel.etf_tr["SPY"] + C.ER["sp_1x"] / 252.0
    return {
        "pass": bool(passed),
        "thresholds": {"max_abs_ann_diff": C.CAL_MAX_ANN_DIFF, "min_corr": C.CAL_MIN_CORR},
        "etfs": rows,
        "sp500tr_vs_spy_plus_er": _compare(panel.ru["sp"], spy_plus_er),
    }
