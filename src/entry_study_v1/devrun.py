"""Development test and OOS evaluation on collected trades (PREREG §7, §8). Pure: trades in, verdicts out."""
from __future__ import annotations

import numpy as np

from src.entry_study_v1 import stats as ST
from src.entry_study_v1.config import Cell
from src.entry_study_v1.pipeline import TradeRec

USD_NOTIONAL = 700.0

# variant name -> (hold, cost, drop event days)
REPORT_VARIANTS = {
    "hold_30m": ("30m", "base", False), "hold_60m": ("60m", "base", False),
    "stress": ("close", "stress", False), "fee_only_0.20": ("close", "fee_only_0.20", False),
    "fee_only_0.10": ("close", "fee_only_0.10", False), "no_event_days": ("close", "base", True),
}


def series(recs: list[TradeRec], hold: str, cost: str, drop_events: bool = False) -> tuple[list[float], list[str]]:
    keep = [r for r in recs if not (drop_events and r.event_day)]
    return [r.res[hold]["r"][cost] for r in keep], [r.seg for r in keep]


def _mean(x) -> float | None:
    return float(np.mean(x)) if len(x) else None


def cell_report(cell: Cell, recs: list[TradeRec], b_count: int) -> dict:
    """Everything §7.4 asks for one cell except the Holm-adjusted p (needs all cells)."""
    x, seg = series(recs, "close", "base")
    main = ST.summarize(x, seg, ST.seed_for(ST.VARIANTS["main"], cell.k), b_count)
    out = {"k": cell.k, "pair": cell.pair, "rule": cell.rule, "side": cell.side, "etf": cell.etf, "main": main,
           "n_event_days": sum(r.event_day for r in recs)}
    for cost in ("base", "stress", "fee_only_0.20", "fee_only_0.10"):
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
        out["variants"][name] = ST.summarize(xv, sv, ST.seed_for(ST.VARIANTS[name], cell.k), b_count)
    return out


def run_dev(cells: list[Cell], pair_results: dict[str, dict], b_count: int = ST.B_DEFAULT) -> dict:
    """pair_results: pair -> pipeline.pair_trades output, for the pairs that passed the data gate only."""
    live = [c for c in cells if c.pair in pair_results]
    k_total = 10 * len(pair_results)
    if len(live) != k_total:
        raise ValueError(f"expected {k_total} cells, got {len(live)}")
    reports = {c.k: cell_report(c, pair_results[c.pair]["main"][c.k], b_count) for c in live}
    holm = ST.holm_adjust({k: r["main"]["p"] for k, r in reports.items()}, k_total)
    passing = []
    for c in live:
        r = reports[c.k]
        r["holm_p"] = holm[c.k]
        m = r["main"]
        stress_x, _ = series(pair_results[c.pair]["main"][c.k], "close", "stress")
        r["dev_pass"] = ST.dev_pass(m["n"], holm[c.k], m["mean_by_segment"].get("A"),
                                    m["mean_by_segment"].get("B"), _mean(stress_x))
        if r["dev_pass"]["pass"]:
            passing.append({"k": c.k, "holm_p": holm[c.k], "mean": m["mean"], "n": m["n"]})
    selected = ST.rank_dev_pass(passing)
    e5_variants = {}
    for res in pair_results.values():
        for thr, per_cell in res["e5_alt"].items():
            name = "E5_1.0pct" if abs(thr - 0.010) < 1e-12 else "E5_2.0pct"
            for k, recs in per_cell.items():
                xv, sv = series(recs, "close", "base")
                e5_variants.setdefault(name, {})[k] = ST.summarize(xv, sv, ST.seed_for(ST.VARIANTS[name], k),
                                                                   b_count)
    unconditional = {}
    for res in pair_results.values():
        for k, recs in res["unconditional"].items():
            xv, sv = series(recs, "close", "base")
            unconditional[k] = ST.summarize(xv, sv, ST.seed_for(ST.VARIANTS["unconditional"], k), b_count)
    verdict = "NO_PASS" if not selected else "DEV_PASS"
    return {"verdict": verdict, "k_total": k_total, "cells": [reports[c.k] for c in live],
            "dev_pass_cells": [p["k"] for p in passing], "selected_for_oos": selected,
            "e5_sensitivity": e5_variants, "unconditional": unconditional}


def oos_cell(cell: Cell, recs: list[TradeRec], b_count: int = ST.B_DEFAULT) -> dict:
    """OOS result for one selected cell: one continuous segment, variant 50 seed, p < 0.10 (§8)."""
    x, seg = series(recs, "close", "base")
    s = ST.summarize(x, seg, ST.seed_for(ST.VARIANTS["oos_main"], cell.k), b_count)
    return {"k": cell.k, "summary": s, "verdict": ST.oos_verdict(s["n"], s["mean"], s["p"])}
