"""Statistics and verdicts (PREREG §6, §7, §8): block bootstrap with the B=200,000 boundary rerun, BH and BY,
Holm (reused), t, DEV_SURVIVOR conditions, ranking, HOLDOUT verdict.

`block_bootstrap_p` (one segment per call), `seed_for` and `sample_sd` come from entry_study_v1.stats, and the
pipeline uses its `holm_adjust` and `power_delta` directly. Only B, the rerun rule, BH and BY are new here.
"""
from __future__ import annotations

import math

import numpy as np

from src.entry_study_v1.stats import BLOCK, block_bootstrap_p, sample_sd, seed_for

B_MAIN = 10_000
B_RERUN = 200_000
RERUN_MAX_EXCEED = 50  # e <= 50 -> rerun with B_RERUN and variant + 4
RERUN_OFFSET = 4
Q_BH = 0.10
DEV_MIN_N = 100
HOLDOUT_MIN_N = 50
HOLM_ALPHA = 0.05
M_MAIN, M_GEX = 5, 2
VARIANTS = {"main_dev": 0, "main_dev_stress": 1, "main_dev_sensitivity": 2, "main_dev_actual_etf": 3,
            "gex_dev": 10, "main_holdout": 50, "gex_holdout": 60}


def _bootstrap_small(arr: np.ndarray, seed: int, b_count: int) -> float | None:
    """Same draw order as block_bootstrap_p, for 2 <= n <= BLOCK (which that function leaves untested)."""
    n = len(arr)
    m_obs = float(np.mean(arr))
    rng = np.random.default_rng(seed)
    offsets = np.arange(BLOCK)
    m_star = np.empty(b_count)
    for b in range(b_count):
        starts = rng.integers(0, n, size=math.ceil(n / BLOCK))
        m_star[b] = np.mean(arr[((starts[:, None] + offsets[None, :]) % n).ravel()][:n])
    if np.ptp(m_star) == 0 or float(np.var(m_star, ddof=1)) == 0:
        return None
    return float((1 + np.count_nonzero(m_star - m_obs >= m_obs)) / (b_count + 1))


def _one_p(arr: np.ndarray, seed: int, b_count: int) -> float | None:
    if len(arr) > BLOCK:
        return block_bootstrap_p(arr, ["all"] * len(arr), seed, b_count)
    return _bootstrap_small(arr, seed, b_count)


def exceed_count(p: float, b_count: int) -> int:
    """e from p = (1 + e) / (B + 1)."""
    return round(p * (b_count + 1)) - 1


def bootstrap_p(x, variant: int, k: int, b_count: int = B_MAIN) -> dict:
    """§7.1: one-sided p for H0 mean <= 0 with the boundary rerun. Returns {"p", "B", "seed", "rerun"}."""
    arr = np.asarray(x, dtype=float)
    seed = seed_for(variant, k)
    if len(arr) < 2 or sample_sd(arr) == 0:
        return {"p": None, "B": None, "seed": seed, "rerun": False}
    p = _one_p(arr, seed, b_count)
    if p is not None and exceed_count(p, b_count) <= RERUN_MAX_EXCEED:
        seed = seed_for(variant + RERUN_OFFSET, k)
        return {"p": _one_p(arr, seed, B_RERUN), "B": B_RERUN, "seed": seed, "rerun": True}
    return {"p": p, "B": b_count if p is not None else None, "seed": seed, "rerun": False}


def _step_up(p: dict[int, float | None], q: float) -> set[int]:
    k_total = len(p)
    if k_total == 0:
        return set()
    ordered = sorted(p, key=lambda c: (1.0 if p[c] is None else p[c], c))
    i_star = 0
    for i, c in enumerate(ordered, start=1):
        if (1.0 if p[c] is None else p[c]) <= i * q / k_total:
            i_star = i
    return set(ordered[:i_star])


def bh_reject(p: dict[int, float | None], q: float = Q_BH) -> set[int]:
    """Benjamini-Hochberg over the active candidates; p None counts as 1; ties by candidate number."""
    return _step_up(p, q)


def by_reject(p: dict[int, float | None], q: float = Q_BH) -> set[int]:
    """Benjamini-Yekutieli (report only): BH with q / sum_{i<=K} 1/i."""
    return _step_up(p, q / sum(1 / i for i in range(1, len(p) + 1)) if p else q)


def t_stat(x) -> float | None:
    arr = np.asarray(x, dtype=float)
    sd = sample_sd(arr)
    if len(arr) < 2 or sd == 0:
        return None
    return float(np.mean(arr) / (sd / math.sqrt(len(arr))))


def dev_survivor(*, bh_rejected: bool, n: int, stress_mean: float | None, half_means: tuple,
                 is_sox: bool, sox_etf_mean: float | None) -> dict:
    """§7.2 ①-⑤; all must be true. Means of zero trades are None (false); exactly 0 is false."""
    conds = {
        "1_bh_reject": bool(bh_rejected),
        "2_n_ge_100": n >= DEV_MIN_N,
        "3_stress_mean_gt_0": stress_mean is not None and stress_mean > 0,
        "4_both_halves_gt_0": all(m is not None and m > 0 for m in half_means),
    }
    if is_sox:
        conds["5_sox_actual_etf_mean_gt_0"] = sox_etf_mean is not None and sox_etf_mean > 0
    return {"conditions": conds, "survivor": all(conds.values())}


def rank_survivors(survivors: list[dict], m: int) -> list[int]:
    """Stress t desc, n desc, k asc; first m. Entries: k, stress_t, n."""
    ordered = sorted(survivors, key=lambda c: (-(c["stress_t"] if c["stress_t"] is not None else -math.inf),
                                                -c["n"], c["k"]))
    return [c["k"] for c in ordered[:m]]


def holdout_verdict(*, n: int, holm_adj: float, stress_mean: float | None, etf_mean: float | None) -> dict:
    """§8 / §9: HOLDOUT_INCONCLUSIVE (n < 50) / HOLDOUT_PASS / HOLDOUT_FAIL (reason FIDELITY when only the
    actual-ETF condition fails)."""
    synth = {"holm_adj_lt_0.05": holm_adj < HOLM_ALPHA,
             "stress_mean_gt_0": stress_mean is not None and stress_mean > 0}
    etf_ok = etf_mean is not None and etf_mean > 0
    conds = {**synth, "n_ge_50": n >= HOLDOUT_MIN_N, "actual_etf_base_mean_gt_0": etf_ok}
    if n < HOLDOUT_MIN_N:
        verdict, reason = "HOLDOUT_INCONCLUSIVE", "N_LT_50"
    elif all(conds.values()):
        verdict, reason = "HOLDOUT_PASS", None
    elif all(synth.values()) and not etf_ok:
        verdict, reason = "HOLDOUT_FAIL", "FIDELITY"
    else:
        verdict, reason = "HOLDOUT_FAIL", "SYNTHETIC"
    return {"conditions": conds, "verdict": verdict, "reason": reason}


def mean_or_none(x) -> float | None:
    arr = np.asarray(x, dtype=float)
    return float(np.mean(arr)) if len(arr) else None
