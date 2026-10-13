"""False-breakout (bull trap) short fade on QQQ (spec EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1 §3, §4).

Reuses the ORB exploration engine read-only: the loader, `prepare_symbol` (daily ATR14 / EMA20 through the prior
day, VWAP), `Trade` / `Leg` / `DayRecord` / `BacktestResult` and the reporter, so the result JSON has the same shape
and the existing compare / trade-stats scripts read it.

Timing: the trap is the completed 1m bar t; the entry fills at the open of bar t+1. From the fill bar on, each bar is
checked in the order flatten (at its open) -> stop -> TP1 (VWAP known at the bar's start) -> TP2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from src.explore_orb_vwap_v0.risk import (
    COMMISSION,
    REF_COST_RATE,
    SLIPPAGE,
    START_EQUITY,
    RiskManager,
)
from src.explore_orb_vwap_v0.strategy import (
    BacktestResult,
    DayRecord,
    Leg,
    Trade,
    qqq_buy_hold,
)
from src.explore_orb_vwap_v0.universe import ORB_BARS, SymbolDay

SYMBOL = "QQQ"
MIN_RANGE_ATR = 0.15
RUNAWAY_ATR = 0.6
STOP_PAD = 0.02  # stop = swing high + 0.02
TP2_R = 2.0
SIGNAL_LAST = 839  # trap bar start 13:59 (closes 14:00); fill at 14:00 at the latest
FLATTEN_BEFORE_CLOSE = 10
SIDE = -1


@dataclass(frozen=True)
class Setup:
    """The day's first trap (spec §4.2)."""

    index: int  # trap bar index
    swing_high: float  # highest high from the breakout bar through the trap bar


def find_setup(sd: SymbolDay) -> tuple[str, Setup | None]:
    """('signal', Setup) or (reason, None): min_range | runaway | no_breakout | no_trap | late_trap.

    Uses only the day's bars up to the trap bar and the prior-day daily ATR14."""
    atr = sd.daily_atr
    if sd.orb_high - sd.orb_low < MIN_RANGE_ATR * atr:
        return "min_range", None
    swing: float | None = None
    for i in range(ORB_BARS, len(sd.bars)):
        b = sd.bars[i]
        if swing is None:
            if b.high <= sd.orb_high:
                continue
            swing = b.high
        else:
            swing = max(swing, b.high)
        if swing > sd.orb_high + RUNAWAY_ATR * atr:
            return "runaway", None
        if b.close < sd.orb_high:
            # spec §4.2 window; on early-close days also no fill at or after the flatten minute (spec §4.2 (J))
            if b.t > SIGNAL_LAST or b.t + 1 >= sd.close_minute - FLATTEN_BEFORE_CLOSE:
                return "late_trap", None
            return "signal", Setup(i, swing)
    return ("no_breakout" if swing is None else "no_trap"), None


def entry_plan(sd: SymbolDay, s: Setup, equity: float) -> tuple[dict | None, str]:
    """Entry fill, stop, R, shares and TP2 level (spec §4.3, §4.4.4), or (None, reason)."""
    raw = sd.bars[s.index + 1].open
    fill = raw - SLIPPAGE
    stop = s.swing_high + STOP_PAD
    r = stop - fill
    if r <= 0:
        return None, "stop_beyond_fill"
    shares = RiskManager.fractional_shares(equity, equity, fill, r)
    if shares <= 0:
        return None, "zero_shares"
    return {"raw": raw, "fill": fill, "stop": stop, "r": r, "shares": shares,
            "tp2": max(sd.orb_low, fill - TP2_R * r)}, ""


