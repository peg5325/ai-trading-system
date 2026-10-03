"""Explore (selection) and confirm (one-shot verdict) drivers (PREREG sections 3-4)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.stage3_swing import contract as SC
from src.research.stage3_swing.loading import Panel, RawData, build_panel
from src.research.stage3_swing.products import return_matrix

from . import contract as C
from .engine import (
    COMBOS,
    classify,
    evaluate_combos,
    held_labels,
    path_stats,
    positions,
    regime_breakdown,
    select,
    window_bounds,
    window_factors,
)

TIMING_NOTE = (
    "Regime decided at the close of t; exposure set at the close of t+1 (earns t+2 onward). "
    "Per-regime sums label each day by the regime that decided the position held that day; "
    "each day's log factor includes the trade cost charged at that day's close. "
    "The initial buy cost at the opening close is in the totals but in no regime's sum."
)
TIE_BREAK_NOTE = (
    "Feasible = max drawdown < 35%. Highest net log growth (ties within 1e-12); then smaller "
    "sum of |exposure|; then lexicographic (A,B,C,D) with exposure order [0, 1, 2, 3, -1]."
)


def cut_raw(raw: RawData, end) -> RawData:
    """Drop every raw row dated after `end` before any computation."""
    ts = pd.Timestamp(end)
    out = RawData(
        yahoo={k: v[v.index <= ts] for k, v in raw.yahoo.items()},
        cboe_vix=raw.cboe_vix[raw.cboe_vix.index <= ts],
        rf=raw.rf[raw.rf.index <= ts],
    )
    assert all(len(v) == 0 or v.index.max() <= ts for v in out.yahoo.values())
    return out


def index_inputs(panel: Panel, index: str):
    """(labels, rmat) for one index: regime per row from the signal level and VIX, product returns."""
    px = np.cumprod(1.0 + np.asarray(panel.pr[index], dtype=float))
    return classify(px, panel.vix), return_matrix(panel.ru[index], panel.rf, index)


def _one_x(n: int):
    return np.full(n, SC.X1, dtype=np.int8), np.ones(n)


def _combo_dict(combo) -> dict:
    return dict(zip(C.REGIMES, (int(e) for e in combo), strict=True))


def _data_info(dates) -> dict:
    return {"first_row": str(dates[0].date()), "last_row": str(dates[-1].date()), "rows": len(dates)}


def run_explore(raw: RawData, provenance: dict) -> dict:
    """Evaluate all 625 combos per index on the explore window and pick one (prereg section 3)."""
    panel = build_panel(cut_raw(raw, C.EXPLORE_END))
    dates = panel.dates
    assert dates.max() <= pd.Timestamp(C.EXPLORE_END), "post-explore row reached the panel"
    a, b = window_bounds(dates, C.EXPLORE_START, C.EXPLORE_END)
    out: dict = {
        "prereg": provenance.get("prereg", {}),
        "provenance": provenance,
        "phase": "explore",
        "explore_window": {"start": str(dates[a].date()), "end": str(dates[b].date())},
        "data": _data_info(dates),
        "cost_one_way": C.COST_ONE_WAY,
        "selection_rule": TIE_BREAK_NOTE,
        "timing": TIMING_NOTE,
        "indices": {},
    }
    n = len(dates)
    for index in C.INDICES:
        labels, rmat = index_inputs(panel, index)
        growth, mdd = evaluate_combos(labels, rmat, a, b, C.COST_ONE_WAY)
        pick = select(growth, mdd)
        p1, w1 = _one_x(n)
        _, lf1 = window_factors(p1, w1, rmat, a, b, C.COST_ONE_WAY)
        row: dict = {
            "n_combos": len(COMBOS),
            "n_feasible": int((-mdd < C.MDD_LIMIT).sum()),
            "one_x": path_stats(p1, w1, rmat, a, b, C.COST_ONE_WAY, dates),
        }
        if pick is None:
            row["status"] = "NO_CANDIDATE"
        else:
            combo = COMBOS[pick]
            prod, w, pl = positions(labels, combo)
            _, lf = window_factors(prod, w, rmat, a, b, C.COST_ONE_WAY)
            row.update(
                status="SELECTED",
                combo=_combo_dict(combo),
                explore=path_stats(prod, w, rmat, a, b, C.COST_ONE_WAY, dates),
                explore_by_regime=regime_breakdown(lf, lf1, held_labels(pl, a, b)),
            )
        out["indices"][index] = row
    return out


def run_confirm(raw: RawData, selection: dict, provenance: dict) -> dict:
    """One-shot confirm on 2020-01-02..2025-07-18 with fresh USD 2,000 (prereg section 4)."""
    panel = build_panel(cut_raw(raw, C.CONFIRM_END))
    dates = panel.dates
    assert dates.max() <= pd.Timestamp(C.CONFIRM_END), "post-confirm row reached the panel"
    a, b = window_bounds(dates, C.CONFIRM_START, C.CONFIRM_END)
    n = len(dates)
    out: dict = {
        "prereg": provenance.get("prereg", {}),
        "provenance": provenance,
        "phase": "confirm",
        "confirm_window": {"start": str(dates[a].date()), "end": str(dates[b].date())},
        "data": _data_info(dates),
        "timing": TIMING_NOTE,
        "indices": {},
    }
    c3_any = []
    for index in C.INDICES:
        sel = selection["indices"][index]
        row: dict = {"explore": {k: sel.get(k) for k in ("status", "combo", "explore", "one_x", "n_feasible")}}
        if sel["status"] != "SELECTED":
            row["verdict"] = sel["status"]
            row["criteria"] = {"c1_twr_beats_1x": False, "c2_mdd_below_35": False, "c3_some_regime_beats_1x": False}
            c3_any.append(False)
            out["indices"][index] = row
            continue
        labels, rmat = index_inputs(panel, index)
        combo = tuple(int(sel["combo"][r]) for r in C.REGIMES)
        p1, w1 = _one_x(n)
        variants = {}
        for name, mode, cost in (
            ("base", "lag", C.COST_ONE_WAY),
            ("stress_cost", "lag", C.COST_STRESS),
            ("same_close", "same", C.COST_ONE_WAY),
        ):
            prod, w, pl = positions(labels, combo, mode)
            _, lf = window_factors(prod, w, rmat, a, b, cost)
            _, lf1 = window_factors(p1, w1, rmat, a, b, cost)
            variants[name] = {
                "mode": mode,
                "cost_one_way": cost,
                "strategy": path_stats(prod, w, rmat, a, b, cost, dates),
                "one_x": path_stats(p1, w1, rmat, a, b, cost, dates),
                "by_regime": regime_breakdown(lf, lf1, held_labels(pl, a, b)),
            }
        base = variants["base"]
        c1 = base["strategy"]["twr_annualized"] > base["one_x"]["twr_annualized"]
        c2 = -base["strategy"]["max_drawdown"] < C.MDD_LIMIT
        c3 = any(r["beats_1x"] for r in base["by_regime"].values())
        row.update(
            combo=_combo_dict(combo),
            criteria={"c1_twr_beats_1x": bool(c1), "c2_mdd_below_35": bool(c2), "c3_some_regime_beats_1x": bool(c3)},
            verdict="PASS" if (c1 and c2 and c3) else "FAIL",
            **variants,
        )
        c3_any.append(bool(c3))
        out["indices"][index] = row
    out["verdicts"] = {k: v["verdict"] for k, v in out["indices"].items()}
    out["stop_recommendation"] = not any(c3_any)
    out["stop_rule"] = "Recommend stopping the project if criterion 3 fails for both indices (NO_CANDIDATE counts as failing)."
    return out
