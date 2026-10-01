"""The 8 frozen arms (§4) and the common fill model (§3).

Every arm is long only, at most one trade per arm x symbol-day, no re-entry, unit notional.
Signals confirmed on the close of bar t fill no earlier than the open of bar t+1; daily
indicators come from t-1 only (the previous NYSE trading day, which must itself be valid).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from src.history import exchange_calendar as cal
from src.research.gross_edge_v2.daily import (
    Indicators,
    compute_indicators,
    valid_daily_bars,
)
from src.research.gross_edge_v2.data import (
    EXCLUDED_BLOCK,
    EXIT_MINUTE,
    OPEN_MINUTE,
    DayBars,
    hm,
    is_valid_symbol_day,
)
from src.research.orb.windows import previous_trading_day

UNIVERSE = (
    "SPY", "QQQ", "IWM", "TQQQ", "SQQQ", "SOXL", "SOXS", "AAPL", "MSFT", "NVDA",
    "AMD", "AVGO", "AMZN", "META", "GOOGL", "TSLA", "MU", "NFLX", "CRM", "PLTR",
)
INVERSE = frozenset({"SQQQ", "SOXS"})
A1_LEGS = (("QQQ", 1.5, "TQQQ", "SQQQ"), ("SOXL", 4.5, "SOXL", "SOXS"))
A2_SYMBOLS = (
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "AMZN", "META", "GOOGL", "TSLA", "MU",
    "NFLX", "CRM", "PLTR",
)
A2_THRESHOLD_PCT = 3.0
D1_SYMBOLS = ("SOXL", "SOXS", "TQQQ", "SQQQ", "TSLA", "PLTR", "AMD", "MU", "AVGO")
B3_RANGE_MULT = 0.6
C1_RSI2_LT = 5.0
C1_ATR_MULT = 0.5
C2_RSI3_LT = 30.0
D1_K = 0.6
D1_STOP_ATR = 0.3

A_SIGNAL_MINUTE = hm("14:44")
A_ENTRY_MINUTE = hm("14:45")
SCAN_LAST_MINUTE = hm("15:30")
B_FIRST_MINUTE = hm("09:31")
C2_RANGE_LAST = hm("10:29")
C2_FIRST_MINUTE = hm("10:30")
D1_SIGNAL_FIRST = hm("10:00")
D1_SIGNAL_LAST = hm("15:00")

# Registry: (arm, traded symbols). Frozen-contract fields are cross-checked below.
ARM_SYMBOLS: Mapping[str, tuple[str, ...]] = {
    "A1": ("TQQQ", "SQQQ", "SOXL", "SOXS"),
    "A2": A2_SYMBOLS,
    "B1": UNIVERSE,
    "B2": UNIVERSE,
    "B3": UNIVERSE,
    "C1": UNIVERSE,
    "C2": UNIVERSE,
    "D1": D1_SYMBOLS,
}
_EXPECTED_FIELDS: Mapping[str, Mapping[str, object]] = {
    "A1": {"signal": "close_1444 / open_0930 - 1", "entry": "14:45 open",
           "stop_loss": None, "exit": "15:55 open"},
    "A2": {"signal": "close_1444 / open_0930 - 1 >= 3.0% and close_1444 > vwap_1444",
           "entry": "14:45 open", "stop_loss": None, "exit": "15:55 open"},
    "B1": {"condition_t_minus_1": "nr7",
           "entry": "buy_stop O_t + stretch10, active 09:31-15:30",
           "stop_loss": "O_t", "exit": "15:55 open"},
    "B2": {"condition_t_minus_1": "inside_day and nr4",
           "entry": "buy_stop O_t + stretch10, active 09:31-15:30",
           "stop_loss": "O_t", "exit": "15:55 open"},
    "B3": {"condition_t_minus_1": "C(t-1) > SMA20",
           "entry": "buy_stop O_t + 0.6 * range(t-1), active 09:31-15:30",
           "stop_loss": "O_t", "exit": "15:55 open"},
    "C1": {"condition_t_minus_1": "RSI2 < 5 and C(t-1) > SMA200",
           "entry": ("strict limit C(t-1) - 0.5 * ATR10, active 09:30-15:30; "
                     "09:30 open below limit fills at open"),
           "stop_loss": None, "exit": "15:55 open"},
    "C2": {"condition_t_minus_1": "RSI3(ROC1) < 30",
           "entry": "buy_stop 09:30-10:29 high, active 10:30-15:30",
           "stop_loss": "09:30-10:29 low", "exit": "15:55 open"},
    "D1": {"signal": "first bar 10:00-15:00 with close <= VWAP - 0.6 * ATR14(t-1)",
           "entry": "next bar open", "stop_loss": "entry - 0.3 * ATR14(t-1)",
           "exit": "15:55 open"},
}

# Skip reasons (diagnostics). "no_signal" / "no_fill" are ordinary outcomes.
MISSING_BAR = "missing_required_bar"
PREV_INVALID = "t_minus_1_invalid"
UNDEFINED = "undefined_indicator"
NO_SIGNAL = "no_signal"
NO_FILL = "no_fill"
INVALID_DAY = "invalid_symbol_day"


class RegistryMismatchError(RuntimeError):
    """The frozen contract's arm definitions differ from the coded registry."""


