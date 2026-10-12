"""Development test and OOS evaluation on collected trades (PREREG §7, §8). Pure: trades in, verdicts out.

Seeds are `20261013 + 1000 * variant + k` handed to the bootstrap directly (`config.seed`); the entry study's own
seed helper is not used here.
"""
from __future__ import annotations

import numpy as np

from src.entry_study_v1 import stats as ST
from src.entry_study_v1.config import Cell
from src.entry_study_v1.pipeline import TradeRec
from src.reversal_study_v1 import config as RC

USD_NOTIONAL = 700.0

# report-only variant name -> (hold, cost, drop event days); the seed variant id comes from the config
REPORT_VARIANTS = {
    "hold_30m": ("30m", RC.BASE, False), "hold_60m": ("60m", RC.BASE, False),
    "stress_0.30": ("close", RC.STRESS, False), "sens_0.10": ("close", RC.SENS, False),
    "no_event_days": ("close", RC.BASE, True),
}


def series(recs: list[TradeRec], hold: str, cost: str, drop_events: bool = False) -> tuple[list[float], list[str]]:
    keep = [r for r in recs if not (drop_events and r.event_day)]
    return [r.res[hold]["r"][cost] for r in keep], [r.seg for r in keep]


def _mean(x) -> float | None:
    return float(np.mean(x)) if len(x) else None


def _times(recs: list[TradeRec]) -> dict | None:
    if not recs:
        return None
    t = [r.entry_t for r in recs]
    return {"min": int(min(t)), "q1": float(np.percentile(t, 25)), "median": float(np.median(t)),
            "q3": float(np.percentile(t, 75)), "max": int(max(t))}


def cell_report(cell: Cell, recs: list[TradeRec], b_count: int, variant_name: str = "main") -> dict:
    """Everything §7.4 asks for one cell except the Holm-adjusted p (needs all cells)."""
    x, seg = series(recs, "close", RC.BASE)
    main = ST.summarize(x, seg, RC.seed(RC.variant_id(variant_name), cell.k), b_count)
    out = {"k": cell.k, "pair": cell.pair, "rule": cell.rule, "side": cell.side, "etf": cell.etf, "main": main,
           "n_event_days": sum(r.event_day for r in recs), "entry_time": _times(recs)}
    for cost in (RC.BASE, RC.STRESS, RC.SENS):
        xc, _ = series(recs, "close", cost)
        out[f"mean_{cost}"] = _mean(xc)
        out[f"median_{cost}"] = float(np.median(xc)) if xc else None
        out[f"usd700_mean_{cost}"] = None if not xc else float(np.mean(xc)) * USD_NOTIONAL
    mae = [r.res["close"]["mae"] for r in recs]
    out["mae_close"] = {"median": float(np.median(mae)) if mae else None,
                        "p05": float(np.percentile(mae, 5)) if mae else None}
    out["variants"] = {}
    for name, (hold, cost, drop) in REPORT_VARIANTS.items():
        xv, sv = series(recs, hold, cost, drop)
        out["variants"][name] = ST.summarize(xv, sv, RC.seed(RC.variant_id(name), cell.k), b_count)
    return out


def run_dev(cells: list[Cell], trades: dict[int, list[TradeRec]], b_count: int = ST.B_DEFAULT) -> dict:
    """trades: cell k -> TradeRec list for all 16 cells (Holm K = 16 is fixed)."""
    if len(cells) != RC.K_TOTAL or set(trades) != {c.k for c in cells}:
        raise ValueError(f"expected {RC.K_TOTAL} cells with trades, got {len(cells)} cells / {sorted(trades)}")
    reports = {c.k: cell_report(c, trades[c.k], b_count) for c in cells}
    holm = ST.holm_adjust({k: r["main"]["p"] for k, r in reports.items()}, RC.K_TOTAL)
    passing = []
    for c in cells:
        r = reports[c.k]
        r["holm_p"] = holm[c.k]
        m = r["main"]
        xs, _ = series(trades[c.k], "close", RC.STRESS)
        r["dev_pass"] = ST.dev_pass(m["n"], holm[c.k], m["mean_by_segment"].get("A"),
                                    m["mean_by_segment"].get("B"), _mean(xs))
        if r["dev_pass"]["pass"]:
            passing.append({"k": c.k, "holm_p": holm[c.k], "mean": m["mean"], "n": m["n"]})
    selected = ST.rank_dev_pass(passing, max_cells=1)
    return {"verdict": "DEV_PASS" if passing else "NO_PASS", "k_total": RC.K_TOTAL,
            "cells": [reports[c.k] for c in cells], "dev_pass_cells": [p["k"] for p in passing],
            "selected_for_oos": selected}


def oos_cell(label: str, cell: Cell, recs: list[TradeRec], b_count: int = ST.B_DEFAULT) -> dict:
    """OOS result for one target: one continuous segment, variant 50 seed, p < 0.10 (§8). Other costs / holds are
    reported only."""
    x, seg = series(recs, "close", RC.BASE)
    s = ST.summarize(x, seg, RC.seed(RC.variant_id("oos_main"), cell.k), b_count)
    out = {"label": label, "k": cell.k, "strategy_id": cell.strategy_id, "summary": s,
           "verdict": ST.oos_verdict(s["n"], s["mean"], s["p"])}
    for name, (hold, cost, drop) in REPORT_VARIANTS.items():
        xv, _ = series(recs, hold, cost, drop)
        out[f"mean_{name}"] = _mean(xv)
    return out
