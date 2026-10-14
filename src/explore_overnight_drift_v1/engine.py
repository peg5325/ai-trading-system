"""QQQ overnight drift: buy at the 15:51 open, sell at the next session's 09:35 open (spec §3, §4).

Reuses the ORB exploration engine read-only: the loader, `prepare_symbol` (daily EMA20 through the prior day, VWAP),
`Trade` / `Leg` / `DayRecord` / `BacktestResult` and the reporter, so the result JSON has the same shape and the
existing compare / trade-stats scripts read it. A trade belongs to its entry day D (spec §4.4).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY
from src.explore_orb_vwap_v0.indicators import OPEN_MIN
from src.explore_orb_vwap_v0.risk import (
    COMMISSION,
    REF_COST_RATE,
    SHARE_DP,
    SLIPPAGE,
    START_EQUITY,
)
from src.explore_orb_vwap_v0.strategy import (
    BacktestResult,
    DayRecord,
    Leg,
    Trade,
    qqq_buy_hold,
)
from src.explore_orb_vwap_v0.universe import SymbolDay
from src.models import ET

SYMBOL = "QQQ"
SIGNAL_MIN = 950  # 15:50 bar (its close is the signal)
ENTRY_MIN = 951  # 15:51 open
EXIT_MIN = 575  # next session 09:35 open
STOP_EXIT_MIN = 571  # next session 09:31 open
GAP_STOP = -0.015  # next open / D last close - 1 <= -1.5% -> disaster stop
FRACTION = 0.95
MIN_SHARES = 0.0001
SIDE = 1


def bar(sd: SymbolDay, minute: int):
    return sd.bars[minute - OPEN_MIN]


def entry_status(sd: SymbolDay, nxt: SymbolDay | None, filters: bool = True) -> str:
    """'enter' or the reason for no entry on day D (spec §4.1), checked in this order:
    excluded_incomplete | daily_not_ready | early_close | no_next_day | next_incomplete | below_ema | below_open |
    below_vwap. `nxt` None or after 2026-01-20 means D+1 is outside the window (spec §3: no entry on the last day).
    With `filters` False (no-filter benchmark, spec §6 (2) as amended) the daily EMA20 readiness is not required."""
    if not sd.complete:
        return "excluded_incomplete"
    if filters and sd.daily_ema is None:
        return "daily_not_ready"
    if sd.close_minute <= ENTRY_MIN:
        return "early_close"
    if nxt is None or nxt.day > DEV_LAST_DAY.isoformat():
        return "no_next_day"
    if not nxt.complete:
        return "next_incomplete"
    if filters:
        c = bar(sd, SIGNAL_MIN).close
        if not c > sd.daily_ema:
            return "below_ema"
        if not c > sd.bars[0].open:
            return "below_open"
        v = sd.vwap[SIGNAL_MIN - OPEN_MIN]
        if v is None or not c > v:
            return "below_vwap"
    return "enter"


@dataclass(frozen=True)
class Hold:
    day: str
    exit_day: str
    hours: float


def overnight_trade(sd: SymbolDay, nxt: SymbolDay, equity: float, exit_minute: int = EXIT_MIN,
                    gap_stop: bool = True) -> tuple[Trade | None, Hold | None]:
    """V1 defaults: exit at the 09:35 open, or the 09:31 open on a gap of -1.5% or worse. V2 open-exit configs pass
    exit_minute=STOP_EXIT_MIN, gap_stop=False (always the 09:31 open, reason `open_exit`)."""
    raw = bar(sd, ENTRY_MIN).open
    fill = raw + SLIPPAGE
    shares = round(equity * FRACTION / fill, SHARE_DP)
    if shares < MIN_SHARES:
        return None, None
    d_close = sd.bars[-1].close
    stop_level = d_close * (1 + GAP_STOP)
    tr = Trade(sd.day, sd.symbol, SIDE, SIGNAL_MIN, ENTRY_MIN, raw, fill, shares, stop_level, fill - stop_level, 0.0)
    gap = nxt.bars[0].open / d_close - 1.0
    if not gap_stop:
        minute, reason = exit_minute, "open_exit"
    else:
        minute, reason = (STOP_EXIT_MIN, "disaster_stop") if gap <= GAP_STOP else (exit_minute, "overnight_exit")
    xraw = bar(nxt, minute).open
    tr.legs.append(Leg(minute, shares, xraw, xraw - SLIPPAGE, reason))
    tr.exit_reason = reason
    tr.settle(COMMISSION, REF_COST_RATE)
    return tr, Hold(sd.day, nxt.day, elapsed_hours(sd.day, ENTRY_MIN, nxt.day, minute))


def elapsed_hours(day0: str, minute0: int, day1: str, minute1: int) -> float:
    """Real elapsed hours between two ET minutes of day (daylight-saving changes included)."""
    def at(day: str, minute: int) -> datetime:
        d = Date.fromisoformat(day)
        return datetime(d.year, d.month, d.day, minute // 60, minute % 60, tzinfo=ET)
    # same-tzinfo subtraction is wall-clock arithmetic; compare in UTC to count the DST hour
    return (at(day1, minute1).astimezone(timezone.utc) - at(day0, minute0).astimezone(timezone.utc)).total_seconds() / 3600.0


def first_scored_day(qqq: dict[str, SymbolDay], days: list[str]) -> int | None:
    """First day QQQ is complete with the prior-day daily EMA20 (spec §3)."""
    for k, d in enumerate(days):
        if qqq[d].complete and qqq[d].daily_ema is not None:
            return k
    return None


def run_backtest(qqq: dict[str, SymbolDay], trading_days: list[Date], filters: bool = True,
                 start_equity: float = START_EQUITY,
                 entry: Callable[[SymbolDay, SymbolDay | None], str] | None = None,
                 exit_minute: int = EXIT_MIN, gap_stop: bool = True) -> tuple[BacktestResult, list[Hold]]:
    """Day loop over the scored days. `filters` False is the no-filter benchmark (spec §6 (2)).
    `skips` counts the non-traded statuses. V2 (spec V2 §4): `entry` replaces `entry_status` (the scored start is
    unchanged), `exit_minute` / `gap_stop` go to `overnight_trade`."""
    days = [d.isoformat() for d in trading_days]
    k0 = first_scored_day(qqq, days)
    equity = ref_equity = start_equity
    records: list[DayRecord] = []
    trades: list[Trade] = []
    holds: list[Hold] = []
    counts: dict[str, int] = {}
    excluded = not_ready = 0
    k0 = len(days) if k0 is None else k0
    scored = days[k0:]
    for k in range(k0, len(days)):
        d = days[k]
        sd = qqq[d]
        nxt = qqq[days[k + 1]] if k + 1 < len(days) else None
        status = entry(sd, nxt) if entry is not None else entry_status(sd, nxt, filters)
        excluded += status == "excluded_incomplete"
        not_ready += status == "daily_not_ready"
        if status == "enter":
            tr, hold = overnight_trade(sd, nxt, equity, exit_minute, gap_stop)
            if tr is not None:
                start = equity
                equity += tr.net_pnl
                ref_equity += tr.ref_pnl
                trades.append(tr)
                holds.append(hold)
                records.append(DayRecord(d, "traded", start, equity, ref_equity, [SYMBOL], 1))
                continue
            status = "zero_shares"
        counts[status] = counts.get(status, 0) + 1
        records.append(DayRecord(d, status, equity, equity, ref_equity, [], 0))
    result = BacktestResult(records, trades, dict(sorted(counts.items())), {SYMBOL: excluded},
                            {SYMBOL: not_ready}, qqq_buy_hold(qqq, scored), start_equity)
    return result, holds


def return_split(qqq: dict[str, SymbolDay], days: list[str]) -> dict:
    """Spec §6 (3): over the given days that are complete with a prior close (previous trading day complete),
    cumulative prior close -> open (overnight) and open -> close (intraday) growth, no costs."""
    on = intra = 1.0
    n = 0
    for d in days:
        sd = qqq[d]
        if not sd.complete or sd.prior_close is None:
            continue
        on *= sd.bars[0].open / sd.prior_close
        intra *= sd.bars[-1].close / sd.bars[0].open
        n += 1
    return {"days": n, "overnight_return": on - 1.0, "intraday_return": intra - 1.0,
            "combined_return": on * intra - 1.0}