def verify_registry(contract: Mapping[str, object]) -> None:
    arms = {arm["id"]: arm for arm in contract["arms"]}  # type: ignore[index]
    if tuple(arms) != tuple(ARM_SYMBOLS):
        raise RegistryMismatchError(f"arm ids {tuple(arms)}")
    if tuple(contract["universe"]) != UNIVERSE:  # type: ignore[arg-type]
        raise RegistryMismatchError("universe differs")
    if frozenset(contract["inverse_etfs"]) != INVERSE:  # type: ignore[arg-type]
        raise RegistryMismatchError("inverse ETF set differs")
    for arm_id, fields in _EXPECTED_FIELDS.items():
        for key, value in fields.items():
            if arms[arm_id].get(key) != value:
                raise RegistryMismatchError(f"{arm_id}.{key} = {arms[arm_id].get(key)!r}")
    legs = tuple(
        (leg["signal_symbol"], float(leg["threshold_pct"]), leg["up_buy"], leg["down_buy"])
        for leg in arms["A1"]["legs"]
    )
    if legs != A1_LEGS:
        raise RegistryMismatchError(f"A1 legs {legs}")
    if tuple(arms["A2"]["symbols"]) != A2_SYMBOLS:
        raise RegistryMismatchError("A2 symbols differ")
    if tuple(arms["D1"]["symbols"]) != D1_SYMBOLS:
        raise RegistryMismatchError("D1 symbols differ")
    for arm_id in ("B1", "B2", "B3", "C1", "C2"):
        if arms[arm_id]["symbols"] != "universe":
            raise RegistryMismatchError(f"{arm_id} symbols differ")
    execution = contract["execution"]  # type: ignore[index]
    if execution["time_exit_et"] != "15:55" or execution["same_bar_conflict"] != (
        "stop_loss_first"
    ):
        raise RegistryMismatchError("execution model differs")
    if tuple(contract["inverse_leg_policy"]["arms_with_inverse_legs"]) != (  # type: ignore[index]
        "A1", "B1", "B2", "B3", "C1", "C2", "D1"
    ):
        raise RegistryMismatchError("inverse-leg arm list differs")


@dataclass(frozen=True)
class Trade:
    arm_id: str
    symbol: str
    day: date
    entry_minute: int
    entry_price: float
    exit_minute: int
    exit_price: float
    exit_reason: str  # "time" | "stop" | "target"
    gross_pct_points: float

    @property
    def inverse(self) -> bool:
        return self.symbol in INVERSE

    def sort_key(self) -> tuple[date, int, str]:
        return (self.day, self.entry_minute, self.symbol)


# ---------------------------------------------------------------------------
# Fill model (§3)
# ---------------------------------------------------------------------------
def buy_stop_fill(bars: DayBars, stop: float, first: int, last: int) -> tuple[int, float] | None:
    """First bar in [first, last] with high >= stop fills at max(stop, open); gaps skipped."""
    for minute in range(first, last + 1):
        bar = bars.bar(minute)
        if bar is not None and bar[1] >= stop:
            return minute, max(stop, bar[0])
    return None


