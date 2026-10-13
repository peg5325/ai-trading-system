"""False-breakout fade pre-run check (spec docs/research/EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.md §2): setups only.

Counts day outcomes of the setup search (min_range / runaway / no_breakout / no_trap / late_trap / signal) and, on
signal days, the stop distance (swing high + 0.02 - entry fill) / entry price, the largest risk the USD 2,000 cash
cap allows with it, and days whose VWAP at the trap bar is at or above the entry open. No exit or P&L is computed.
Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_false_breakout_fade_v1.engine import (
    SYMBOL,
    entry_plan,
    find_setup,
    first_scored_day,
)
from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.risk import RISK_FRACTION, START_EQUITY
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.history.exchange_calendar import trading_days


def preflight(qqq: dict, days: list[str]) -> dict:
    k0 = first_scored_day(qqq, days)
    scored = days[k0:] if k0 is not None else []
    counts: dict[str, int] = {}
    stop_pct, cap_risk = [], []
    vwap_above = 0
    for d in scored:
        sd = qqq[d]
        if not sd.complete:
            key = "excluded_incomplete"
        elif not sd.daily_ready:
            key = "daily_not_ready"
        else:
            key, s = find_setup(sd)
            if s is not None:
                plan, why = entry_plan(sd, s, START_EQUITY)
                if plan is None:
                    key = why
                else:
                    pct = plan["r"] / plan["fill"]
                    stop_pct.append(pct)
                    cap_risk.append(min(RISK_FRACTION * START_EQUITY, START_EQUITY * pct))
                    v = sd.vwap[s.index]
                    vwap_above += v is not None and v >= plan["raw"]
        counts[key] = counts.get(key, 0) + 1
    return {"scored_days": len(scored), "first": scored[0] if scored else None, "counts": dict(sorted(counts.items())),
            "stop_pct": stop_pct, "cap_risk": cap_risk, "vwap_above_entry": vwap_above}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    cal = trading_days(LOAD_START, DEV_LAST_DAY)
    r = preflight(prepare_symbol(SYMBOL, data[SYMBOL].regular, cal), [d.isoformat() for d in cal])
    print(f"scored days {r['scored_days']} from {r['first']} (equity fixed at USD 2,000 for the risk cap)\n")
    print("| day outcome | days |\n|---|---|")
    for k, v in r["counts"].items():
        print(f"| {k} | {v} |")
    sp, cr = r["stop_pct"], r["cap_risk"]
    if len(sp) > 1:
        q = statistics.quantiles(sp, n=4)
        print(f"\nsignal days with an entry: {len(sp)}")
        print(f"stop distance / entry: p25 {q[0] * 100:.3f}% / median {statistics.median(sp) * 100:.3f}% / "
              f"p75 {q[2] * 100:.3f}%")
        print(f"max risk under the cash cap (USD, at most 20): median {statistics.median(cr):.2f}, "
              f"days reaching USD 20: {sum(x >= 19.99 for x in cr)} of {len(cr)}")
        print(f"VWAP at the trap bar >= entry open (TP1 on the fill bar): {r['vwap_above_entry']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
