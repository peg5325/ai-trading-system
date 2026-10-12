"""Glue for the reversal study: verified loads (six symbols), mask rebuild, signal trades, exit overlay (PREREG §5, §7).

Reuses `entry_study_v1.pipeline` / `data` / `mask` / `cli` (not modified). Costs are the reversal cost sets
(`reversal_study_v1.config.COSTS`).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date as Date

import numpy as np

from src.entry_study_v1 import devrun as ENTRY_DEVRUN
from src.entry_study_v1 import mask as M
from src.entry_study_v1 import stats as ST
from src.entry_study_v1 import trades as TR
from src.entry_study_v1.bars import InvalidRun
from src.entry_study_v1.cli import check_mask_rebuild
from src.entry_study_v1.config import Cell
from src.entry_study_v1.data import DEV_SEGMENTS, Segment, SymbolData
from src.entry_study_v1.pipeline import (
    TradeRec,
    by_t,
    load_dev_segments,
    make_ref_day,
    verify_against_manifest,
)
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1.overlay import FORCED_EXIT_T, overlay_exit

__all__ = [
    "DEV_SEGMENTS",
    "TradeRec",
    "check_rebuilt_mask",
    "collect_trades",
    "overlay_cell",
    "reproduce_entry_baseline",
    "verified_dev_data",
]


def verified_dev_data(db, symbols, manifest: dict, *, purpose: str,
                      segments: tuple[Segment, ...] = DEV_SEGMENTS) -> dict[str, dict[str, SymbolData]]:
    """Development rows of `symbols` (upper bound 2026-01-21 00:00 ET enforced by the loader), checked against the
    fixed data manifest before anything else happens."""
    seg_data = load_dev_segments(db, symbols, purpose=purpose, segments=segments)
    verify_against_manifest(seg_data, manifest)
    return seg_data


def check_rebuilt_mask(mask: dict, pairs: dict, seg_data: dict, cal: dict, events_sha256: str,
                       segments: tuple[Segment, ...] = DEV_SEGMENTS) -> None:
    """Rebuild the P1/P2 mask from the verified rows and compare it with the fixed mask."""
    check_mask_rebuild(mask, M.build_mask(pairs, seg_data, segments, cal, events_sha256))


def collect_trades(cells: list[Cell], evaluate: Callable, mask_pair: dict, seg_data: dict[str, dict[str, SymbolData]],
                   segments: tuple[Segment, ...], costs=RC.COSTS) -> dict[int, list[TradeRec]]:
    """Trades of one pair's cells over its scored days. `evaluate(rule, RefDay, params) -> Result`.
    A signal trades the cell with the same (rule, side); at most one signal per rule and day."""
    by_key = {(c.rule, c.side): c for c in cells}
    params_of = {c.rule: c.params for c in cells}
    rules = sorted(params_of)
    out: dict[int, list[TradeRec]] = {c.k: [] for c in cells}
    ref = next(iter(cells)).ref
    for seg in segments:
        m = mask_pair["segments"][seg.name]
        data = seg_data[seg.name]
        for iso in m["scored"]:
            rd = make_ref_day(Date.fromisoformat(iso), seg, data[ref])
            event = iso in m["event_days"]
            for rule in rules:
                res = evaluate(rule, rd, params_of[rule])
                if res.signal is None:
                    continue
                c = by_key.get((rule, res.signal.side))
                if c is None:
                    continue  # e.g. the designated OOS cell trades only its own side
                etf_bars = by_t(data[c.etf].regular.get(iso, []))
                out[c.k].append(TradeRec(c.k, iso, seg.name, event, res.signal.entry_t,
                                         TR.trade(c, res.signal.entry_t, etf_bars, iso, costs)))
    return out


# ------------------------------------------------------------------ §7.5 overlay


def overlay_cell(cell: Cell, recs: list[TradeRec], seg_data: dict[str, dict[str, SymbolData]],
                 segments: tuple[Segment, ...], t3_params: dict, t4_params: dict, costs=RC.COSTS) -> dict:
    """Overlay exit vs hold-to-close for one entry-study cell's trades (report only: no p, no Holm)."""
    seg_by_name = {s.name: s for s in segments}
    names = (RC.BASE, RC.STRESS)
    rows = []
    for r in recs:
        seg = seg_by_name[r.seg]
        data = seg_data[r.seg]
        rd = make_ref_day(Date.fromisoformat(r.day), seg, data[cell.ref])
        ov = overlay_exit(rd, cell.side, r.entry_t, t3_params, t4_params)
        bars = by_t(data[cell.etf].regular.get(r.day, []))
        for t in (r.entry_t, ov.exit_t, FORCED_EXIT_T):
            if t not in bars:
                raise InvalidRun(f"{r.day}: {cell.etf} has no bar {t}")
        row = {"reason": ov.reason, "hold_min": ov.exit_t - r.entry_t}
        for n in names:
            row[f"overlay_{n}"] = TR.net_return(bars[r.entry_t].open, bars[ov.exit_t].open, costs[n])
            row[f"close_{n}"] = TR.net_return(bars[r.entry_t].open, bars[FORCED_EXIT_T].open, costs[n])
        rows.append(row)
    out: dict = {"k": cell.k, "n": len(rows)}
    if not rows:
        return out
    for n in names:
        ov = np.array([x[f"overlay_{n}"] for x in rows])
        cl = np.array([x[f"close_{n}"] for x in rows])
        out[f"mean_overlay_{n}"] = float(np.mean(ov))
        out[f"mean_close_{n}"] = float(np.mean(cl))
        out[f"mean_diff_{n}"] = float(np.mean(ov - cl))
    for reason in ("T3", "T4", "both", "close"):
        out[f"share_{reason}"] = sum(x["reason"] == reason for x in rows) / len(rows)
    out["median_hold_min"] = float(np.median([x["hold_min"] for x in rows]))
    return out