def limit_buy_fill(bars: DayBars, limit: float, first: int, last: int) -> tuple[int, float] | None:
    """Strict limit: first bar with low < limit fills at min(limit, open); touch does not fill."""
    for minute in range(first, last + 1):
        bar = bars.bar(minute)
        if bar is not None and bar[2] < limit:
            return minute, min(limit, bar[0])
    return None


def manage_exit(
    bars: DayBars,
    entry_minute: int,
    *,
    stop_loss: float | None,
    vwap_target: bool,
) -> tuple[int, float, str]:
    """Walk bars from the entry bar to 15:54; otherwise exit at the 15:55 open.

    - stop: entry bar inclusive; entry bar exits at the stop price, later bars at
      min(stop, open) when low <= stop.
    - D target: from the bar after entry, resting sell limit = VWAP at the previous bar's
      close; fills when high >= limit at max(limit, open). Stop is checked first.
    Missing bars are skipped without any fill.
    """
    for minute in range(entry_minute, EXIT_MINUTE):
        bar = bars.bar(minute)
        if bar is None:
            continue
        bar_open, bar_high, bar_low, _ = bar
        if stop_loss is not None and bar_low <= stop_loss:
            price = stop_loss if minute == entry_minute else min(stop_loss, bar_open)
            return minute, price, "stop"
        if vwap_target and minute > entry_minute:
            # Cumulative VWAP over every bar strictly before this minute (known at its open);
            # a missing previous bar therefore uses the last available cumulative value.
            target = bars.vwap_through(minute - 1)
            if target is not None and bar_high >= target:
                return minute, max(target, bar_open), "target"
    exit_bar = bars.bar(EXIT_MINUTE)
    if exit_bar is None:  # callers require 15:55 before entering
        raise RuntimeError("15:55 bar required before entry")
    return EXIT_MINUTE, exit_bar[0], "time"


def make_trade(
    arm_id: str, bars: DayBars, entry: tuple[int, float], *,
    stop_loss: float | None = None, vwap_target: bool = False,
) -> Trade:
    entry_minute, entry_price = entry
    exit_minute, exit_price, reason = manage_exit(
        bars, entry_minute, stop_loss=stop_loss, vwap_target=vwap_target
    )
    return Trade(
        arm_id=arm_id, symbol=bars.symbol, day=bars.day,
        entry_minute=entry_minute, entry_price=entry_price,
        exit_minute=exit_minute, exit_price=exit_price, exit_reason=reason,
        gross_pct_points=(exit_price / entry_price - 1.0) * 100.0,
    )


# ---------------------------------------------------------------------------
# Arms. Each returns a Trade or a skip-reason string. Callers pass only bars of a valid
# evaluated symbol-day (None otherwise) and indicators of t-1 (None if t-1 invalid).
# ---------------------------------------------------------------------------
def _has_all(bars: DayBars, minutes: tuple[int, ...]) -> bool:
    return all(bars.has(minute) for minute in minutes)


A_REQUIRED = (OPEN_MINUTE, A_SIGNAL_MINUTE, A_ENTRY_MINUTE, EXIT_MINUTE)
COMMON_REQUIRED = (OPEN_MINUTE, EXIT_MINUTE)


def _late_day_return_pct(bars: DayBars) -> float:
    open_0930 = bars.bar(OPEN_MINUTE)[0]  # type: ignore[index]
    close_1444 = bars.bar(A_SIGNAL_MINUTE)[3]  # type: ignore[index]
    return (close_1444 / open_0930 - 1.0) * 100.0


def a1_leg(
    leg: tuple[str, float, str, str],
    signal_bars: DayBars | None,
    traded: Mapping[str, DayBars | None],
) -> Trade | str:
    """One A1 leg. Signal and traded symbol must both be valid and carry the A bars.

    Conservative reading of §2.4: both symbols need 09:30, 14:44, 14:45 and 15:55.
    """
    _signal_symbol, threshold, up_buy, down_buy = leg
    if signal_bars is None:
        return INVALID_DAY
    if not _has_all(signal_bars, A_REQUIRED):
        return MISSING_BAR
    move = _late_day_return_pct(signal_bars)
    if move >= threshold:
        buy = up_buy
    elif move <= -threshold:
        buy = down_buy
    else:
        return NO_SIGNAL
    bars = traded.get(buy)
    if bars is None:
        return INVALID_DAY
    if not _has_all(bars, A_REQUIRED):
        return MISSING_BAR
    return make_trade("A1", bars, (A_ENTRY_MINUTE, bars.bar(A_ENTRY_MINUTE)[0]))  # type: ignore[index]


