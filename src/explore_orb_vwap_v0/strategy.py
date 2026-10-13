"""Breakout -> pullback-support entry, exits, and the day-by-day account loop (spec §3, §2).

Timing: every decision at 1m bar t uses only bars <= t (signal bar t, breakout states, VWAP through t, 5-minute
bars completed by the end of t); the entry fills at the open of bar t+1 plus slippage. Exits are checked from the
fill bar t+1 onward, in the order: pending EMA20 exit at the open, 15:50 flatten at the open, stop, TP1, then the
EMA20 check when the bar completes a 5-minute bar (exit at the next 1m open).

`EngineConfig` selects V0 (default, unchanged behaviour) or the V1 additions (spec V1 §3): daily-filter readiness,
the minimum ORB range and the EMA20 direction-equal exclusion before the watchlist; the breakout volume rule when
arming breakouts; the direction / allow_short filters on support signals; fractional sizing in the RiskManager.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from .config import V0, EngineConfig, EngineConfigV4
from .indicators import OPEN_MIN, completed_five, completes_five
from .risk import START_EQUITY, EntryPlan, RiskManager
from .universe import ORB_BARS, Candidate, SniperFilter, SymbolDay, UniverseFilter

BREAKOUT_FROM = 585  # 09:45: breakout bars count from here
SIGNAL_FIRST = 600  # signal bar start 10:00
SIGNAL_LAST = 899  # signal bar start 14:59 (config.signal_last; V4: 869)
LAST_FILL = 900  # no fill after 15:00 (config.last_fill; V4: 870)
FLATTEN_BEFORE_CLOSE = 10  # 15:50 (early close: close - 10 min)
NEAR = 0.001  # 0.10% band around ORB / VWAP
SWING_BARS = 3


@dataclass
class Leg:
    minute: int
    shares: int | float
    raw: float  # price before slippage
    fill: float
    reason: str


@dataclass
class Trade:
    day: str
    symbol: str
    side: int
    signal_minute: int
    entry_minute: int
    entry_raw: float
    entry_fill: float
    shares: int | float
    initial_stop: float
    risk_per_share: float
    target: float
    legs: list[Leg] = field(default_factory=list)
    tp1: bool = False
    exit_reason: str = ""
    net_pnl: float = 0.0
    commission: float = 0.0
    ref_pnl: float = 0.0

    def settle(self, commission_per_share: float, ref_rate: float) -> None:
        exit_value = sum(leg.shares * leg.fill for leg in self.legs)
        exit_raw = sum(leg.shares * leg.raw for leg in self.legs)
        self.commission = commission_per_share * 2 * self.shares
        self.net_pnl = self.side * (exit_value - self.shares * self.entry_fill) - self.commission
        ref_cost = ref_rate * (self.shares * self.entry_raw + exit_raw)
        self.ref_pnl = self.side * (exit_raw - self.shares * self.entry_raw) - ref_cost


@dataclass
class _Watch:
    sd: SymbolDay
    long_break: int | None = None
    short_break: int | None = None
    last_exit: int = -1


@dataclass
class _Position:
    watch: _Watch
    trade: Trade
    remaining: int | float
    stop: float
    pending_exit: bool = False


def support_signal(sd: SymbolDay, i: int, long_break: int | None, short_break: int | None) -> int:
    """+1 long / -1 short pullback-support candle at bar index i, 0 for none or for both (spec §3.2)."""
    b = sd.bars[i]
    t = b.t
    v = sd.vwap[i]
    if v is None:
        return 0
    long_sig = (long_break is not None and long_break < t
                and (b.low <= sd.orb_high * (1 + NEAR) or b.low <= v * (1 + NEAR))
                and b.close > b.open
                and b.open - b.low >= b.close - b.open and b.open - b.low > 0
                and b.close > v and b.close > sd.orb_high)
    short_sig = (short_break is not None and short_break < t
                 and (b.high >= sd.orb_low * (1 - NEAR) or b.high >= v * (1 - NEAR))
                 and b.close < b.open
                 and b.high - b.open >= b.open - b.close and b.high - b.open > 0
                 and b.close < v and b.close < sd.orb_low)
    if long_sig and short_sig:
        return 0
    return 1 if long_sig else -1 if short_sig else 0


def swing_and_atr(sd: SymbolDay, t: int, side: int) -> tuple[float, float] | None:
    """Swing low (long) / high (short) of the last 3 5-minute bars completed by the end of bar t, and ATR14
    after the last completed one. None when not available."""
    k = completed_five(t)
    if k < SWING_BARS:
        return None
    last = sd.five[k - SWING_BARS:k]
    atr = last[-1].atr
    if atr is None:
        return None
    swing = min(f.low for f in last) if side > 0 else max(f.high for f in last)
    return swing, atr


def breakout_volume_ok(sd: SymbolDay, i: int, mult: float | None) -> bool:
    """Spec V1 §3.3: bar volume >= mult * (09:30~09:44 volume / 15). Always True when the rule is off (V0)."""
    return mult is None or sd.bars[i].volume >= mult * (sd.orb_volume / ORB_BARS)


class StrategyEngine:
    def __init__(self, risk: RiskManager | None = None, ref_rate: float = 0.001,
                 config: EngineConfig = V0) -> None:
        self.config = config
        self.risk = risk or RiskManager(config=config)
        self.ref_rate = ref_rate

    def simulate_day(self, day: str, watch: list[SymbolDay], day_start_equity: float) -> tuple[list[Trade], dict]:
        """Run one day for the watchlist (ordered by |RS| rank). Returns trades and skip counters."""
        ws = [_Watch(sd) for sd in watch]
        skips: dict[str, int] = {}
        trades: list[Trade] = []
        if not ws:
            return trades, skips
        close = ws[0].sd.close_minute
        if any(w.sd.close_minute != close for w in ws):
            raise ValueError(f"{day}: watchlist symbols disagree on the close minute")
        flat = close - FLATTEN_BEFORE_CLOSE
        realized = 0.0
        losers = 0
        pos: _Position | None = None
        for i in range(flat - OPEN_MIN + 1):
            t = OPEN_MIN + i
            if pos is not None and self._manage(pos, i, t, flat):
                tr = pos.trade
                tr.settle(self.risk.commission, self.ref_rate)
                trades.append(tr)
                realized += tr.net_pnl
                losers += tr.net_pnl < 0
                pos.watch.last_exit = t
                pos = None
            vol_mult = self.config.breakout_volume_mult
            for w in ws:
                b = w.sd.bars[i]
                if t >= BREAKOUT_FROM:
                    if w.long_break is None and b.close > w.sd.orb_high:
                        if breakout_volume_ok(w.sd, i, vol_mult):
                            w.long_break = t
                        else:
                            skips["breakout_volume"] = skips.get("breakout_volume", 0) + 1
                    if w.short_break is None and b.close < w.sd.orb_low:
                        if breakout_volume_ok(w.sd, i, vol_mult):
                            w.short_break = t
                        else:
                            skips["breakout_volume"] = skips.get("breakout_volume", 0) + 1
            if (pos is not None or not SIGNAL_FIRST <= t <= self.config.signal_last
                    or t + 1 > self.config.last_fill or t + 1 >= flat):
                continue
            if self.risk.kill_switch(day_start_equity, realized, losers):
                continue
            for w in ws:
                if t <= w.last_exit:
                    continue
                side = support_signal(w.sd, i, w.long_break, w.short_break)
                if side == 0:
                    continue
                if self.config.direction_filter and side != w.sd.direction:
                    skips["direction_filter"] = skips.get("direction_filter", 0) + 1
                    continue
                if not self.config.allow_short and side < 0:
                    skips["short_disabled"] = skips.get("short_disabled", 0) + 1
                    continue
                sa = swing_and_atr(w.sd, t, side)
                if sa is None:
                    skips["no_indicator"] = skips.get("no_indicator", 0) + 1
                    continue
                cash = day_start_equity + realized
                plan, why = self.risk.plan(side, w.sd.bars[i + 1].open, sa[0], sa[1], day_start_equity, cash)
                if plan is None:
                    skips[why] = skips.get(why, 0) + 1
                    continue
                pos = self._open(day, w, t, plan)
                break
        if pos is not None:
            raise AssertionError(f"{day}: position still open after the flatten bar")
        return trades, skips

    @staticmethod
    def _open(day: str, w: _Watch, t: int, plan: EntryPlan) -> _Position:
        tr = Trade(day, w.sd.symbol, plan.side, t, t + 1, plan.raw, plan.fill, plan.shares, plan.stop,
                   plan.risk_per_share, plan.target)
        return _Position(w, tr, plan.shares, plan.stop)

    def _exit_all(self, pos: _Position, t: int, raw: float, reason: str) -> bool:
        tr = pos.trade
        tr.legs.append(Leg(t, pos.remaining, raw, self.risk.exit_fill(tr.side, raw), reason))
        tr.exit_reason = reason
        pos.remaining = 0
        return True

    def _manage(self, pos: _Position, i: int, t: int, flat: int) -> bool:
        """Process bar i for the open position. True when the position is fully closed."""
        tr = pos.trade
        if i < tr.entry_minute - OPEN_MIN:
            return False
        sd = pos.watch.sd
        b = sd.bars[i]
        side = tr.side
        if pos.pending_exit:
            return self._exit_all(pos, t, b.open, "ema20")
        if t == flat:
            return self._exit_all(pos, t, b.open, "flatten")
        stop_hit = b.low <= pos.stop if side > 0 else b.high >= pos.stop
        if stop_hit:
            raw = min(pos.stop, b.open) if side > 0 else max(pos.stop, b.open)
            return self._exit_all(pos, t, raw, "breakeven" if tr.tp1 else "stop")
        if not tr.tp1 and (b.high >= tr.target if side > 0 else b.low <= tr.target):
            q = self.risk.tp1_quantity(tr.shares)
            tr.legs.append(Leg(t, q, tr.target, self.risk.exit_fill(side, tr.target), "tp1"))
            tr.tp1 = True
            pos.remaining = round(pos.remaining - q, 4)  # exact for ints; avoids float drift for fractional
            pos.stop = tr.entry_fill  # breakeven, checked from the next bar (stop was checked above)
            if pos.remaining == 0:
                tr.exit_reason = "tp1"
                return True
        if tr.tp1 and completes_five(t):
            f = sd.five[completed_five(t) - 1]
            if f.ema is not None and (f.close < f.ema if side > 0 else f.close > f.ema):
                pos.pending_exit = True
        return False


@dataclass
class DayRecord:
    day: str
    # spy_excluded (never without an RS reference) | no_candidates | no_trade | traded
    # | min_range_after_pick (V4 only: the picked stock failed the minimum range, spec V4 §4.2)
    status: str
    start_equity: float
    end_equity: float
    ref_end_equity: float
    watchlist: list[str]
    trades: int


@dataclass
class BacktestResult:
    days: list[DayRecord]
    trades: list[Trade]
    skips: dict[str, int]
    excluded_symbol_days: dict[str, int]
    not_ready_symbol_days: dict[str, int]
    qqq_buy_hold: dict | None
    start_equity: float
    filter_symbol_days: dict[str, dict[str, int]] | None = None  # V1 pre-watchlist exclusions (None under V0, V4)
    sniper: dict | None = None  # V4 only: per-day picks, pick counts, split breaks (spec V4 §6)


def first_scored_day(prepared: dict[str, dict[str, SymbolDay]], days: list[str], ref: str | None = "SPY",
                     need_daily: bool = False, need_rvol: bool = True) -> int | None:
    """Index of the first day where the RS reference is complete with a prior close and some symbol is ready.

    `ref` None (spec V2 §1.3): the first day some traded symbol is ready, and also daily-ready (prior daily EMA20 /
    ATR14 known) when `need_daily`. `need_daily` is ignored when a reference is given (V0 / V1 unchanged).
    `need_rvol` False (spec V3 §2): readiness does not include the 20-session RVOL base."""
    for k, d in enumerate(days):
        if ref is None:
            if any(p[d].is_ready(need_rvol) and (not need_daily or p[d].daily_ready) for p in prepared.values()):
                return k
            continue
        spy = prepared[ref][d]
        if spy.complete and spy.prior_close is not None and any(p[d].is_ready(need_rvol) for p in prepared.values()):
            return k
    return None


def daily_exclusion(sd: SymbolDay, config: EngineConfig) -> str | None:
    """Spec V1 §3.1~§3.3 pre-watchlist exclusion for a V0-ready symbol-day, or None to keep it."""
    if not config.daily_filters:
        return None
    if not sd.daily_ready:
        return "daily_not_ready"
    mult = config.min_range_atr_mult
    if mult is not None and sd.orb_high - sd.orb_low < mult * sd.daily_atr:
        return "min_range"
    if config.direction_filter and sd.direction == 0:
        return "direction_equal"
    return None


def run_backtest(prepared: dict[str, dict[str, SymbolDay]], trading_days: list[Date],
                 start_equity: float = START_EQUITY, engine: StrategyEngine | None = None,
                 universe_filter: UniverseFilter | None = None,
                 config: EngineConfig | None = None,
                 benchmark: dict[str, SymbolDay] | None = None) -> BacktestResult:
    """`config` defaults to the engine's (V0 when neither is given). Passing both with different values is an error.

    The RS reference is `config.rs_reference`. With None (spec V2 §1.3) no reference is read, the
    "reference incomplete -> spy_excluded" rule does not apply, and days where no traded symbol passes are
    `no_candidates`.

    The screener follows `config.rvol_min` / `config.gap_band` (None disables; spec V3 §1) unless
    `universe_filter` is given.

    EngineConfigV4 (spec V4 §4): the SniperFilter picks at most one stock (no V1 pre-watchlist exclusions; it needs
    the daily values itself), then the minimum range is checked on that stock only. `benchmark` (QQQ symbol-days,
    never a candidate) feeds the 1x buy-and-hold comparison; without it QQQ is taken from `prepared` as before."""
    if engine is None:
        engine = StrategyEngine(config=config or V0)
    elif config is not None and config != engine.config:
        raise ValueError("run_backtest config differs from the engine config")
    config = engine.config
    ref = config.rs_reference
    need_rvol = config.rvol_min is not None
    gap = config.gap_band
    sniper = isinstance(config, EngineConfigV4)
    if sniper:
        if universe_filter is not None:
            raise ValueError("the V4 sniper uses its own screener")
        uf = SniperFilter(config.rvol_min, config.gap_up_min)
    else:
        uf = universe_filter or UniverseFilter(config.rvol_min, *(gap if gap is not None else (None, None)))
    picks: list[dict] = []
    days = [d.isoformat() for d in trading_days]
    k0 = first_scored_day(prepared, days, ref, need_daily=config.daily_filters, need_rvol=need_rvol)
    scored = days[k0:] if k0 is not None else []
    first_data = {s: min((d for d, sd in p.items() if sd.has_data), default=None)
                  for s, p in prepared.items()}
    equity = ref_equity = start_equity
    records: list[DayRecord] = []
    trades: list[Trade] = []
    skips: dict[str, int] = {}
    excluded = {s: 0 for s in prepared}
    not_ready = {s: 0 for s in prepared}
    v1_excl = {k: {s: 0 for s in prepared} for k in ("daily_not_ready", "min_range", "direction_equal")}
    for d in scored:
        for s, p in prepared.items():
            if first_data[s] is not None and d >= first_data[s]:
                if not p[d].complete:
                    excluded[s] += 1
                elif not p[d].is_ready(need_rvol):
                    not_ready[s] += 1
        spy = prepared[ref][d] if ref is not None else None
        if spy is not None and (not spy.complete or spy.prior_close is None):
            records.append(DayRecord(d, "spy_excluded", equity, equity, ref_equity, [], 0))
            continue
        rows = {s: p[d] for s, p in prepared.items()}
        if not sniper:
            for s in sorted(rows):
                if rows[s].is_ready(need_rvol):
                    why = daily_exclusion(rows[s], config)
                    if why is not None:
                        v1_excl[why][s] += 1
                        del rows[s]
            wl: list[Candidate] = uf.watchlist(rows, spy)
        else:
            passing = uf.passing(rows)
            wl = passing[:1]
        if not wl:
            records.append(DayRecord(d, "no_candidates", equity, equity, ref_equity, [], 0))
            continue
        if sniper:
            pick = prepared[wl[0].symbol][d]
            min_ok = pick.orb_high - pick.orb_low >= config.min_range_atr_mult * pick.daily_atr
            picks.append({"day": d, "symbol": pick.symbol, "rvol": wl[0].rvol, "gap": wl[0].gap,
                          "passing": [c.symbol for c in passing], "min_range_ok": min_ok})
            if not min_ok:
                records.append(DayRecord(d, "min_range_after_pick", equity, equity, ref_equity, [pick.symbol], 0))
                continue
        day_trades, day_skips = engine.simulate_day(d, [prepared[c.symbol][d] for c in wl], equity)
        for key, v in day_skips.items():
            skips[key] = skips.get(key, 0) + v
        start = equity
        equity += sum(tr.net_pnl for tr in day_trades)
        ref_equity += sum(tr.ref_pnl for tr in day_trades)
        trades.extend(day_trades)
        records.append(DayRecord(d, "traded" if day_trades else "no_trade", start, equity, ref_equity,
                                 [c.symbol for c in wl], len(day_trades)))
    sniper_info = None
    if sniper:
        counts: dict[str, dict[str, int]] = {s: {"picked": 0, "min_range_after_pick": 0, "passing": 0}
                                             for s in prepared}
        for pk in picks:
            counts[pk["symbol"]]["picked"] += 1
            counts[pk["symbol"]]["min_range_after_pick"] += not pk["min_range_ok"]
            for s in pk["passing"]:
                counts[s]["passing"] += 1
        sniper_info = {
            "picks": picks,
            "per_symbol": counts,
            "split_breaks": {s: sorted(k for k, sd in p.items() if sd.split_break) for s, p in prepared.items()},
        }
    return BacktestResult(records, trades, skips, excluded, not_ready,
                          qqq_buy_hold(benchmark if benchmark is not None else prepared.get("QQQ"), scored),
                          start_equity, v1_excl if config.has_v1_filters and not sniper else None, sniper_info)


def qqq_buy_hold(qqq: dict[str, SymbolDay] | None, scored: list[str]) -> dict | None:
    """QQQ 1x: open of the first scored day with a complete QQQ session -> close of the last such day."""
    if qqq is None:
        return None
    full = [d for d in scored if qqq[d].complete]
    if not full:
        return None
    o = qqq[full[0]].bars[0].open
    c = qqq[full[-1]].bars[-1].close
    return {"first_day": full[0], "last_day": full[-1], "open": o, "close": c, "return": c / o - 1.0}
