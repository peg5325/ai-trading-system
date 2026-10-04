"""Blind power study for criterion 3 of the stage-3 swing v3 prereg (synthetic data only).

Question: which "correction for the number of candidates" (Charter v2.1 section 2, item 3)
keeps the family-wise false pass <= 5% in synthetic markets where the rules carry no
timing information, and passes a genuinely better rule most often in a synthetic market
where trend/volatility timing really helps?

Nothing here reads real prices or any stage-3 result. Every market is simulated from
fixed parameters (see SCENARIOS) with a fixed seed. Rules, products, costs, horizon and
account scoring are the repository's stage-3 code (src/research/stage3_swing); only the
first-hit scorer is re-implemented for speed (checked against account.score_paths by
`--selfcheck`).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.stage3_swing import contract as C
from src.research.stage3_swing.account import (
    log_factors,
    log_factors_drift,
    score_paths,
    window_growth,
)
from src.research.stage3_swing.bootstrap import stationary_indices
from src.research.stage3_swing.engine import era_bounds, index_paths
from src.research.stage3_swing.loading import calendar_flags
from src.research.stage3_swing.rules import series_from_arrays

SEED = 20261003
H = C.HORIZON_DAYS
COST = C.COST_ONE_WAY
LOG_STOP = float(np.log(C.STOP_FRACTION))
CAND = C.CANDIDATE_IDS  # 14 per index
INDICES = ("sp", "ndx")

# v3 data design (given): S&P 1928 -> 2025-07-18, monthly starts 1929-02..2015-07;
# Nasdaq-100 unchanged: prep 1993-02, starts 1994-02..2015-07. Synthetic weekday calendar.
CAL_START, CAL_END = "1928-01-03", "2025-07-18"
NDX_PREP = "1993-02-01"
START_WINDOWS = {"sp": ((1929, 2), (2015, 7)), "ndx": ((1994, 2), (2015, 7))}
RF_DAILY = 0.035 / 252
DIV_YIELD = {"sp": 0.02, "ndx": 0.007}

# --------------------------------------------------------------------------- scenarios
# Long-run log drift (CAGR) and volatility; NDX shock = RHO * S&P shock + idiosyncratic.
RHO_NDX = 0.85
TARGET = {"sp": (np.log(1.095), 0.18), "ndx": (np.log(1.12), 0.26)}

# Bull/bear regimes: (annual log drift, vol) per regime and mean durations in trading
# days (bull ~30 months, bear ~8 months, bull share 0.79). Unconditional drift/vol match
# TARGET. Justification and calibration check: design doc section 3.
REG_DUR = (630.0, 170.0)
REG = {"sp": ((0.18, 0.13), (-0.25, 0.30)), "ndx": ((0.24, 0.20), (-0.35, 0.42))}


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str  # iid | garch | regime
    edge: bool
    param: object = None


SCENARIOS = {
    # no timing information: i.i.d. fat-tailed (Student-t, 4 d.f.) days
    "N1_iid": Scenario("N1_iid", "iid", False, 4.0),
    # no information in the mean; volatility clusters (GARCH(1,1) a=0.08 b=0.91, t6)
    "N2_garch": Scenario("N2_garch", "garch", False, (0.08, 0.91, 6.0)),
    # no information in the mean; the edge market's persistent calm/turbulent vol regimes
    "N3_regime_vol": Scenario("N3_regime_vol", "regime", False, 0.0),
    # edge: bull/bear regimes with persistent mean and vol (gap 1.0 = full REG)
    "E1_regime": Scenario("E1_regime", "regime", True, 1.0),
    # sensitivity: half the bull/bear mean gap
    "E2_regime_half": Scenario("E2_regime_half", "regime", True, 0.5),
    # boundary stress (partial null): gap 0.65 puts S&P rules 7 and 8 just below dP = 0
    # (population -0.006 / -0.013) while NDX 7/8 are genuinely positive. Gap chosen from
    # population values only, before any criterion-3 configuration was evaluated.
    "N4_boundary": Scenario("N4_boundary", "regime", False, 0.65),
}


def _t_shocks(rng, n, nu):
    return rng.standard_t(nu, size=n) / np.sqrt(nu / (nu - 2.0))


def _regime_path(rng, n):
    p_stay = (1.0 - 1.0 / REG_DUR[0], 1.0 - 1.0 / REG_DUR[1])
    u = rng.random(n)
    s = np.empty(n, dtype=np.int8)
    s[0] = 0 if u[0] < REG_DUR[0] / sum(REG_DUR) else 1
    for t in range(1, n):
        s[t] = s[t - 1] if u[t] < p_stay[s[t - 1]] else 1 - s[t - 1]
    return s


def simulate_world(scn: Scenario, n: int, seed) -> dict:
    """Daily log returns for S&P and NDX plus a VIX proxy."""
    rng = np.random.default_rng(seed)
    e_idio = rng.standard_normal(n)
    mu = {k: np.full(n, TARGET[k][0] / 252) for k in TARGET}
    out = {}
    if scn.kind == "iid":
        z = _t_shocks(rng, n, scn.param)
        sd = {k: np.full(n, TARGET[k][1] / np.sqrt(252)) for k in TARGET}
    elif scn.kind == "garch":
        a, b, nu = scn.param
        z = _t_shocks(rng, n, nu)
        var_bar = TARGET["sp"][1] ** 2 / 252
        omega = var_bar * (1 - a - b)
        h = np.empty(n)
        h[0] = var_bar
        for t in range(1, n):
            h[t] = omega + a * h[t - 1] * z[t - 1] ** 2 + b * h[t - 1]
        scale = np.sqrt(h / var_bar)
        sd = {k: TARGET[k][1] / np.sqrt(252) * scale for k in TARGET}
    else:
        z = rng.standard_normal(n)
        s = _regime_path(rng, n)
        pi_b = REG_DUR[0] / sum(REG_DUR)
        sd = {}
        for k in TARGET:
            (mb, vb), (mr, vr) = REG[k]
            sd[k] = np.where(s == 0, vb, vr) / np.sqrt(252)
            bar = pi_b * mb + (1 - pi_b) * mr
            mu[k] = (bar + scn.param * (np.where(s == 0, mb, mr) - bar)) / 252
        out["regime"] = s
    out["x_sp"] = mu["sp"] + sd["sp"] * z
    out["x_ndx"] = mu["ndx"] + sd["ndx"] * (RHO_NDX * z + np.sqrt(1 - RHO_NDX**2) * e_idio)
    # VIX proxy: 1.15 x conditional S&P vol (variance premium) x persistent log-noise
    eta = rng.standard_normal(n) * 0.06
    u = np.zeros(n)
    for t in range(1, n):
        u[t] = 0.9 * u[t - 1] + eta[t]
    out["vix"] = 100 * 1.15 * sd["sp"] * np.sqrt(252) * np.exp(u)
    return out


# --------------------------------------------------------------------------- calendar
@dataclass
class Layout:
    dates: dict  # index -> DatetimeIndex
    flags: dict  # index -> calendar flags
    k: dict  # index -> first row in the full S&P calendar
    starts: dict  # index -> monthly start rows (local)
    prep: dict  # index -> rows before the first start (kept real in the bootstrap)
    eras: dict  # index -> [(a, b)] era rows (v1 eras, unchanged)
    n_full: int


def make_layout() -> Layout:
    full = pd.bdate_range(CAL_START, CAL_END)
    k = {"sp": 0, "ndx": int(np.searchsorted(full.values, np.datetime64(NDX_PREP)))}
    dates, flags, starts, prep, eras = {}, {}, {}, {}, {}
    for index in INDICES:
        d = full[k[index]:]
        dates[index] = d
        flags[index] = calendar_flags(d)
        (y0, m0), (y1, m1) = START_WINDOWS[index]
        ym = np.asarray(d.year * 100 + d.month)
        first = np.flatnonzero(np.r_[True, ym[1:] != ym[:-1]])
        starts[index] = first[(ym[first] >= y0 * 100 + m0) & (ym[first] <= y1 * 100 + m1)]
        prep[index] = int(starts[index][0])
        eras[index] = era_bounds(d)
    return Layout(dates, flags, k, starts, prep, eras, len(full))


def world_arrays(lay: Layout, sim: dict) -> dict:
    """Per-index panel arrays in the layout of bootstrap.panel_arrays."""
    out = {}
    for index in INDICES:
        k = lay.k[index]
        is_me, tdm, tde, month = lay.flags[index]
        ru = np.expm1(sim[f"x_{index}"][k:])
        out[index] = {
            "pr": ru - DIV_YIELD[index] / 252, "ru": ru,
            "rf": np.full(len(ru), RF_DAILY), "vix": sim["vix"][k:],
            "is_me": is_me, "td_of_month": tdm, "td_from_end": tde, "month": month,
        }
    return out


def _series(arr: dict):
    return series_from_arrays(
        arr["pr"], arr["ru"], arr["rf"], arr["vix"],
        arr["is_me"], arr["td_of_month"], arr["td_from_end"], arr["month"],
    )


# --------------------------------------------------------------------------- fast scoring
_LEVELS = 12  # 2**12 = 4096 > H


def _sparse(a: np.ndarray, fn, pad: float) -> list:
    tab = [np.concatenate([a, np.full(2**_LEVELS + 1, pad)])]
    for p in range(1, _LEVELS):
        prev, h = tab[-1], 2 ** (p - 1)
        nxt = np.full_like(prev, pad)
        nxt[: len(prev) - h] = fn(prev[: len(prev) - h], prev[h:])
        tab.append(nxt)
    return tab


def _first_hit(tab, pos0, thr, up: bool):
    """First row >= pos0 with value >= thr (up) or <= thr (down), by binary lifting."""
    pos = pos0.copy()
    for p in range(_LEVELS - 1, -1, -1):
        blk = tab[p][pos]
        pos = pos + np.where(blk < thr if up else blk > thr, 2**p, 0)
    return pos


def fast_score(lc: np.ndarray, w: np.ndarray, starts: np.ndarray, cost: float = COST):
    """(reached, stopped) per start: USD 4,000 first vs close <= 0.65 x running peak first.

    Uses the cumulative log factor lc; for the drifting rules 7/8 the partial segment right
    after a start follows the running sleeve instead of a fresh one (selfcheck bounds it).
    """
    n = len(lc)
    v0 = C.START_EQUITY * (1.0 - cost * w[starts])
    reach = _first_hit(_sparse(lc, np.maximum, -np.inf), starts + 1,
                       lc[starts] + np.log(C.TARGET_EQUITY / v0), up=True)
    reach = np.where((reach < n) & (reach - starts <= H), reach - starts, 10**9)
    i0 = int(starts.min())
    ii = np.arange(i0, n)
    d = _first_hit(_sparse(lc, np.minimum, np.inf), ii + 1, lc[ii] + LOG_STOP, up=False)
    sufmin = np.minimum.accumulate(np.where(d < n, d, 10**9)[::-1])[::-1]
    stop = sufmin[starts - i0]
    stop = np.where(stop - starts <= H, stop - starts, 10**9)
    return reach < stop, stop < reach


# --------------------------------------------------------------------------- statistics
def observe(arr: dict, index: str, lay: Layout):
    """P_reach, P_stop and era growth for the 15 rules; plus paths for the timing null."""
    rmat, paths = index_paths(_series(arr), index, COST)
    st = lay.starts[index]
    pr, ps = np.zeros(15), np.zeros(15)
    eg = np.zeros((15, len(lay.eras[index])))
    for r, rule in enumerate(C.RULE_IDS):
        _prod, w, lc, drift = paths[rule]
        reached, stopped = fast_score(lc, w, st)
        pr[r], ps[r] = reached.mean(), stopped.mean()
        eg[r] = [window_growth(lc, w, a, b, COST, drift) for a, b in lay.eras[index]]
    return pr, ps, eg, rmat, paths


def boot_delta(arr: dict, index: str, lay: Layout, block: int, rng) -> np.ndarray:
    """One stationary-bootstrap replication (v1 section 6.3) for one index: dP* (14,)."""
    prep = lay.prep[index]
    idx = prep + stationary_indices(len(arr["rf"]) - prep, block, rng)
    res = {k: np.concatenate([v[:prep], v[idx]]) for k, v in arr.items()}
    _, paths = index_paths(_series(res), index, COST)
    st = lay.starts[index]
    base = fast_score(paths["1"][2], paths["1"][1], st)[0].mean()
    return np.array([fast_score(paths[r][2], paths[r][1], st)[0].mean() - base for r in CAND])


def shift_delta(paths: dict, rmat, lay: Layout, index: str, base: float, offset: int) -> np.ndarray:
    """Random-timing null: each candidate's executed positions rolled by `offset` rows,
    so exposure level and holding-period pattern are kept but timing is random."""
    st = lay.starts[index]
    out = np.zeros(len(CAND))
    for j, rule in enumerate(CAND):
        prod, w, _, drift = paths[rule]
        prod_s, w_s = np.roll(prod, offset), np.roll(w, offset)
        if drift is not None:
            lc = np.cumsum(log_factors_drift(w_s, np.roll(drift.reb, offset), rmat[C.X3], COST))
        else:
            lc = np.cumsum(log_factors(prod_s, w_s, rmat, COST))
        out[j] = fast_score(lc, w_s, st)[0].mean() - base
    return out


# --------------------------------------------------------------------------- tests
B_V1 = 1000  # v1 6.3 as written
B_SMALL = 300  # other bootstrap configurations (smooth p-values need fewer draws)
R_TIMING = 600  # random-timing draws; 1/(R+1) < 0.05/28 so Holm can pass
BLOCKS = (250, 1000, 2520)
# simulated histories per scenario (null markets need more: few worlds reach the tests)
STUDY_WORLDS = {"N1_iid": 1000, "N2_garch": 1000, "N3_regime_vol": 1000,
                "E1_regime": 300, "E2_regime_half": 200, "N4_boundary": 400}


def holm_adjust(p: np.ndarray) -> np.ndarray:
    order = np.argsort(p, kind="stable")
    m = len(p)
    adj = np.maximum.accumulate(np.minimum(1.0, (m - np.arange(m)) * p[order]))
    out = np.empty(m)
    out[order] = adj
    return out


def rw_stepdown(t_obs: np.ndarray, t_null: np.ndarray, alpha: float = C.ALPHA) -> np.ndarray:
    """Romano-Wolf step-down on the max statistic (one-sided); rows of t_null are draws."""
    rej = np.zeros(t_obs.shape, dtype=bool)
    active = np.isfinite(t_obs)
    while active.any():
        crit = np.quantile(t_null[:, active].max(axis=1), 1 - alpha)
        new = active & (t_obs > crit)
        if not new.any():
            break
        rej |= new
        active &= ~new
    return rej


def _sd(d: np.ndarray):
    sd = d.std(axis=0, ddof=1)
    return np.where(sd > 0, sd, np.nan)


class norm:  # standard normal tails without scipy
    _erfc = np.frompyfunc(math.erfc, 1, 1)

    @staticmethod
    def sf(x):
        return (0.5 * norm._erfc(np.asarray(x, dtype=float) / math.sqrt(2.0))).astype(float)

    @staticmethod
    def cdf(x):
        return norm.sf(-np.asarray(x, dtype=float))


def _holm_within(p: np.ndarray, keep: np.ndarray) -> np.ndarray:
    out = np.zeros(len(p), dtype=bool)
    if keep.any():
        out[keep] = holm_adjust(p[keep]) <= C.ALPHA
    return out


def criterion3(dp_hat: np.ndarray, boot: dict, rt: np.ndarray, c124: np.ndarray) -> dict:
    """Criterion-3 decision (28 bools) per configuration. boot: block -> (B, 28) dP*.

    Rows prefixed REF_ are references that do not correct for all 28 comparisons (not
    Charter-compliant, never selectable): Holm only over the candidates that already pass
    criteria 1, 2 and the era check, and an unadjusted one-sided 5% test.
    """
    a = C.ALPHA
    out = {}
    for L in (250, 2520):
        d = boot[L][:B_SMALL]
        p_z = norm.sf(np.nan_to_num(dp_hat / _sd(d), nan=-np.inf))
        out[f"REF_SB{L}_z_HolmScreened"] = _holm_within(p_z, c124)
        out[f"REF_SB{L}_z_unadjusted"] = p_z <= a
    for L, d in boot.items():
        if L == 250:  # v1 6.3 as written: count p-value, B = 1000, Holm
            p = (1.0 + (d <= 0.0).sum(axis=0)) / (d.shape[0] + 1.0)
            out["V1_SB250_pct_Holm"] = holm_adjust(p) <= a
            d = d[:B_SMALL]
        mean, sd = d.mean(axis=0), _sd(d)
        # percentile form, smoothed: P*(dP* <= 0) ~ Phi(-mean*/sd*)
        out[f"SB{L}_pctz_Holm"] = holm_adjust(np.nan_to_num(norm.cdf(-mean / sd), nan=1.0)) <= a
        # recentred (null-imposed) z-test: p = 1 - Phi(dP_hat / sd*)
        t_obs = np.nan_to_num(dp_hat / sd, nan=-np.inf)
        out[f"SB{L}_z_Holm"] = holm_adjust(norm.sf(t_obs)) <= a
        out[f"SB{L}_maxT_RW"] = rw_stepdown(t_obs, np.nan_to_num((d - mean) / sd, nan=-np.inf))
    # random-timing null: same positions shifted in time (same exposure, random timing)
    p_rt = (1.0 + (rt >= dp_hat[None, :]).sum(axis=0)) / (rt.shape[0] + 1.0)
    out["RT_Holm"] = holm_adjust(p_rt) <= a
    m_rt, s_rt = rt.mean(axis=0), _sd(rt)
    out["RT_maxT_RW"] = rw_stepdown(
        np.nan_to_num((dp_hat - m_rt) / s_rt, nan=-np.inf),
        np.nan_to_num((rt - m_rt) / s_rt, nan=-np.inf),
    )
    return out


# --------------------------------------------------------------------------- workers
_LAY = None


def _layout() -> Layout:
    global _LAY
    if _LAY is None:
        _LAY = make_layout()
    return _LAY


def _seed(scn_name: str, *rest) -> list:
    return [SEED, list(SCENARIOS).index(scn_name), *rest]


def world_obs(scn_name: str, w: int):
    lay = _layout()
    sim = simulate_world(SCENARIOS[scn_name], lay.n_full, _seed(scn_name, w))
    arr = world_arrays(lay, sim)
    return sim, arr, {index: observe(arr[index], index, lay) for index in INDICES}


def criteria_124(obs: dict) -> tuple:
    """dP_hat, criteria 1 and 2 and the era check (v1 6.1, 6.2, 6.4) for all 28."""
    dp, c1, c2, era, ps = [], [], [], [], []
    for index in INDICES:
        pr, p_stop, eg, _, _ = obs[index]
        dp.append(pr[1:] - pr[0])
        c1.append(pr[1:] > pr[0])
        c2.append(p_stop[1:] <= C.P_STOP_MAX)
        era.append((eg[1:] >= eg[0][None, :]).sum(axis=1) >= 2)
        ps.append(p_stop[1:])
    cat = np.concatenate
    return cat(dp), cat(c1), cat(c2), cat(era), cat(ps), np.r_[obs["sp"][0][0], obs["ndx"][0][0]]


def calibration_stats(sim: dict) -> dict:
    """S&P stylised facts of one simulated 1929-2025 history: 1x buy-and-hold vs the
    published 10-month-average timing test at 1x (out of market = T-bill)."""
    lay = _layout()
    ru = np.expm1(sim["x_sp"])
    px = np.cumprod(1 + ru - DIV_YIELD["sp"] / 252)
    is_me = lay.flags["sp"][0]
    me = np.flatnonzero(is_me)
    above = px[me] > pd.Series(px[me]).rolling(10).mean().to_numpy()
    sig = np.full(len(ru), np.nan)
    sig[me] = above
    sig = pd.Series(sig).ffill().fillna(0).to_numpy()
    held = np.r_[0.0, 0.0, sig[:-2]]  # decided at close t, traded at close t+1
    a = lay.prep["sp"]
    out = {}
    for k, r in (("bh", ru[a:]), ("timing", (held * ru + (1 - held) * RF_DAILY)[a:])):
        lv = np.cumprod(1 + r)
        out[k] = {
            "cagr": float(lv[-1] ** (252 / len(r)) - 1),
            "vol": float(r.std() * np.sqrt(252)),
            "maxdd": float((lv / np.maximum.accumulate(lv)).min() - 1),
            "sharpe": float((r.mean() - RF_DAILY) / r.std() * np.sqrt(252)),
        }
    return out


def truth_world(args) -> dict:
    scn_name, w = args
    sim, _, obs = world_obs(scn_name, w)
    dp, c1, c2, era, ps, base = criteria_124(obs)
    return {"dp": dp, "ps": ps, "c1": c1, "c2": c2, "era": era, "base": base,
            "calib": calibration_stats(sim)}


def study_world(args) -> dict:
    scn_name, w = args
    lay = _layout()
    _, arr, obs = world_obs(scn_name, w)
    dp, c1, c2, era, _ps, _ = criteria_124(obs)
    c124 = c1 & c2 & era
    res = {"w": w, "dp": dp, "c124": c124, "c3": None}
    if not c124.any():  # no candidate can PASS whatever criterion 3 says
        return res
    boot = {}
    for L in BLOCKS:
        reps = B_V1 if L == 250 else B_SMALL
        draws = np.zeros((reps, 28))
        for i, index in enumerate(INDICES):
            rng = np.random.default_rng(_seed(scn_name, w, L, i))
            for b in range(reps):
                draws[b, 14 * i:14 * (i + 1)] = boot_delta(arr[index], index, lay, L, rng)
        boot[L] = draws
    rng = np.random.default_rng(_seed(scn_name, w, 99))
    u = rng.random(R_TIMING)
    rt = np.zeros((R_TIMING, 28))
    for i, index in enumerate(INDICES):
        pr, _, _, rmat, paths = obs[index]
        n = len(arr[index]["rf"])
        for b in range(R_TIMING):
            off = 252 + int(u[b] * (n - 504))  # same draw shifts both indices jointly
            rt[b, 14 * i:14 * (i + 1)] = shift_delta(paths, rmat, lay, index, pr[0], off)
    res["c3"] = criterion3(dp, boot, rt, c124)
    res["boot_mean"] = {L: d.mean(axis=0) for L, d in boot.items()}
    res["boot_sd"] = {L: d.std(axis=0, ddof=1) for L, d in boot.items()}
    res["rt_mean"], res["rt_sd"] = rt.mean(axis=0), rt.std(axis=0, ddof=1)
    return res


def _pool_map(fn, jobs, workers, log=None):
    out = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for k, r in enumerate(pool.map(fn, jobs, chunksize=1)):
            out.append(r)
            if log and (k + 1) % 25 == 0:
                print(f"  {log}: {k + 1}/{len(jobs)} worlds, {time.time() - t0:.0f}s", flush=True)
    return out


# --------------------------------------------------------------------------- aggregation
LABELS = [f"{index}:{rule}" for index in INDICES for rule in CAND]


def _mc(p: float, n: int) -> float:
    return float(np.sqrt(max(p * (1 - p), 1e-12) / n))


def summarize_truth(rows: list) -> dict:
    dp = np.stack([r["dp"] for r in rows])
    ps = np.stack([r["ps"] for r in rows])
    calib = {k: {m: float(np.mean([r["calib"][k][m] for r in rows])) for m in ("cagr", "vol", "maxdd", "sharpe")}
             for k in ("bh", "timing")}
    return {
        "worlds": len(rows),
        "base_p_reach": np.stack([r["base"] for r in rows]).mean(axis=0).tolist(),
        "dp_mean": dp.mean(axis=0).tolist(),
        "dp_se": (dp.std(axis=0, ddof=1) / np.sqrt(len(rows))).tolist(),
        "dp_sd": dp.std(axis=0, ddof=1).tolist(),
        "p_stop_mean": ps.mean(axis=0).tolist(),
        "rate_c1": np.mean([r["c1"] for r in rows], axis=0).tolist(),
        "rate_c2": np.mean([r["c2"] for r in rows], axis=0).tolist(),
        "rate_era": np.mean([r["era"] for r in rows], axis=0).tolist(),
        "rate_c124": np.mean([r["c1"] & r["c2"] & r["era"] for r in rows], axis=0).tolist(),
        "calibration_sp_1929_2025": calib,
    }


def edge_sets(truth: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Population truth from the truth run: null = mean dP <= 0; edge = mean dP > 0;
    edge_c2 = edge and mean P_stop <= 0.25 (a candidate the Charter should adopt)."""
    dp = np.asarray(truth["dp_mean"])
    ps = np.asarray(truth["p_stop_mean"])
    return dp <= 0, dp > 0, (dp > 0) & (ps <= C.P_STOP_MAX)


