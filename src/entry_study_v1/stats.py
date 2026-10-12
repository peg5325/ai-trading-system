"""Statistics and verdicts (PREREG §6.2, §7.2, §7.3, §8): block bootstrap, Holm, DEV_PASS, OOS selection/pass."""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

BASE_SEED = 20261012
B_DEFAULT = 10_000
BLOCK = 5

# Seed variants (PREREG §7.2): seed = BASE_SEED + 1000 * variant + k.
VARIANTS = {
    "main": 0, "hold_30m": 1, "hold_60m": 2, "stress": 3, "fee_only_0.20": 4, "fee_only_0.10": 5,
    "no_event_days": 6, "E5_1.0pct": 7, "E5_2.0pct": 8, "unconditional": 9, "oos_main": 50,
}


def seed_for(variant: int, k: int) -> int:
    return BASE_SEED + 1000 * variant + k


def sample_sd(x) -> float:
    """Sample sd (ddof=1); exactly 0.0 when all values are identical (avoids float residue from the mean)."""
    arr = np.asarray(x, dtype=float)
    if len(arr) < 2 or np.ptp(arr) == 0:
        return 0.0
    return float(np.std(arr, ddof=1))


def block_bootstrap_p(x, seg, seed: int, b_count: int = B_DEFAULT) -> float | None:
    """One-sided circular block-bootstrap p for H0: mean <= 0 (§7.2). x sorted by date; seg[i] its segment name.

    Segments with n_s <= 5 are dropped from the test only. Returns None when the test cannot be run
    (n < 2, zero sample sd, no testable segment, zero bootstrap variance)."""
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    if n < 2 or sample_sd(arr) == 0:
        return None
    seg = list(seg)
    order: list[str] = []
    for s in seg:
        if s not in order:
            order.append(s)
    parts = [(s, arr[[i for i, v in enumerate(seg) if v == s]]) for s in order]
    tested = [xs for s, xs in parts if len(xs) > BLOCK]
    if not tested:
        return None
    m_obs = float(np.mean(np.concatenate(tested)))
    rng = np.random.default_rng(seed)
    offsets = np.arange(BLOCK)
    m_star = np.empty(b_count)
    for b in range(b_count):
        samples = []
        for xs in tested:
            n_s = len(xs)
            starts = rng.integers(0, n_s, size=math.ceil(n_s / BLOCK))
            idx = ((starts[:, None] + offsets[None, :]) % n_s).ravel()
            samples.append(xs[idx][:n_s])
        m_star[b] = np.mean(np.concatenate(samples))
    if np.ptp(m_star) == 0 or float(np.var(m_star, ddof=1)) == 0:
        return None
    return float((1 + np.count_nonzero(m_star - m_obs >= m_obs)) / (b_count + 1))


def holm_adjust(p: dict[int, float | None], k_total: int) -> dict[int, float]:
    """Holm step-down over `k_total` cells. p maps cell index -> raw p (None = untestable). Cells with p None
    count in K, sort last and get adjusted p = 1."""
    if len(p) != k_total:
        raise ValueError(f"Holm needs {k_total} cells, got {len(p)}")
    ordered = sorted(p, key=lambda c: (p[c] is None, p[c] if p[c] is not None else 0.0, c))
    adj: dict[int, float] = {}
    running = 0.0
    for j, c in enumerate(ordered, start=1):
        if p[c] is None:
            adj[c] = 1.0
            continue
        running = max(running, min(1.0, (k_total - j + 1) * p[c]))
        adj[c] = running
    return adj


def summarize(x, seg, seed: int | None = None, b_count: int = B_DEFAULT) -> dict:
    """n, per-segment n/mean, mean, median, sd, t, p for one trade list (sorted by date)."""
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    out: dict = {"n": n, "n_by_segment": {}, "mean_by_segment": {}, "mean": None, "median": None, "t": None,
                 "p": None}
    for s in sorted(set(seg)):
        sel = arr[[i for i, v in enumerate(seg) if v == s]]
        out["n_by_segment"][s] = len(sel)
        out["mean_by_segment"][s] = float(np.mean(sel))
    if n:
        out["mean"] = float(np.mean(arr))
        out["median"] = float(np.median(arr))
        out["win_rate"] = float(np.mean(arr > 0))
    if n >= 2 and sample_sd(arr) > 0:
        out["t"] = float(np.mean(arr) / (sample_sd(arr) / math.sqrt(n)))
    if seed is not None:
        out["p"] = block_bootstrap_p(x, seg, seed, b_count)
    return out


def dev_pass(n: int, holm_p: float | None, mean_a: float | None, mean_b: float | None,
             mean_stress: float | None) -> dict:
    """The four DEV_PASS conditions (§7.3); every one must be true."""
    conds = {
        "holm_p_lt_0.05": holm_p is not None and holm_p < 0.05,
        "n_ge_30": n >= 30,
        "both_segments_positive": mean_a is not None and mean_b is not None and mean_a > 0 and mean_b > 0,
        "stress_mean_positive": mean_stress is not None and mean_stress > 0,
    }
    return {"conditions": conds, "pass": all(conds.values())}


def rank_dev_pass(passing: list[dict], max_cells: int = 2) -> list[int]:
    """§8 order: Holm p asc, base-cost mean desc, n desc, cell index asc. Entries: k, holm_p, mean, n."""
    ordered = sorted(passing, key=lambda c: (c["holm_p"], -c["mean"], -c["n"], c["k"]))
    return [c["k"] for c in ordered[:max_cells]]


def oos_verdict(n: int, mean: float | None, p: float | None) -> str:
    """OOS_PASS / OOS_INCONCLUSIVE (n < 10) / OOS_FAIL (§8). Gate failures (OOS_INVALID) are decided earlier."""
    if n < 10:
        return "OOS_INCONCLUSIVE"
    if mean is not None and mean > 0 and p is not None and p < 0.10:
        return "OOS_PASS"
    return "OOS_FAIL"


def power_delta(sd: float, n: int, alpha: float, power: float = 0.8) -> float:
    """Detectable mean net return per trade: (z_{1-alpha} + z_power) * sd / sqrt(n) (§6.2)."""
    nd = NormalDist()
    return (nd.inv_cdf(1 - alpha) + nd.inv_cdf(power)) * sd / math.sqrt(n)
