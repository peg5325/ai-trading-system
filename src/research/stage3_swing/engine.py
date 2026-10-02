"""Glue: rule positions -> per-rule log account factors for one index."""
from __future__ import annotations

import numpy as np

from . import contract as C
from .account import log_factors
from .products import return_matrix
from .rules import Series, rule_positions


def index_paths(s: Series, index: str, cost: float, mode: str = "lag"):
    """Returns (rmat, {rule: (prod, w, lc)}) for one index."""
    rmat = return_matrix(s.ru, s.rf, index)
    out = {}
    for rule, (prod, w) in rule_positions(s, mode).items():
        out[rule] = (prod, w, np.cumsum(log_factors(prod, w, rmat, cost)))
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