def pass_metrics(passes: np.ndarray, c124: np.ndarray, sets: tuple) -> dict:
    """passes, c124: (worlds, 28) bools. False pass, power and their Monte-Carlo errors."""
    null_set, edge_set, edge_c2 = sets
    n = len(passes)
    anyp = passes.any(axis=1).mean()
    strong = passes[:, null_set].any(axis=1).mean() if null_set.any() else 0.0
    res = {
        "any_pass": float(anyp), "any_pass_mc": _mc(anyp, n),
        "false_pass_strong": float(strong), "false_pass_strong_mc": _mc(strong, n),
        "per_candidate_pass": {LABELS[k]: float(passes[:, k].mean()) for k in range(28)
                               if passes[:, k].any()},
    }
    for key, s in (("power", edge_set), ("power_c2", edge_c2)):
        if s.any():
            pw = passes[:, s].any(axis=1).mean()
            res[key], res[key + "_mc"] = float(pw), _mc(pw, n)
    ceiling = c124[:, edge_set].any(axis=1)  # an edge candidate passes 1, 2 and the era check
    if edge_set.any() and ceiling.any():
        # criterion 3 alone: of those worlds, the share where criterion 3 lets one through
        cp = passes[ceiling][:, edge_set].any(axis=1).mean()
        res["power_given_c124"], res["power_given_c124_mc"] = float(cp), _mc(cp, int(ceiling.sum()))
    return res