def a2(bars: DayBars) -> Trade | str:
    if not _has_all(bars, A_REQUIRED):
        return MISSING_BAR
    vwap = bars.vwap_through(A_SIGNAL_MINUTE)
    close_1444 = bars.bar(A_SIGNAL_MINUTE)[3]  # type: ignore[index]
    if vwap is None or _late_day_return_pct(bars) < A2_THRESHOLD_PCT or close_1444 <= vwap:
        return NO_SIGNAL
    return make_trade("A2", bars, (A_ENTRY_MINUTE, bars.bar(A_ENTRY_MINUTE)[0]))  # type: ignore[index]


def b_arm(arm_id: str, bars: DayBars, prev: Indicators | None) -> Trade | str:
    if not _has_all(bars, COMMON_REQUIRED):
        return MISSING_BAR
    if prev is None:
        return PREV_INVALID
    if arm_id == "B1":
        condition, offset = prev.nr7, prev.stretch10
    elif arm_id == "B2":
        both = None if prev.inside is None or prev.nr4 is None else (prev.inside and prev.nr4)
        condition, offset = both, prev.stretch10
    elif arm_id == "B3":
        condition = None if prev.sma20 is None else prev.close > prev.sma20
        offset = B3_RANGE_MULT * prev.range
    else:
        raise ValueError(arm_id)
    if condition is None or offset is None:
        return UNDEFINED
    if not condition:
        return NO_SIGNAL
    day_open = bars.bar(OPEN_MINUTE)[0]  # type: ignore[index]
    entry = buy_stop_fill(bars, day_open + offset, B_FIRST_MINUTE, SCAN_LAST_MINUTE)
    if entry is None:
        return NO_FILL
    return make_trade(arm_id, bars, entry, stop_loss=day_open)


def c1(bars: DayBars, prev: Indicators | None) -> Trade | str:
    if not _has_all(bars, COMMON_REQUIRED):
        return MISSING_BAR
    if prev is None:
        return PREV_INVALID
    if prev.rsi2 is None or prev.sma200 is None or prev.atr10 is None:
        return UNDEFINED
    if not (prev.rsi2 < C1_RSI2_LT and prev.close > prev.sma200):
        return NO_SIGNAL
    limit = prev.close - C1_ATR_MULT * prev.atr10
    entry = limit_buy_fill(bars, limit, OPEN_MINUTE, SCAN_LAST_MINUTE)
    if entry is None:
        return NO_FILL
    return make_trade("C1", bars, entry)


def c2(bars: DayBars, prev: Indicators | None) -> Trade | str:
    if not _has_all(bars, COMMON_REQUIRED):
        return MISSING_BAR
    if prev is None:
        return PREV_INVALID
    if prev.rsi3_roc1 is None:
        return UNDEFINED
    if not prev.rsi3_roc1 < C2_RSI3_LT:
        return NO_SIGNAL
    if not bars.has(C2_RANGE_LAST):
        return MISSING_BAR
    window = [bars.bar(m) for m in range(OPEN_MINUTE, C2_RANGE_LAST + 1)]
    present = [bar for bar in window if bar is not None]
    range_high = max(bar[1] for bar in present)
    range_low = min(bar[2] for bar in present)
    entry = buy_stop_fill(bars, range_high, C2_FIRST_MINUTE, SCAN_LAST_MINUTE)
    if entry is None:
        return NO_FILL
    return make_trade("C2", bars, entry, stop_loss=range_low)


