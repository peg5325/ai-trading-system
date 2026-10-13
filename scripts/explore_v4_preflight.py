"""V4 pre-run check (spec docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md §2): selection only, no P&L.

Loads the seven stocks through the V0 engine loader (research_load, refuses 2026-01-21 and later), prepares them with
the V4 split rule and prints, per symbol: days passing the screener, days picked (RVOL #1), picks failing the minimum
range; the stop-distance upper bound 1.5 x 5m ATR14 / price on pick days (ATR after the 09:55 bar, the last one
complete before 10:00) and the largest risk the USD 2,000 cash cap allows with it; and split breaks. No entry, exit
or P&L is computed. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.config import V4_RVOL_SNIPER as V4
from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.risk import ATR_MULT, RISK_FRACTION, START_EQUITY
from src.explore_orb_vwap_v0.strategy import first_scored_day
from src.explore_orb_vwap_v0.universe import SniperFilter, prepare_symbol
from src.history.exchange_calendar import trading_days

PRE_SIGNAL_FIVE = 6  # 5-minute bars complete by 10:00 (09:30 ~ 09:55)


def preflight(prepared: dict, days: list[str]) -> dict:
    k0 = first_scored_day(prepared, days, None, need_daily=True, need_rvol=True)
    scored = days[k0:] if k0 is not None else []
    f = SniperFilter(V4.rvol_min, V4.gap_up_min)
    per = {s: {"passing": 0, "picked": 0, "min_range_fail": 0} for s in prepared}
    stop_pct, cap_risk = [], []
    for d in scored:
        passing = f.passing({s: p[d] for s, p in prepared.items()})
        for c in passing:
            per[c.symbol]["passing"] += 1
        if not passing:
            continue
        sd = prepared[passing[0].symbol][d]
        per[sd.symbol]["picked"] += 1
        if sd.orb_high - sd.orb_low < V4.min_range_atr_mult * sd.daily_atr:
            per[sd.symbol]["min_range_fail"] += 1
            continue
        atr = sd.five[PRE_SIGNAL_FIVE - 1].atr
        price = sd.bars[PRE_SIGNAL_FIVE * 5 - 1].close
        pct = ATR_MULT * atr / price
        stop_pct.append(pct)
        cap_risk.append(min(RISK_FRACTION * START_EQUITY, START_EQUITY * pct))
    splits = {s: [d for d, sd in p.items() if sd.split_break] for s, p in prepared.items()}
    return {"scored_days": len(scored), "first": scored[0] if scored else None, "per_symbol": per,
            "stop_pct": stop_pct, "cap_risk": cap_risk, "splits": {s: v for s, v in splits.items() if v}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load(V4.universe)
    cal = trading_days(LOAD_START, DEV_LAST_DAY)
    prepared = {s: prepare_symbol(s, data[s].regular, cal, split_gap=V4.split_gap) for s in V4.universe}
    r = preflight(prepared, [d.isoformat() for d in cal])
    print(f"scored days {r['scored_days']} from {r['first']}; split breaks {r['splits'] or 'none'}\n")
    print("| symbol | days passing | picked | picked, min range fail |\n|---|---|---|---|")
    for s, c in r["per_symbol"].items():
        print(f"| {s} | {c['passing']} | {c['picked']} | {c['min_range_fail']} |")
    tot = {k: sum(c[k] for c in r["per_symbol"].values()) for k in ("picked", "min_range_fail")}
    print(f"| total pick days | | {tot['picked']} | {tot['min_range_fail']} |\n")
    sp, cr = r["stop_pct"], r["cap_risk"]
    if sp:
        q = statistics.quantiles(sp, n=4)
        print(f"pick days reaching 10:00 with range ok: {len(sp)}")
        print(f"1.5 x 5m ATR14 / price: p25 {q[0] * 100:.3f}% / median {statistics.median(sp) * 100:.3f}% / "
              f"p75 {q[2] * 100:.3f}%")
        print(f"max risk under the cash cap (USD, at most 20): median {statistics.median(cr):.2f}, "
              f"days reaching USD 20: {sum(x >= 19.99 for x in cr)} of {len(cr)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