def oracle_bounds(truth_rows: list, sets: tuple) -> dict:
    """Best case for any valid test: z = dP_hat / (true sampling sd across worlds)."""
    dp = np.stack([r["dp"] for r in truth_rows])
    c124 = np.stack([r["c1"] & r["c2"] & r["era"] for r in truth_rows])
    p = norm.sf(dp / dp.std(axis=0, ddof=1))
    holm = np.stack([holm_adjust(row) <= C.ALPHA for row in p])
    return {
        "ORACLE_trueSD_Holm28": pass_metrics(c124 & holm, c124, sets),
        "ORACLE_trueSD_unadjusted": pass_metrics(c124 & (p <= C.ALPHA), c124, sets),
        "NO_CORRECTION": pass_metrics(c124, c124, sets),
    }


def summarize_study(rows: list, truth: dict, scn: Scenario) -> dict:
    n = len(rows)
    sets = edge_sets(truth)
    null_set, edge_set, edge_c2 = sets
    configs = next((list(r["c3"]) for r in rows if r["c3"] is not None), [])
    c124 = np.stack([r["c124"] for r in rows])
    out = {"worlds": n, "eligible_worlds": int(sum(r["c3"] is not None for r in rows)),
           "null_set": [LABELS[k] for k in np.flatnonzero(null_set)],
           "edge_set": [LABELS[k] for k in np.flatnonzero(edge_set)],
           "edge_c2_set": [LABELS[k] for k in np.flatnonzero(edge_c2)],
           "c124_any_rate": float(c124.any(axis=1).mean()),
           "configs": {}}
    for cfg in configs + ["NO_CORRECTION"]:
        passes = np.zeros((n, 28), dtype=bool)
        for k, r in enumerate(rows):
            if r["c3"] is None:
                continue
            c3 = np.ones(28, dtype=bool) if cfg == "NO_CORRECTION" else r["c3"][cfg]
            passes[k] = r["c124"] & c3
        out["configs"][cfg] = pass_metrics(passes, c124, sets)
    # v1 exact (B = 1000 count p) vs its smoothed twin at the same block, for the record
    agree = [np.array_equal(r["c3"]["V1_SB250_pct_Holm"], r["c3"]["SB250_pctz_Holm"])
             for r in rows if r["c3"] is not None]
    out["v1_exact_vs_smoothed_agreement"] = float(np.mean(agree)) if agree else None
    return out