def overlay_report(entry_cells: list[Cell], entry_pair_trades: dict[str, dict], entry_dev_result: dict,
                   seg_data: dict[str, dict[str, SymbolData]], segments: tuple[Segment, ...],
                   t3_params: dict, t4_params: dict) -> dict:
    """§7.5: report the overlay only if the entry baseline reproduces the entry dev_result.json exactly."""
    bad = reproduce_entry_baseline(entry_cells, entry_pair_trades, entry_dev_result)
    if bad:
        return {"reported": False, "why": "entry baseline does not reproduce dev_result.json", "mismatches": bad}
    cells = [{**overlay_cell(c, entry_pair_trades[c.pair]["main"][c.k], seg_data, segments, t3_params, t4_params),
              "pair": c.pair, "rule": c.rule, "side": c.side, "etf": c.etf}
             for c in entry_cells if c.pair in entry_pair_trades]
    return {"reported": True, "cells": cells}


def reproduce_entry_baseline(entry_cells: list[Cell], entry_pair_trades: dict[str, dict], dev_result: dict,
                             ks: list[int] | None = None) -> list[str]:
    """§7.5 gate: recompute the entry study's fields from the entry trades and compare with its dev_result.json
    by EXACT equality. Default cells = every entry cell of the given pairs (P1, P2 -> k = 0..19). Returns the
    mismatches (empty list = the overlay may be reported)."""
    by_k = {r["k"]: r for r in dev_result["cells"]}
    if ks is None:
        ks = [c.k for c in entry_cells if c.pair in entry_pair_trades]
    cell_pair = {c.k: c.pair for c in entry_cells}
    bad: list[str] = []
    for k in ks:
        if k not in by_k:
            bad.append(f"k={k}: missing in dev_result")
            continue
        recs = entry_pair_trades[cell_pair[k]]["main"][k]
        x, seg = ENTRY_DEVRUN.series(recs, "close", "base")
        main = ST.summarize(x, seg)
        xs, _ = ENTRY_DEVRUN.series(recs, "close", "stress")
        got = {"main.n": main["n"], "main.n_by_segment": main["n_by_segment"],
               "main.mean_by_segment": main["mean_by_segment"], "mean_base": ENTRY_DEVRUN._mean(x),
               "mean_stress": ENTRY_DEVRUN._mean(xs), "median_base": float(np.median(x)) if x else None}
        want_row = by_k[k]
        want = {"main.n": want_row["main"]["n"], "main.n_by_segment": want_row["main"]["n_by_segment"],
                "main.mean_by_segment": want_row["main"]["mean_by_segment"], "mean_base": want_row["mean_base"],
                "mean_stress": want_row["mean_stress"], "median_base": want_row["median_base"]}
        for name, v in got.items():
            if v != want[name]:
                bad.append(f"k={k}: {name} {v!r} != {want[name]!r}")
    return bad
