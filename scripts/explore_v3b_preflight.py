"""V3B pre-run check (spec docs/research/EXPLORE_V3B_ASYM_SWING_2000USD.md §2): indicators only, no trades, no P&L.

Over the continuous stream from the scored start: percentiles of the completed 30m bars' ATR14 / close, each config's
vol_min pass rate, and the share of completed 15m bars with |close - EMA21| / EMA21 <= max_ema_dist (EMA21 ready).
Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_v3_asym_swing.engine import SCORED_START, SYMBOL, VOL_ATR
from src.explore_v3_asym_swing.engine_v3b import CONFIGS_B
from src.explore_v3_asym_swing.indicators import (
    EMA,
    Aggregator,
    WilderATR,
    build_stream,
)

PCTS = (25, 50, 60, 75, 90)


def series(stream) -> tuple[list[float], list[float]]:
    """(30m ATR14/close values, 15m |close - EMA21| / EMA21 values) for completed bars from the scored start."""
    a30, a15 = Aggregator(30), Aggregator(15)
    atr, e21 = WilderATR(VOL_ATR), EMA(21)
    vols, dists = [], []

    def on30(b):
        v = atr.update(b.h, b.l, b.c)
        if v is not None and b.day >= SCORED_START:
            vols.append(v / b.c)

    def on15(b):
        e = e21.update(b.c)
        if e is not None and b.day >= SCORED_START:
            dists.append(abs(b.c - e) / e)

    for m in stream:
        for agg, fn in ((a30, on30), (a15, on15)):
            b = agg.close_stale(m)
            if b is not None:
                fn(b)
        for agg, fn in ((a30, on30), (a15, on15)):
            b = agg.add(m)
            if b is not None:
                fn(b)
    return vols, dists


def percentiles(xs: list[float]) -> dict[int, float]:
    q = statistics.quantiles(xs, n=100, method="inclusive")
    return {p: q[p - 1] for p in PCTS}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    vols, dists = series(build_stream(data[SYMBOL].regular))
    pc = percentiles(vols)
    print(f"30m bars from {SCORED_START}: {len(vols)}; 15m bars: {len(dists)}\n")
    print("| 30m ATR14 / close | " + " | ".join(f"P{p}" for p in PCTS) + " |")
    print("|---|" + "---|" * len(PCTS))
    print("| value | " + " | ".join(f"{pc[p] * 100:.3f}%" for p in PCTS) + " |\n")
    print("| config | vol_min | vol pass | max_ema_dist | ema-dist pass |\n|---|---|---|---|---|")
    for c in CONFIGS_B:
        vp = sum(v >= c.vol_min for v in vols) / len(vols)
        dp = sum(x <= c.max_ema_dist for x in dists) / len(dists)
        print(f"| {c.name} | {c.vol_min * 100:.2f}% | {vp * 100:.1f}% | {c.max_ema_dist * 100:.2f}% | {dp * 100:.1f}% |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