def simulate_trade(sd: SymbolDay, s: Setup, plan: dict) -> Trade:
    i = s.index
    bars = sd.bars
    tp2 = plan["tp2"]
    tp2_reason = "tp2_orb_low" if tp2 == sd.orb_low else "tp2_2r"
    tr = Trade(sd.day, sd.symbol, SIDE, bars[i].t, bars[i + 1].t, plan["raw"], plan["fill"], plan["shares"],
               plan["stop"], plan["r"], tp2)
    remaining = plan["shares"]
    stop = plan["stop"]
    flat = sd.close_minute - FLATTEN_BEFORE_CLOSE

    def leg(t: int, q: float, raw: float, reason: str) -> None:
        tr.legs.append(Leg(t, q, raw, raw + SLIPPAGE, reason))  # short exit = buy back, slippage against us

    for j in range(i + 1, len(bars)):
        b = bars[j]
        if b.t == flat:
            leg(b.t, remaining, b.open, "flatten")
            tr.exit_reason = "flatten"
            break
        if b.high >= stop:
            leg(b.t, remaining, max(stop, b.open), "breakeven" if tr.tp1 else "stop")
            tr.exit_reason = "breakeven" if tr.tp1 else "stop"
            break
        if not tr.tp1:
            v = sd.vwap[j - 1]
            if v is not None and b.low <= v:
                q = round(tr.shares / 2, 4) or tr.shares
                leg(b.t, q, min(v, b.open), "tp1_vwap")
                tr.tp1 = True
                remaining = round(remaining - q, 4)
                stop = tr.entry_fill  # breakeven from the next bar (this bar's stop was checked above)
                if remaining == 0:
                    tr.exit_reason = "tp1_vwap"
                    break
        if b.low <= tp2:
            leg(b.t, remaining, min(tp2, b.open), tp2_reason)
            tr.exit_reason = tp2_reason
            break
    else:
        raise AssertionError(f"{sd.day}: position still open after the last bar")
    tr.settle(COMMISSION, REF_COST_RATE)
    return tr


def first_scored_day(qqq: dict[str, SymbolDay], days: list[str]) -> int | None:
    """First day QQQ is complete with prior-day daily ATR14 / EMA20 and prior close (spec §3)."""
    for k, d in enumerate(days):
        sd = qqq[d]
        if sd.complete and sd.daily_ready:
            return k
    return None


def run_backtest(qqq: dict[str, SymbolDay], trading_days: list[Date],
                 start_equity: float = START_EQUITY) -> tuple[BacktestResult, list[dict]]:
    """Day loop. Returns the shared BacktestResult and the per-day setups. Day statuses: excluded_incomplete |
    daily_not_ready | min_range | runaway | no_breakout | no_trap | late_trap | stop_beyond_fill | zero_shares |
    traded. `skips` counts the non-traded statuses only (traded days are in the day records)."""
    days = [d.isoformat() for d in trading_days]
    k0 = first_scored_day(qqq, days)
    scored = days[k0:] if k0 is not None else []
    equity = ref_equity = start_equity
    records: list[DayRecord] = []
    trades: list[Trade] = []
    counts: dict[str, int] = {}
    setups: list[dict] = []
    excluded = not_ready = 0
    for d in scored:
        sd = qqq[d]
        if not sd.complete:
            status = "excluded_incomplete"
            excluded += 1
        elif not sd.daily_ready:
            status = "daily_not_ready"
            not_ready += 1
        else:
            status, s = find_setup(sd)
            if s is not None:
                plan, why = entry_plan(sd, s, equity)
                setups.append({"day": d, "trap_minute": sd.bars[s.index].t, "swing_high": s.swing_high,
                               "orb_high": sd.orb_high, "orb_low": sd.orb_low, "daily_atr": sd.daily_atr,
                               "vwap_at_signal": sd.vwap[s.index],
                               "entry_raw": sd.bars[s.index + 1].open, "skipped": why or None})
                if plan is None:
                    status = why
                else:
                    tr = simulate_trade(sd, s, plan)
                    trades.append(tr)
                    start = equity
                    equity += tr.net_pnl
                    ref_equity += tr.ref_pnl
                    records.append(DayRecord(d, "traded", start, equity, ref_equity, [SYMBOL], 1))
                    continue
        counts[status] = counts.get(status, 0) + 1
        records.append(DayRecord(d, status, equity, equity, ref_equity, [], 0))
    result = BacktestResult(records, trades, dict(sorted(counts.items())), {SYMBOL: excluded},
                            {SYMBOL: not_ready}, qqq_buy_hold(qqq, scored), start_equity)
    return result, setups