# --------------------------------------------------------------------------- selfcheck
def selfcheck(worlds: int = 3) -> None:
    """fast_score must equal account.score_paths (repository scorer) on synthetic worlds."""
    lay = _layout()
    worst = 0.0
    for scn_name in SCENARIOS:
        for w in range(worlds):
            _, _arr, obs = world_obs(scn_name, 10_000 + w)
            for index in INDICES:
                paths = obs[index][4]
                for rule in C.RULE_IDS:
                    _prod, wt, lc, drift = paths[rule]
                    ex = score_paths(lc, wt, lay.starts[index], COST, drift)
                    fr, fs = fast_score(lc, wt, lay.starts[index])
                    worst = max(worst, abs(ex["reached"].mean() - fr.mean()),
                                abs(ex["stopped"].mean() - fs.mean()))
    print(f"selfcheck: max |P difference| fast vs repository scorer = {worst:.4f}")
    if worst > 0.01:
        raise SystemExit("fast scorer deviates from the repository scorer")


NULLS = ("N1_iid", "N2_garch", "N3_regime_vol")


def table(out: Path) -> str:
    """Markdown table: false pass in the no-edge markets, boundary stress, power, MC error."""
    st = {n: json.loads((out / f"study_{n}.json").read_text()) for n in SCENARIOS
          if (out / f"study_{n}.json").exists()}
    tr = {n: json.loads((out / f"truth_{n}.json").read_text()) for n in SCENARIOS
          if (out / f"truth_{n}.json").exists()}
    cfgs = list(st["E1_regime"]["configs"]) if "E1_regime" in st else []

    def pct(x, mc=None):
        return "-" if x is None else (f"{100 * x:.1f}" + ("" if mc is None else f" ±{100 * mc:.1f}"))

    head = ("| config | " + " | ".join(f"{n} any-pass" for n in NULLS)
            + " | N4 false pass | E1 power | E1 power given 1·2·era | E2 power |")
    lines = [head, "|" + "---|" * (head.count("|") - 1)]
    for c in cfgs + ["ORACLE_trueSD_Holm28", "ORACLE_trueSD_unadjusted"]:
        src = (lambda n, c=c: st.get(n, {}).get("configs", {}).get(c)) if not c.startswith("ORACLE") else \
              (lambda n, c=c: tr.get(n, {}).get("oracle", {}).get(c))
        row = [c]
        for n in NULLS:
            r = src(n)
            row.append(pct(r["any_pass"], r["any_pass_mc"]) if r else "-")
        r = src("N4_boundary")
        row.append(pct(r["false_pass_strong"], r["false_pass_strong_mc"]) if r else "-")
        for n, key in (("E1_regime", "power"), ("E1_regime", "power_given_c124"), ("E2_regime_half", "power")):
            r = src(n)
            row.append(pct(r.get(key), r.get(key + "_mc")) if r else "-")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.floating, np.integer, np.bool_)):
        return x.item()
    return x


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--truth", action="store_true", help="population values per scenario")
    ap.add_argument("--study", action="store_true", help="false pass / power per configuration")
    ap.add_argument("--table", action="store_true", help="print the results table from --out")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--truth-worlds", type=int, default=2000)
    ap.add_argument("--worlds", type=int, default=None, help="override STUDY_WORLDS")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=os.environ.get("CLAUDE_JOB_DIR", ".") + "/criterion3_out")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = args.scenarios.split(",")
    if args.selfcheck:
        selfcheck()
    if args.truth:
        for name in names:
            t0 = time.time()
            rows = _pool_map(truth_world, [(name, 1_000_000 + w) for w in range(args.truth_worlds)],
                             args.workers)
            res = summarize_truth(rows)
            res["oracle"] = oracle_bounds(rows, edge_sets(res))
            res["seconds"] = time.time() - t0
            (out / f"truth_{name}.json").write_text(json.dumps(_jsonable(res), indent=1))
            print(name, json.dumps(_jsonable(res["calibration_sp_1929_2025"])), f"{res['seconds']:.0f}s")
    if args.study:
        for name in names:
            truth = json.loads((out / f"truth_{name}.json").read_text())
            t0 = time.time()
            worlds = args.worlds or STUDY_WORLDS[name]
            rows = _pool_map(study_world, [(name, w) for w in range(worlds)], args.workers, log=name)
            np.save(out / f"study_{name}.npy", np.array(rows, dtype=object), allow_pickle=True)
            res = summarize_study(rows, truth, SCENARIOS[name])
            res["seconds"] = time.time() - t0
            (out / f"study_{name}.json").write_text(json.dumps(_jsonable(res), indent=1))
            print(name, f"{res['seconds']:.0f}s", flush=True)
    if args.table:
        print(table(out))


if __name__ == "__main__":
    main()
