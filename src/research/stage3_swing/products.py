"""Synthetic product returns (PREREG section 3)."""
from __future__ import annotations

import numpy as np

from . import contract as C


def synthetic_return(ru, rf, leverage: float, er: float, spread: float = C.SPREAD):
    """r_L = L*r_u + (1-L)*rf - |L-1|*s/252 - ER_L/252 (arrays or scalars)."""
    return (
        leverage * np.asarray(ru, dtype=float)
        + (1.0 - leverage) * np.asarray(rf, dtype=float)
        - abs(leverage - 1.0) * spread / 252.0
        - er / 252.0
    )


def expense_ratio(index: str, leverage: float) -> float:
    if leverage == 1.0:
        return C.ER["sp_1x"] if index == "sp" else C.ER["ndx_1x"]
    return C.ER["lev"]


def return_matrix(ru, rf, index: str) -> np.ndarray:
    """Rows indexed by product code (CASH, X1, X2, X3, INV); cash returns 0."""
    ru = np.asarray(ru, dtype=float)
    mat = np.zeros((5, ru.shape[0]))
    for code, lev in C.LEVERAGE.items():
        mat[code] = synthetic_return(ru, rf, lev, expense_ratio(index, lev))
    return mat
