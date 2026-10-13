"""Returns and costs (PREREG §2): synthetic 3x R* (judged), simple +-3i (reported), actual ETF C/O - 1, net."""
from __future__ import annotations

import numpy as np

SEC_FEE = 0.0000206  # 0.00206%, sell side only
COSTS = {"base": 0.0010, "stress": 0.0015, "sensitivity": 0.0005}  # one-way commission c; slippage 0


def r_star(i, g, side: str):
    """Exact 3x same-day return given the open gap: long 3(1+g)i/(1+3g), short -3(1+g)i/(1-3g); clipped at -1."""
    i, g = np.asarray(i, dtype=float), np.asarray(g, dtype=float)
    if side == "long":
        r = 3 * (1 + g) * i / (1 + 3 * g)
    elif side == "short":
        r = -3 * (1 + g) * i / (1 - 3 * g)
    else:
        raise ValueError(f"side must be long/short, got {side!r}")
    return np.maximum(r, -1.0)


def simple_3x(i, side: str):
    """Report-only +-3 i (no rebalancing correction), clipped at -1."""
    i = np.asarray(i, dtype=float)
    return np.maximum(3 * i if side == "long" else -3 * i, -1.0)


def net(r, cost: str = "base"):
    """Charter v2.4 §3: net = (1 + R)(1 - c - f) - (1 + c)."""
    c = COSTS[cost]
    return (1 + np.asarray(r, dtype=float)) * (1 - c - SEC_FEE) - (1 + c)
