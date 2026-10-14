"""V3 model A pre-run check (spec docs/research/EXPLORE_V3_ASYM_SWING_2000USD.md §2): indicators only, no trades, no P&L.

Per config over the continuous stream from the scored start: completed 30m bars with a SuperTrend direction, direction
changes (upper bound on gear-2 cycles), share of 30m closes passing ATR14/close >= 0.18%, short-timeframe EMA(9, 21)
crosses. Also the stream size and days with fewer bars than a full session. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_v3_asym_swing.engine import (
    CONFIGS,
    SCORED_START,
    SYMBOL,
    VOL_ATR,
    VOL_MIN,
)
from src.explore_v3_asym_swing.indicators import (
    EMA,
    Aggregator,
    SuperTrend,
    WilderATR,
    build_stream,
    last_minute_of_day,
)


def preflight(stream, cfg) -> dict:
    a30, ash = Aggregator(30), Aggregator(cfg.short_tf)
    st, atr, e9, e21 = SuperTrend(cfg.st_period, cfg.st_mult), WilderATR(VOL_ATR), EMA(9), EMA(21)
    c = {"bars30": 0, "flips": 0, "vol_ok": 0, "crosses": 0}
    state = {"dir": None, "sign": None}

    def on30(b):
        d = st.update(b.h, b.l, b.c)
        a = atr.update(b.h, b.l, b.c)
        if d is None or b.day < SCORED_START:
            state["dir"] = d
            return
        c["bars30"] += 1
        c["flips"] += state["dir"] is not None and d != state["dir"]
        c["vol_ok"] += a is not None and a / b.c >= VOL_MIN
        state["dir"] = d

    def onsh(b):
        x, y = e9.update(b.c), e21.update(b.c)
        if x is None or y is None:
            return
        s = 1 if x > y else -1 if x < y else 0
        if b.day >= SCORED_START and state["sign"] is not None and s != 0 and s != state["sign"]:
            c["crosses"] += 1
        if s != 0:
            state["sign"] = s

    for m in stream:
        for agg, fn in ((a30, on30), (ash, onsh)):
            b = agg.close_stale(m)
            if b is not None:
                fn(b)
        for agg, fn in ((a30, on30), (ash, onsh)):
            b = agg.add(m)
            if b is not None:
                fn(b)
    c["vol_ok_share"] = c["vol_ok"] / c["bars30"] if c["bars30"] else None
    return c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    args = ap.parse_args(argv)
    data = DataLoader(args.db, LOAD_START, DEV_LAST_DAY).load((SYMBOL,))
    stream = build_stream(data[SYMBOL].regular)
    days: dict[str, int] = {}
    for m in stream:
        days[m.day] = days.get(m.day, 0) + 1
    short_days = sorted(d for d, n in days.items() if n < last_minute_of_day(d) - 570 + 1 and d >= SCORED_START)
    print(f"stream bars {len(stream)}, days {len(days)}; scored-window days with missing minutes: {len(short_days)}"
          f" {short_days}\n")
    print("| config | 30m bars | SuperTrend flips | vol filter pass | short EMA crosses |\n|---|---|---|---|---|")
    for cfg in CONFIGS:
        r = preflight(stream, cfg)
        print(f"| {cfg.name} | {r['bars30']} | {r['flips']} | {r['vol_ok_share'] * 100:.1f}% | {r['crosses']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
