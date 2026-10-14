"""Overnight drift V2 pre-run check (spec docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.md §2): entries only.

For each config, counts over the V1 scored days how many days would enter and the reason for every other day,
including EMA50 not-ready days. No exit price, return or P&L is read. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.explore_overnight_drift_v1.ablation import CONFIGS, daily_ema_series, entry_for
from src.explore_overnight_drift_v1.engine import SYMBOL, first_scored_day
from src.history.exchange_calendar import trading_days


def preflight(qqq: dict, days: list[str]) -> dict:
    ema20 = daily_ema_series(qqq, days, 20)
    ema50 = daily_ema_series(qqq, days, 50)
    k0 = first_scored_day(qqq, days)
    out = {}
    for cfg in CONFIGS:
        entry = entry_for(cfg, ema20, ema50)
        counts: dict[str, int] = {}
        for k in range(k0, len(days)):
            nxt = qqq[days[k + 1]] if k + 1 < len(days) else None
            s = entry(qqq[days[k]], nxt)
            counts[s] = counts.get(s, 0) + 1
        out[cfg.name] = dict(sorted(counts.items()))
    return {"scored_days": len(days) - k0, "first": days[k0], "counts": out}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    cal = trading_days(LOAD_START, DEV_LAST_DAY)
    r = preflight(prepare_symbol(SYMBOL, data[SYMBOL].regular, cal), [d.isoformat() for d in cal])
    print(f"scored days {r['scored_days']} from {r['first']}\n")
    statuses = sorted({s for c in r["counts"].values() for s in c})
    print("| status | " + " | ".join(r["counts"]) + " |")
    print("|---|" + "---|" * len(r["counts"]))
    for s in statuses:
        print(f"| {s} | " + " | ".join(str(c.get(s, 0)) for c in r["counts"].values()) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
