"""Glue: rule positions -> per-rule log account factors for one index."""
from __future__ import annotations

import numpy as np

from . import contract as C
from .account import Drift, drift_l3, log_factors, log_factors_drift
from .products import return_matrix
from .rules import Series, rebalance_flags, rule_positions


def index_paths(s: Series, index: str, cost: float, mode: str = "lag"):
    """Returns (rmat, {rule: (prod, w, lc, drift)}) for one index; drift is None for full-weight rules."""
    rmat = return_matrix(s.ru, s.rf, index)
    flags = rebalance_flags(s, mode)
    out = {}
    for rule, (prod, w) in rule_positions(s, mode).items():
        if rule in flags:
            drift = Drift(flags[rule], drift_l3(rmat[C.X3]))
            lc = np.cumsum(log_factors_drift(w, flags[rule], rmat[C.X3], cost))
        else:
            drift = None
            lc = np.cumsum(log_factors(prod, w, rmat, cost))
        out[rule] = (prod, w, lc, drift)
    return rmat, out


def era_bounds(dates, eras=C.ERAS) -> list[tuple[int, int]]:
    """(first row on/after era start, last row on/before era end) for each era."""
    import pandas as pd

    bounds = []
    for lo, hi in eras:
        a = int(np.searchsorted(dates.values, pd.Timestamp(lo).to_datetime64(), side="left"))
        b = int(np.searchsorted(dates.values, pd.Timestamp(hi).to_datetime64(), side="right")) - 1
        bounds.append((a, b))
    return bounds