def d1(bars: DayBars, prev: Indicators | None) -> Trade | str:
    if not _has_all(bars, COMMON_REQUIRED):
        return MISSING_BAR
    if prev is None:
        return PREV_INVALID
    if prev.atr14 is None:
        return UNDEFINED
    for minute in range(D1_SIGNAL_FIRST, D1_SIGNAL_LAST + 1):
        bar = bars.bar(minute)
        vwap = bars.vwap_through(minute)
        if bar is None or vwap is None:
            continue
        if bar[3] <= vwap - D1_K * prev.atr14:
            # First occurrence only: if the next bar is missing the arm skips the day.
            entry_bar = bars.bar(minute + 1)
            if entry_bar is None:
                return MISSING_BAR
            entry_price = entry_bar[0]
            return make_trade(
                "D1", bars, (minute + 1, entry_price),
                stop_loss=entry_price - D1_STOP_ATR * prev.atr14, vwap_target=True,
            )
    return NO_SIGNAL


# ---------------------------------------------------------------------------
# Orchestration over evaluated days
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Extraction:
    trades: tuple[Trade, ...]  # sorted by (day, entry minute, symbol) within each arm
    skip_counts: Mapping[str, Mapping[str, int]]  # arm -> reason -> count
    invalid_symbol_days: Mapping[str, int]  # symbol -> invalid evaluated symbol-days


def valid_bars_for(bars_by_day: Mapping[date, DayBars], day: date) -> DayBars | None:
    bars = bars_by_day.get(day)
    if bars is None or not is_valid_symbol_day(day, bars.count):
        return None
    return bars


def extract_trades(
    bars_by_symbol: Mapping[str, Mapping[date, DayBars]],
    evaluated_days: Sequence[date],
) -> Extraction:
    """Run all 8 arms over the evaluated days.

    ``bars_by_symbol`` must hold every loaded day from the warm-up start, so that valid
    excluded-block days still feed the indicators. Only ``evaluated_days`` are traded.
    """
    for day in evaluated_days:
        if cal.is_early_close(day) or EXCLUDED_BLOCK[0] <= day <= EXCLUDED_BLOCK[1]:
            raise ValueError(f"{day} is not an evaluated trading day")
    indicators = {
        symbol: compute_indicators(valid_daily_bars(bars_by_symbol.get(symbol, {})))
        for symbol in UNIVERSE
    }
    trades: list[Trade] = []
    skips: dict[str, Counter[str]] = {arm_id: Counter() for arm_id in ARM_SYMBOLS}
    invalid: Counter[str] = Counter({symbol: 0 for symbol in UNIVERSE})

    def record(arm_id: str, outcome: Trade | str) -> None:
        if isinstance(outcome, Trade):
            trades.append(outcome)
        else:
            skips[arm_id][outcome] += 1

    for day in sorted(set(evaluated_days)):
        today = {
            symbol: valid_bars_for(bars_by_symbol.get(symbol, {}), day) for symbol in UNIVERSE
        }
        for symbol, bars in today.items():
            if bars is None:
                invalid[symbol] += 1
        prev_day = previous_trading_day(day)
        # Indicators exist only for valid days, so an invalid t-1 yields None (no trade).
        # An early-close t-1 is not a valid daily bar (§3.1), so it also blocks B/C/D.
        prev = {symbol: indicators[symbol].get(prev_day) for symbol in UNIVERSE}

        for leg in A1_LEGS:
            record("A1", a1_leg(leg, today[leg[0]], today))
        for symbol in A2_SYMBOLS:
            bars = today[symbol]
            record("A2", INVALID_DAY if bars is None else a2(bars))
        for symbol in UNIVERSE:
            bars = today[symbol]
            for arm_id in ("B1", "B2", "B3"):
                record(arm_id, INVALID_DAY if bars is None else b_arm(arm_id, bars, prev[symbol]))
            record("C1", INVALID_DAY if bars is None else c1(bars, prev[symbol]))
            record("C2", INVALID_DAY if bars is None else c2(bars, prev[symbol]))
        for symbol in D1_SYMBOLS:
            bars = today[symbol]
            record("D1", INVALID_DAY if bars is None else d1(bars, prev[symbol]))

    ordered = tuple(sorted(trades, key=lambda trade: (trade.arm_id, *trade.sort_key())))
    return Extraction(
        trades=ordered,
        skip_counts={arm: dict(sorted(counter.items())) for arm, counter in skips.items()},
        invalid_symbol_days=dict(sorted(invalid.items())),
    )
