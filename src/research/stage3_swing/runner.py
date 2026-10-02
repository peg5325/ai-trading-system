"""One-shot study driver (PREREG sections 3-6). Calibration runs before any strategy."""
from __future__ import annotations

import numpy as np

from . import contract as C
from .account import (
    full_period_stats,
    inverse_contribution,
    month_starts,
    score_paths,
    score_paths_after_tax,
    summarize_paths,
    window_growth,
)
from .bootstrap import (
    bootstrap_pvalues,
    holm_adjust,
    panel_arrays,
    run_bootstrap,
    series_for,
)
from .calibration import run_calibration
from .engine import era_bounds, index_paths
from .loading import RawData, build_panel

INVERSE_RULES = ("3i", "5i", "6i")
LIMITATIONS = (
    "Thresholds are monitored on daily closes only; intraday crossings are missed.",
    "Rules 7 and 8 hold a drifting 3x sleeve rebalanced monthly (and on 200-day switches for 8); full-weight rules have no drift.",
    "Cash earns 0% (Charter section 3); no validated interest rate.",
    (
        "Post-tax view is approximate: gains realised only on sells, tax paid at calendar-year end, "
        "no loss carry-forward, unrealised gains untaxed."
    ),
    "Pre-1999-03 NDX return is price-only (no dividends), a bias against longs.",
    "The 10-year horizon is 2520 trading days.",
)


def run_study(raw: RawData, provenance: dict, reps: int = C.BOOT_REPS, workers: int = 1) -> dict:
    panel = build_panel(raw)
    dates = panel.dates
    result: dict = {
        "prereg": provenance.get("prereg", {}),
        "provenance": provenance,
        "data": {
            "first_row": str(dates[0].date()),
            "last_row": str(dates[-1].date()),
            "rows": len(dates),
            "meta": panel.meta,
        },
    }
    result["calibration"] = run_calibration(panel)
    if not result["calibration"]["pass"]:
        result["status"] = "CALIBRATION_FAIL"
        return result

    n = len(dates)
    i0 = int(np.searchsorted(dates.values, np.datetime64(C.TRADE_START)))
    starts = month_starts(dates)
    eras = era_bounds(dates)
    years = np.asarray(dates.year)
    year_end = np.append(years[1:] != years[:-1], False)

    per_index: dict = {}
    observed = np.zeros((len(C.INDICES), len(C.CANDIDATE_IDS)))
    for ii, index in enumerate(C.INDICES):
        s = series_for(panel_arrays(panel), index)
        rmat, base = index_paths(s, index, C.COST_ONE_WAY)
        _, stress = index_paths(s, index, C.COST_STRESS)
        _, same = index_paths(s, index, C.COST_ONE_WAY, mode="same")
        rows: dict = {}
        for rule in C.RULE_IDS:
            prod, w, lc, drift = base[rule]
            paths = score_paths(lc, w, starts, C.COST_ONE_WAY, drift)
            row = summarize_paths(paths)
            row["full_period"] = full_period_stats(
                prod, w, lc, i0, n - 1, C.COST_ONE_WAY, dates, drift
            )
            row["era_growth"] = [
                window_growth(lc, w, a, b, C.COST_ONE_WAY, drift) for a, b in eras
            ]
            row["stress_cost"] = summarize_paths(
                score_paths(stress[rule][2], stress[rule][1], starts, C.COST_STRESS, stress[rule][3])
            )
            row["same_close"] = summarize_paths(
                score_paths(same[rule][2], same[rule][1], starts, C.COST_ONE_WAY, same[rule][3])
            )
            row["after_tax"] = summarize_paths(
                score_paths_after_tax(
                    prod, w, rmat, starts, C.COST_ONE_WAY, year_end,
                    drift.reb if drift is not None else None,
                )
            )
            if rule in INVERSE_RULES:
                row["inverse"] = inverse_contribution(prod, w, rmat, i0, n - 1)
            rows[rule] = row
        per_index[index] = rows
        for j, rule in enumerate(C.CANDIDATE_IDS):
            observed[ii, j] = rows[rule]["p_reach"] - rows["1"]["p_reach"]

    delta_star = run_bootstrap(
        {k: v[:i0] for k, v in panel_arrays(panel).items()},
        {k: v[i0:] for k, v in panel_arrays(panel).items()},
        starts - i0, C.COST_ONE_WAY, reps=reps, workers=workers,
    )
    p_raw = bootstrap_pvalues(delta_star)
    p_holm = holm_adjust(p_raw)

    candidates = []
    for ii, index in enumerate(C.INDICES):
        base_row = per_index[index]["1"]
        for j, rule in enumerate(C.CANDIDATE_IDS):
            row = per_index[index][rule]
            c1 = row["p_reach"] > base_row["p_reach"]
            c2 = row["p_stop"] <= C.P_STOP_MAX
            c3 = bool(p_holm[ii, j] <= C.ALPHA)
            era_ok = [
                g >= g1 for g, g1 in zip(row["era_growth"], base_row["era_growth"], strict=True)
            ]
            c4 = sum(era_ok) >= 2
            stress = row["stress_cost"]
            stress_ok = (
                stress["p_reach"] > per_index[index]["1"]["stress_cost"]["p_reach"]
                and stress["p_stop"] <= C.P_STOP_MAX
            )
            candidates.append({
                "index": index, "rule": rule,
                "delta_p_reach": float(observed[ii, j]),
                "p_raw": float(p_raw[ii, j]), "p_holm": float(p_holm[ii, j]),
                "c1_beats_1x": bool(c1), "c2_p_stop": bool(c2),
                "c3_holm": c3, "c4_eras": bool(c4), "eras_ge_1x": era_ok,
                "stress_cost_c1_c2": bool(stress_ok),
                "PASS": bool(c1 and c2 and c3 and c4),
            })
    passing = [f"{c['index']}:{c['rule']}" for c in candidates if c["PASS"]]
    result.update(
        status="PASS" if passing else "NO_PASS",
        passing=passing,
        per_index=per_index,
        candidates=candidates,
        bootstrap={
            "reps": reps, "seed": C.BOOT_SEED, "mean_block": C.BOOT_MEAN_BLOCK,
            "alpha": C.ALPHA, "comparisons": int(p_raw.size),
        },
        limitations=list(LIMITATIONS),
    )
    return result
