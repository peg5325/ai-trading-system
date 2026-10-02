"""Stationary bootstrap + Holm (PREREG section 6.3)."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import contract as C
from .account import p_reach
from .engine import index_paths
from .loading import Panel
from .rules import series_from_arrays

_ARRAY_KEYS = ("rf", "vix", "is_me", "td_of_month", "td_from_end", "month")


def stationary_indices(length: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap indices (circular, geometric blocks)."""
    new = rng.random(length) < 1.0 / mean_block
    new[0] = True
    starts = rng.integers(0, length, size=length)
    block = np.cumsum(new) - 1
    block_pos = np.flatnonzero(new)[block]
    return (starts[new][block] + np.arange(length) - block_pos) % length


def panel_arrays(panel: Panel) -> dict[str, np.ndarray]:
    arr = {f"pr_{i}": panel.pr[i] for i in C.INDICES}
    arr.update({f"ru_{i}": panel.ru[i] for i in C.INDICES})
    arr.update({k: getattr(panel, k) for k in _ARRAY_KEYS})
    return arr


def series_for(arr: dict[str, np.ndarray], index: str):
    return series_from_arrays(
        arr[f"pr_{index}"], arr[f"ru_{index}"], arr["rf"], arr["vix"],
        arr["is_me"], arr["td_of_month"], arr["td_from_end"], arr["month"],
    )


def delta_p(arr: dict[str, np.ndarray], starts: np.ndarray, cost: float) -> np.ndarray:
    """Delta P_reach vs 1x for every (index, candidate rule); shape (2, 14)."""
    out = np.zeros((len(C.INDICES), len(C.CANDIDATE_IDS)))
    for i, index in enumerate(C.INDICES):
        _, paths = index_paths(series_for(arr, index), index, cost)
        base = p_reach(paths["1"][2], paths["1"][1], starts, cost)
        for j, rule in enumerate(C.CANDIDATE_IDS):
            _, w, lc = paths[rule]
            out[i, j] = p_reach(lc, w, starts, cost) - base
    return out


def replicate(arr_prep: dict, arr_trade: dict, starts_offset: np.ndarray, b: int, cost: float):
    """One bootstrap replication: real prep slice + resampled trade-period days."""
    length = len(arr_trade["rf"])
    rng = np.random.default_rng([C.BOOT_SEED, b])
    idx = stationary_indices(length, C.BOOT_MEAN_BLOCK, rng)
    prep_len = len(arr_prep["rf"])
    arr = {k: np.concatenate([arr_prep[k], arr_trade[k][idx]]) for k in arr_trade}
    return delta_p(arr, starts_offset + prep_len, cost)


_STATE: dict = {}


def _init(arr_prep, arr_trade, starts_offset, cost):
    _STATE.update(prep=arr_prep, trade=arr_trade, starts=starts_offset, cost=cost)


def _work(b: int) -> np.ndarray:
    return replicate(_STATE["prep"], _STATE["trade"], _STATE["starts"], b, _STATE["cost"])


def run_bootstrap(arr_prep, arr_trade, starts_offset, cost, reps=C.BOOT_REPS, workers=1) -> np.ndarray:
    """Returns delta-P* array of shape (reps, 2, 14)."""
    workers = max(1, min(workers, os.cpu_count() or 1))
    if workers == 1:
        _init(arr_prep, arr_trade, starts_offset, cost)
        return np.stack([_work(b) for b in range(reps)])
    with ProcessPoolExecutor(
        max_workers=workers, initializer=_init,
        initargs=(arr_prep, arr_trade, starts_offset, cost),
    ) as pool:
        return np.stack(list(pool.map(_work, range(reps), chunksize=4)))


def bootstrap_pvalues(delta_star: np.ndarray) -> np.ndarray:
    """p_k = (1 + #{dP*_k <= 0}) / (B + 1), shape (2, 14)."""
    reps = delta_star.shape[0]
    return (1.0 + (delta_star <= 0.0).sum(axis=0)) / (reps + 1.0)


def holm_adjust(p: np.ndarray) -> np.ndarray:
    flat = np.asarray(p, dtype=float).ravel()
    m = len(flat)
    order = np.argsort(flat, kind="stable")
    adj = np.minimum(1.0, (m - np.arange(m)) * flat[order])
    adj = np.maximum.accumulate(adj)
    out = np.empty(m)
    out[order] = adj
    return out.reshape(np.shape(p))
