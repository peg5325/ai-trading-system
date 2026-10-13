"""Overnight drift pre-run check (spec docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.md §2): entry conditions only.

Counts, over the scored days, how many days would enter and the reason for every other day. No exit price, return or
P&L is read. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.explore_overnight_drift_v1.engine import SYMBOL, entry_status, first_scored_day
from src.history.exchange_calendar import trading_days


def preflight(qqq: dict, days: list[str], filters: bool = True) -> dict:
    k0 = first_scored_day(qqq, days)
    counts: dict[str, int] = {}
    if k0 is None:
        return {"scored_days": 0, "first": None, "counts": counts}
    for k in range(k0, len(days)):
        nxt = qqq[days[k + 1]] if k + 1 < len(days) else None
        s = entry_status(qqq[days[k]], nxt, filters)
        counts[s] = counts.get(s, 0) + 1
    return {"scored_days": len(days) - k0, "first": days[k0], "counts": dict(sorted(counts.items()))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    cal = trading_days(LOAD_START, DEV_LAST_DAY)
    qqq = prepare_symbol(SYMBOL, data[SYMBOL].regular, cal)
    for filters in (True, False):
        r = preflight(qqq, [d.isoformat() for d in cal], filters)
        print(f"\n### {'with filters' if filters else 'no-filter benchmark'}: scored days {r['scored_days']} "
              f"from {r['first']}\n\n| entry status | days |\n|---|---|")
        for k, v in r["counts"].items():
            print(f"| {k} | {v} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
