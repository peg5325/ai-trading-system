"""Pre-run count and power check (PREREG §6). Reference-symbol signals only; no ETF price is used, no P&L."""
from __future__ import annotations

from datetime import date as Date

import numpy as np

from src.entry_study_v1 import rules as ERL
from src.entry_study_v1.config import Cell
from src.entry_study_v1.data import Segment, SymbolData
from src.entry_study_v1.pipeline import make_ref_day
from src.entry_study_v1.precount import SD
from src.entry_study_v1.stats import power_delta
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import rules as RL


def _dist(minutes: list[int]) -> dict | None:
    if not minutes:
        return None
    return {"min": int(min(minutes)), "q1": float(np.percentile(minutes, 25)), "median": float(np.median(minutes)),
            "q3": float(np.percentile(minutes, 75)), "max": int(max(minutes))}


def count_pair(pair: str, cells: list[Cell], mask_pair: dict, seg_data: dict[str, dict[str, SymbolData]],
               segments: tuple[Segment, ...]) -> dict:
    """Signals by rule x side x segment (with event days and entry-time distribution) and the §6.2 diagnostics."""
    mine = {c.rule: c.params for c in cells if c.pair == pair}
    ref = mask_pair["ref"]
    counts: dict = {}
    times: dict[str, list[int]] = {}
    diag = {
        "T1": {"scored_days": 0, "gap_days": 0, "pre_substituted_days": 0, "filled_before_0935_days": 0,
               "open_already_filled_days": 0, "signal_days": 0},
        "T2": {"scored_days": 0, "breakout_up": 0, "breakout_down": 0, "failed_up": 0, "failed_down": 0},
        "T3": {"scored_days": 0, "down_mornings": 0, "up_mornings": 0, "recovered_down": 0, "recovered_up": 0,
               "vwap_undefined_days": 0},
        "T4": {"scored_days": 0, "first_breakouts": 0, "opposite_breakouts": 0, "sigma_undefined_minutes": 0},
    }

    def bump(key: str, seg: str, event: bool, entry_t: int) -> None:
        d = counts.setdefault(key, {"A": 0, "B": 0, "event_days": 0})
        d[seg] = d.get(seg, 0) + 1
        d["event_days"] += int(event)
        times.setdefault(key, []).append(entry_t)

    for seg in segments:
        m = mask_pair["segments"][seg.name]
        for iso in m["scored"]:
            rd = make_ref_day(Date.fromisoformat(iso), seg, seg_data[seg.name][ref])
            event = iso in m["event_days"]
            for rule in RC.RULES:
                p = mine[rule]
                res = RL.evaluate(rule, rd, p)
                i = res.info
                d = diag[rule]
                d["scored_days"] += 1
                if rule == "T1":
                    if "open_already_filled" in i:  # |g| >= threshold
                        d["gap_days"] += 1
                        d["pre_substituted_days"] += int(i["pre_substituted"])
                        d["filled_before_0935_days"] += int(i["filled_before_window"])
                        d["open_already_filled_days"] += int(i["open_already_filled"])
                        d["signal_days"] += int(res.signal is not None)
                elif rule == "T2":
                    if "breakout_dir" in i:
                        d[f"breakout_{i['breakout_dir']}"] += 1
                        d[f"failed_{i['breakout_dir']}"] += int(res.signal is not None)
                elif rule == "T3":
                    d["vwap_undefined_days"] += int(bool(i.get("vwap_undefined")))
                    if i.get("morning"):
                        d[f"{i['morning']}_mornings"] += 1
                        d[f"recovered_{i['morning']}"] += int(res.signal is not None)
                else:
                    d["first_breakouts"] += int("first_t" in i)
                    d["opposite_breakouts"] += int(res.signal is not None)
                    d["sigma_undefined_minutes"] += len(ERL.sigma_undefined_minutes(rd, p))
                if res.signal is not None:
                    bump(f"{rule}:{res.signal.side}", seg.name, event, res.signal.entry_t)
    return {"signals": counts, "entry_time": {k: _dist(v) for k, v in sorted(times.items())}, "diagnostics": diag}


def power_table(pair: str, counts: dict, k_total: int = RC.K_TOTAL) -> list[dict]:
    """delta = (z_{1-alpha} + z_0.8) * sd / sqrt(n) per cell for the close, 60m and 30m holds, alpha = 0.05 and
    0.05 / K (Holm first step). n = 0 rows included."""
    rows = []
    for rule in RC.RULES:
        for side in ("long", "short"):
            c = counts["signals"].get(f"{rule}:{side}", {"A": 0, "B": 0})
            n = c["A"] + c["B"]
            row = {"cell": f"{pair}:{rule}:{side}", "n": n}
            for hold in ("close", "60m", "30m"):
                sd = SD[pair][hold]
                row[hold] = {"sd": sd,
                             "delta_alpha_0.05": power_delta(sd, n, 0.05) if n else None,
                             "delta_alpha_0.05_over_K": power_delta(sd, n, 0.05 / k_total) if n else None}
            rows.append(row)
    return rows
