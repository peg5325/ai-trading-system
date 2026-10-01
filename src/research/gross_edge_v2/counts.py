"""Signal-condition counts for parity with the disclosed count-only pass (§7.1).

Counts conditions only: no fills, no exits, no returns. Uses the implementation's own
validity and indicator code. Definitions follow the count-only pass: the condition day t
must be an evaluated day; for B/C/D the previous NYSE trading day must be a valid daily
bar; day t's own validity is NOT required (the disclosed pass did not require it), only
the bars the condition reads.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from src.research.gross_edge_v2.arms import (
    A2_SYMBOLS,
    A_SIGNAL_MINUTE,
    D1_SIGNAL_FIRST,
    D1_SIGNAL_LAST,
    D1_SYMBOLS,
    UNIVERSE,
    late_day_return_pct,
)
from src.research.gross_edge_v2.daily import compute_indicators, valid_daily_bars
from src.research.gross_edge_v2.data import OPEN_MINUTE, DayBars
from src.research.orb.windows import previous_trading_day

DEFINITIONS = (
    "condition counts only (no fills, no returns); t in evaluated days; B/C/D need a valid "
    "t-1 daily bar and defined indicators; A needs 09:30 and 14:44 bars of the signal "
    "symbol; D needs ATR14(t-1) and a 10:00-15:00 bar with close <= VWAP - k*ATR14; day t "
    "validity not required"
)


def _summary(hits: set[tuple[str, date]]) -> dict[str, int]:
    return {"symbol_days": len(hits), "days": len({day for _, day in hits})}


def condition_counts(
    bars_by_symbol: Mapping[str, Mapping[date, DayBars]], evaluated_days: Sequence[date]
) -> dict[str, Any]:
    indicators = {
        symbol: compute_indicators(valid_daily_bars(bars_by_symbol.get(symbol, {})))
        for symbol in UNIVERSE
    }
    hits: dict[str, set[tuple[str, date]]] = {
        name: set() for name in (
            "QQQ_abs_R_ge_1.5", "QQQ_abs_R_ge_2.0", "SOXL_abs_R_ge_4.5", "SOXL_abs_R_ge_6.0",
            "A1_pooled", "A2", "B1_nr7", "B2_inside_nr4", "B3_close_gt_sma20",
            "C1_rsi2_lt5_close_gt_sma200", "C2_rsi3_roc1_lt30", "D_k0.6", "D_k1.0",
        )
    }

    def a_move(symbol: str, day: date) -> float | None:
        bars = bars_by_symbol.get(symbol, {}).get(day)
        if bars is None or not (bars.has(OPEN_MINUTE) and bars.has(A_SIGNAL_MINUTE)):
            return None
        return late_day_return_pct(bars)

    for day in sorted(set(evaluated_days)):
        for symbol, levels in (("QQQ", (1.5, 2.0)), ("SOXL", (4.5, 6.0))):
            move = a_move(symbol, day)
            if move is None:
                continue
            for level in levels:
                if abs(move) >= level:
                    hits[f"{symbol}_abs_R_ge_{level}"].add((symbol, day))
            if abs(move) >= levels[0]:
                hits["A1_pooled"].add((symbol, day))
        for symbol in A2_SYMBOLS:
            move = a_move(symbol, day)
            if move is None or move < 3.0:
                continue
            bars = bars_by_symbol[symbol][day]
            vwap = bars.vwap_through(A_SIGNAL_MINUTE)
            if vwap is not None and bars.bar(A_SIGNAL_MINUTE)[3] > vwap:  # type: ignore[index]
                hits["A2"].add((symbol, day))

        prev_day = previous_trading_day(day)
        for symbol in UNIVERSE:
            prev = indicators[symbol].get(prev_day)
            if prev is None:
                continue
            key = (symbol, day)
            if prev.nr7:
                hits["B1_nr7"].add(key)
            if prev.inside and prev.nr4:
                hits["B2_inside_nr4"].add(key)
            if prev.sma20 is not None and prev.close > prev.sma20:
                hits["B3_close_gt_sma20"].add(key)
            if (prev.rsi2 is not None and prev.sma200 is not None
                    and prev.rsi2 < 5.0 and prev.close > prev.sma200):
                hits["C1_rsi2_lt5_close_gt_sma200"].add(key)
            if prev.rsi3_roc1 is not None and prev.rsi3_roc1 < 30.0:
                hits["C2_rsi3_roc1_lt30"].add(key)
            bars = bars_by_symbol.get(symbol, {}).get(day)
            if symbol not in D1_SYMBOLS or prev.atr14 is None or bars is None:
                continue
            for k, name in ((0.6, "D_k0.6"), (1.0, "D_k1.0")):
                for minute in range(D1_SIGNAL_FIRST, D1_SIGNAL_LAST + 1):
                    bar, vwap = bars.bar(minute), bars.vwap_through(minute)
                    if bar is not None and vwap is not None and bar[3] <= vwap - k * prev.atr14:
                        hits[name].add(key)
                        break
    return {
        "definitions": DEFINITIONS,
        "evaluated_days": len(set(evaluated_days)),
        "counts": {name: _summary(found) for name, found in hits.items()},
    }
