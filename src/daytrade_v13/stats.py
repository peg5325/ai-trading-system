"""Statistics and verdict for the single comparison v1.3-cap vs v1.0+ (v1.1 PREREG §6.4, borrowed by v1.3 §9).

- d = r(v1.3-cap) - r(v1.0+) per scored day (zero-trade days included as 0).
- Runs = maximal sequences of scored days that are consecutive sessions of a segment (A/B and every excluded
  mask day break a run). Inference population = days of runs with n > 5; m = their mean d.
- Circular block bootstrap: per run, blocks of 5 with starts uniform on 0..n-1, wrapping, concatenated and cut to
  n; all runs pooled into m*. B = 10,000, numpy.random.default_rng(20261007).
  p = (1 + #{m* - m >= m}) / (B + 1), d not re-centred. One-sided 5%.
- Insufficient information: no inference days (m undefined), all inference d == 0, or var(m*) == 0.
- Unadjusted 95% percentile interval of m*: numpy.quantile(method="linear").
- Verdict rows (first match; row 0 overrides; row 1 is v1.1's ② only and never applies here).
"""
from __future__ import annotations

import math

import numpy as np

SEED = 20261007
B = 10_000
BLOCK = 5
MIN_RUN = 5  # runs with n > MIN_RUN are tested
ALPHA = 0.05
D_MARGIN = 0.02  # "D more than 2 percentage points larger"

ROW_LABELS = {
    0: "무효",
    1: "검정 안 함",
    2: "기각(나빠짐)",
    3: "판단 불가(검정 정보 부족)",
    4: "기각(절대 손실)",
    5: "판단 불가",
    6: "채택 후보",
}


def runs_from_mask(segment: dict) -> list[list[str]]:
    """Contiguous runs of scored days in one mask segment. The segment's calendar sessions are exactly
    scored + excluded (`build_mask`), so any excluded session between two scored days breaks the run."""
    scored = set(segment["scored"])
    sessions = sorted(scored | {e["date"] for e in segment["excluded"]})
    runs: list[list[str]] = []
    cur: list[str] = []
    for day in sessions:
        if day in scored:
            cur.append(day)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def inference_runs(runs: list[list[str]]) -> list[list[str]]:
    return [r for r in runs if len(r) > MIN_RUN]


def bootstrap_indices(run_lengths: list[int], b: int = B, seed: int = SEED) -> np.ndarray:
    """(b, sum(n)) indices into the concatenated inference days. Draw order: one rng.integers((b, k)) call per
    run, runs in date order."""
    rng = np.random.default_rng(seed)
    parts = []
    offset = 0
    for n in run_lengths:
        k = -(-n // BLOCK)
        starts = rng.integers(0, n, size=(b, k))
        idx = (starts[:, :, None] + np.arange(BLOCK)[None, None, :]) % n
        parts.append(idx.reshape(b, k * BLOCK)[:, :n] + offset)
        offset += n
    return np.concatenate(parts, axis=1) if parts else np.zeros((b, 0), dtype=int)


def bootstrap_test(d_runs: list[list[float]], b: int = B, seed: int = SEED) -> dict:
    """Test H0: mean d <= 0 on the inference runs (each a list of d in date order)."""
    tested = [r for r in d_runs if len(r) > MIN_RUN]
    flat = np.array([x for r in tested for x in r], dtype=float)
    out = {"inference_days": int(flat.size), "inference_runs": len(tested), "m": None, "p": None,
           "ci95": None, "m_star_var": None, "insufficient": None}
    if flat.size == 0:
        out["insufficient"] = "no inference days"
        return out
    m = float(flat.mean())
    out["m"] = m
    if not np.any(flat != 0):
        out["insufficient"] = "all inference d are 0"
        return out
    idx = bootstrap_indices([len(r) for r in tested], b, seed)
    m_star = flat[idx].mean(axis=1)
    var = float(m_star.var(ddof=1))
    out["m_star_var"] = var
    if var == 0:
        out["insufficient"] = "m* variance is 0"
        return out
    out["p"] = float((1 + np.count_nonzero(m_star - m >= m)) / (b + 1))
    lo, hi = np.quantile(m_star, [0.025, 0.975], method="linear")
    out["ci95"] = [float(lo), float(hi)]
    return out


def daily_d(arm: dict, base: dict) -> dict[str, dict[str, float]]:
    """segment -> date -> d, from two `runner.run_arm` results over the same scored days."""
    out: dict[str, dict[str, float]] = {}
    for name, acc in arm["segments"].items():
        other = base["segments"][name]["days"]
        out[name] = {}
        for a, b in zip(acc["days"], other, strict=True):
            if a["date"] != b["date"]:
                raise ValueError(f"day order differs: {a['date']} vs {b['date']}")
            out[name][a["date"]] = a["pnl"]["day_return"] - b["pnl"]["day_return"]
    return out


def d_by_runs(d: dict[str, dict[str, float]], mask: dict) -> list[list[float]]:
    return [[d[name][day] for day in run] for name in d for run in runs_from_mask(mask["segments"][name])]


def inference_mean(d_runs: list[list[float]]) -> float | None:
    flat = [x for r in d_runs if len(r) > MIN_RUN for x in r]
    return sum(flat) / len(flat) if flat else None


def verdict(*, invalid: list[str], g_arm: float, g_base: float, d_arm: float, d_base: float, test: dict,
            m_stress: float | None) -> dict:
    """v1.1 §6.4 verdict table for v1.3-cap vs v1.0+ (first matching row)."""
    m = test["m"]
    for x in (g_arm, g_base, d_arm, d_base):
        if not math.isfinite(x):
            invalid = [*invalid, "non-finite G or D"]
    if invalid:
        row, why = 0, invalid
    elif (g_arm < g_base or d_arm - d_base > D_MARGIN or (m is not None and m < 0)
          or (m_stress is not None and m_stress < 0)):
        row, why = 2, [s for s, c in (("G < base G", g_arm < g_base), ("D > base D + 2%p", d_arm - d_base > D_MARGIN),
                                       ("m < 0", m is not None and m < 0),
                                       ("stress m < 0", m_stress is not None and m_stress < 0)) if c]
    elif test["insufficient"] or m == 0 or m_stress == 0 or g_arm == g_base:
        row, why = 3, [s for s, c in ((test["insufficient"], bool(test["insufficient"])), ("m = 0", m == 0),
                                       ("stress m = 0", m_stress == 0), ("G = base G", g_arm == g_base)) if c]
    elif g_arm <= 0:
        row, why = 4, ["G <= 0"]
    elif test["p"] > ALPHA:
        row, why = 5, [f"p = {test['p']:.4f} > 5%"]
    else:
        row, why = 6, [f"p = {test['p']:.4f} <= 5%"]
    return {"row": row, "label": ROW_LABELS[row], "why": why}
