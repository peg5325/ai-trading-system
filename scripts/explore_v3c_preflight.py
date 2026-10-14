"""V3C pre-run check (spec docs/research/EXPLORE_V3C_ASYM_SWING_2000USD.md §2): indicators only, no trades, no P&L.

Over the continuous stream from the scored start: the V3B items (30m ATR14 / close percentiles, each config's vol_min
and max_ema_dist pass rates), the share of completed 30m bars in the bear regime (close < EMA200) and in the bear regime
with ATR14 / close >= 0.42%, and the share of completed 15m bars meeting the long / short rebound confirmation
(spec §4.1 conditions 1-3). Exploratory, not a decision basis.
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
from src.explore_v3_asym_swing.engine_v3c import CONFIGS_C, REGIME_EMA
from src.explore_v3_asym_swing.indicators import (
    EMA,
    Aggregator,
    WilderATR,
    build_stream,
)

PCTS = (25, 50, 60, 75, 90)
STRICT_SHORT_VOL = next(c.short_vol_min for c in CONFIGS_C if c.short_regime == "bear_only_vol")


def series(stream) -> dict[str, list]:
    """Per completed bar from the scored start: 30m (ATR14/close, bear) and 15m (|close - EMA21|/EMA21, long rebound,
    short rebound)."""
    a30, a15 = Aggregator(30), Aggregator(15)
    atr, e200 = WilderATR(VOL_ATR), EMA(REGIME_EMA)
    e9, e21 = EMA(9), EMA(21)
    out: dict[str, list] = {"vol": [], "bear": [], "bear_vol": [], "dist": [], "reb_long": [], "reb_short": []}
    st = {"prev": None, "sign": None}

    def on30(b):
        v = atr.update(b.h, b.l, b.c)
        e = e200.update(b.c)
        if v is None or b.day < SCORED_START:
            return
        out["vol"].append(v / b.c)
        if e is not None:
            out["bear"].append(b.c < e)
            out["bear_vol"].append(b.c < e and v / b.c >= STRICT_SHORT_VOL)

    def on15(b):
        prev, st["prev"] = st["prev"], b
        p9 = e9.value
        x9, x21 = e9.update(b.c), e21.update(b.c)
        if x9 is None or x21 is None:
            return
        sign = 1 if x9 > x21 else -1 if x9 < x21 else 0
        fresh = st["sign"] is not None and sign != 0 and sign != st["sign"]
        if sign != 0:
            st["sign"] = sign
        if b.day < SCORED_START:
            return
        out["dist"].append(abs(b.c - x21) / x21)
        for d, key in ((1, "reb_long"), (-1, "reb_short")):
            ok = (sign == d and p9 is not None and d * (x9 - p9) > 0 and (b.c > b.o if d > 0 else b.c < b.o)
                  and d * (b.c - x9) > 0
                  and (fresh or (prev is not None and (b.c > prev.h if d > 0 else b.c < prev.l))))
            out[key].append(ok)

    for m in stream:
        for agg, fn in ((a30, on30), (a15, on15)):
            b = agg.close_stale(m)
            if b is not None:
                fn(b)
        for agg, fn in ((a30, on30), (a15, on15)):
            b = agg.add(m)
            if b is not None:
                fn(b)
    return out


def percentiles(xs: list[float]) -> dict[int, float]:
    q = statistics.quantiles(xs, n=100, method="inclusive")
    return {p: q[p - 1] for p in PCTS}


def _share(xs) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    s = series(build_stream(data[SYMBOL].regular))
    vols, dists = s["vol"], s["dist"]
    pc = percentiles(vols)
    print(f"30m bars from {SCORED_START}: {len(vols)}; 15m bars: {len(dists)}\n")
    print("| 30m ATR14 / close | " + " | ".join(f"P{p}" for p in PCTS) + " |")
    print("|---|" + "---|" * len(PCTS))
    print("| value | " + " | ".join(f"{pc[p] * 100:.3f}%" for p in PCTS) + " |\n")
    print("| config | vol_min | vol pass | max_ema_dist | ema-dist pass |\n|---|---|---|---|---|")
    for c in CONFIGS_C:
        vp = sum(v >= c.vol_min for v in vols) / len(vols)
        dp = sum(x <= c.max_ema_dist for x in dists) / len(dists)
        print(f"| {c.name} | {c.vol_min * 100:.2f}% | {vp * 100:.1f}% | {c.max_ema_dist * 100:.2f}% | {dp * 100:.1f}% |")
    print(f"\n- 30m bars in the bear regime (close < EMA{REGIME_EMA}): {_share(s['bear']) * 100:.1f}%")
    print(f"- bear regime and ATR14/close >= {STRICT_SHORT_VOL * 100:.2f}%: {_share(s['bear_vol']) * 100:.1f}%")
    print(f"- 15m bars meeting the long rebound confirmation: {_share(s['reb_long']) * 100:.1f}%")
    print(f"- 15m bars meeting the short rebound confirmation: {_share(s['reb_short']) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
