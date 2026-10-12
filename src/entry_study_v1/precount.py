"""Pre-run count and power check (PREREG §6). Reads reference-symbol signals only; no ETF price, no P&L."""
from __future__ import annotations

from datetime import date as Date

from src.entry_study_v1 import rules as RL
from src.entry_study_v1.config import Cell
from src.entry_study_v1.data import Segment, SymbolData
from src.entry_study_v1.pipeline import make_ref_day
from src.entry_study_v1.stats import power_delta

# Population sd (fraction) of ETF returns from 09:35 open to the hold end, development segments (PREREG §6.2
# table, larger of the pair's ETFs and the A/B segments - the doc's worked examples use this reading).
# P3: SPXL/SPXS were not yet measured; the doc's stand-in is SPY x 3 (30m 0.54/0.90%, close 1.65/2.85%).
# P3 60m stand-in (J): SPY 60m sd 0.39% (B) x 3 = 1.17%.
SD = {"P1": {"30m": 0.0228, "60m": 0.0275, "close": 0.0575}, "P2": {"30m": 0.0129, "60m": 0.0163, "close": 0.0356},
      "P3": {"30m": 0.0090, "60m": 0.0117, "close": 0.0285}}


def count_pair(pair: str, cells: list[Cell], mask_pair: dict, seg_data: dict[str, dict[str, SymbolData]],
               segments: tuple[Segment, ...]) -> dict:
    """Signals by rule x side x segment, event-day signals, E2 PRE substitutions, E3 undefined minutes, E5 1%/2%."""
    mine = {(c.rule, c.side): c for c in cells if c.pair == pair}
    ref = mask_pair["ref"]
    e5_thresholds = {"main": None, **{f"{t:.3f}": t for t in mine[("E5", "long")].params["sensitivity_thresholds"]}}
    counts: dict = {}

    def bump(key: str, seg: str, event: bool) -> None:
        d = counts.setdefault(key, {"A": 0, "B": 0, "event_days": 0})
        d[seg] = d.get(seg, 0) + 1
        d["event_days"] += int(event)

    extras = {"e2_pre_substituted_days": 0, "e2_scored_days": 0, "e3_sigma_undefined_minutes": 0}
    for seg in segments:
        m = mask_pair["segments"][seg.name]
        for iso in m["scored"]:
            rd = make_ref_day(Date.fromisoformat(iso), seg, seg_data[seg.name][ref])
            event = iso in m["event_days"]
            for rule in RL.RULE_FUNCS:
                p = mine[(rule, "long")].params
                res = RL.evaluate(rule, rd, p)
                if rule == "E2":
                    extras["e2_scored_days"] += 1
                    extras["e2_pre_substituted_days"] += int(res.info["pre_substituted"])
                if rule == "E3":
                    extras["e3_sigma_undefined_minutes"] += len(RL.sigma_undefined_minutes(rd, p))
                if res.signal is not None:
                    bump(f"{rule}:{res.signal.side}", seg.name, event)
                if rule == "E5":
                    for name, thr in e5_thresholds.items():
                        if thr is None:
                            continue
                        r5 = RL.e5(rd, p, threshold=thr)
                        if r5.signal is not None:
                            bump(f"E5@{name}:{r5.signal.side}", seg.name, event)
    return {"signals": counts, **extras}


def power_table(pair: str, counts: dict, k_total: int) -> list[dict]:
    """delta = (z_{1-alpha} + z_0.8) * sd / sqrt(n) for each of the pair's 10 cells (n = 0 rows included),
    alpha = 0.05 and 0.05 / K (Holm first step), for the close, 60m and 30m holds."""
    rows = []
    for rule in ("E1", "E2", "E3", "E4", "E5"):
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
