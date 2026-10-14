"""V3 model A simulation on the continuous 1m stream (spec §5, §7).

Per 1m bar: (1) late-completed buckets (missing last minute) -> (2) pending actions at this bar's open (exits, then
entries) -> (3) intrabar stop / fee-lock, then close-based MFE and latch -> (4) gear-1 exit at the day's last bar
close -> (5) buckets completed by this bar (30m first, then the short timeframe); their actions fill at the next
bar's open. Higher-timeframe values are used only after their bucket completes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.explore_orb_vwap_v0.risk import COMMISSION, REF_COST_RATE, SHARE_DP, SLIPPAGE
from src.explore_orb_vwap_v0.strategy import BacktestResult, DayRecord, Leg

from .indicators import (
    EMA,
    M1,
    Aggregator,
    Bucket,
    SuperTrend,
    WilderATR,
    is_last_bar_of_day,
)

SYMBOL = "QQQ"
FRACTION = 0.95
MIN_SHARES = 0.0001
VOL_MIN = 0.0018
VOL_ATR = 14
NO_ENTRY_BEFORE = 575  # fills at 09:30 .. 09:34 are not allowed for new entries
COOLDOWN = timedelta(minutes=15)
MAX_ENTRIES_PER_CYCLE = 2
SCORED_START = "2023-05-09"


@dataclass(frozen=True)
class Config:
    name: str
    st_period: int
    st_mult: float
    short_tf: int
    mfe_latch: float
    fee_lock: float
    hard_stop: float


CONFIGS = (
    Config("V3A_BASE", 10, 2.5, 5, 0.0040, 0.0022, 0.0030),
    Config("V3A_TIGHT", 10, 2.0, 5, 0.0030, 0.0020, 0.0025),
    Config("V3A_WIDE", 14, 3.0, 15, 0.0050, 0.0025, 0.0040),
)


@dataclass
class V3Trade:
    """Field names follow the ORB engine's Trade so the reporter and trade-stats scripts read it."""

    day: str  # entry day
    symbol: str
    side: int
    signal_minute: int
    entry_minute: int
    entry_raw: float
    entry_fill: float
    shares: float
    initial_stop: float  # gear-1 hard stop level
    risk_per_share: float  # entry fill x hard_stop
    target: float  # 0 (no target)
    legs: list[Leg] = field(default_factory=list)
    tp1: bool = False
    exit_reason: str = ""
    net_pnl: float = 0.0
    commission: float = 0.0
    ref_pnl: float = 0.0
    exit_day: str = ""
    swing: bool = False  # gear 2 at exit
    mfe: float = 0.0
    hold_minutes: float = 0.0
    kind: str = "normal"  # normal | switch
    cycle: int = 0

    def settle(self) -> None:
        leg = self.legs[-1]
        self.commission = COMMISSION * 2 * self.shares
        self.net_pnl = self.side * (leg.fill - self.entry_fill) * self.shares - self.commission
        self.ref_pnl = (self.side * (leg.raw - self.entry_raw) * self.shares
                        - REF_COST_RATE * self.shares * (self.entry_raw + leg.raw))


@dataclass
class _Pos:
    trade: V3Trade
    entry_at: datetime
    swing: bool = False
    mfe: float = 0.0


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


class Engine:
    def __init__(self, cfg: Config, start_equity: float = 2000.0) -> None:
        self.cfg = cfg
        self.equity = self.ref_equity = start_equity
        self.start_equity = start_equity
        self.agg30 = Aggregator(30)
        self.aggs = Aggregator(cfg.short_tf)
        self.st = SuperTrend(cfg.st_period, cfg.st_mult)
        self.atr14 = WilderATR(VOL_ATR)
        self.ema9, self.ema21 = EMA(9), EMA(21)
        self.st_dir: int | None = None
        self.vol_ok = False
        self.sign: int | None = None
        self.last_cross: dict[int, datetime] = {}
        self.cycle = 0
        self.cycle_entries = 0
        self.pos: _Pos | None = None
        self.pending_exit: str | None = None
        self.pending_entry: tuple[int, str] | None = None
        self.last_exit_gear1: bool = False
        self.last_exit_at: datetime | None = None
        self.trades: list[V3Trade] = []
        self.counts: dict[str, int] = {}
        self.day_end: dict[str, tuple[float, float, int, bool]] = {}  # day -> (mtm, ref_mtm, entries, holding)
        self._entries_today: dict[str, int] = {}
        self._prev: M1 | None = None

    # ---------------------------------------------------------------- bucket events
    def _on_30m(self, b: Bucket) -> None:
        d = self.st.update(b.h, b.l, b.c)
        atr = self.atr14.update(b.h, b.l, b.c)
        self.vol_ok = atr is not None and atr / b.c >= VOL_MIN
        if d is None:
            return
        flipped = self.st_dir is not None and d != self.st_dir
        if flipped:
            self.cycle += 1
            self.cycle_entries = 0
        self.st_dir = d
        p = self.pos
        # spec §5.2 (J, review): a gear-2 position exits whenever a completed 30m bar's direction is against it; the
        # reverse (switch) entry is made only on the bar where the direction actually flipped
        if p is not None and p.swing and d == -p.trade.side and self.pending_exit is None:
            self.pending_exit = "st_flip"
            if self.vol_ok and flipped:
                self.pending_entry = (d, "switch", b.last_minute)

    def _on_short(self, b: Bucket) -> None:
        e9, e21 = self.ema9.update(b.c), self.ema21.update(b.c)
        if e9 is None or e21 is None:
            return
        sign = 1 if e9 > e21 else -1 if e9 < e21 else 0
        if self.sign is not None and sign != 0 and sign != self.sign:
            self.last_cross[sign] = b.end(self.cfg.short_tf)
        if sign != 0:
            self.sign = sign
        p = self.pos
        if p is not None:
            if not p.swing and sign == -p.trade.side and self.pending_exit is None:
                self.pending_exit = "ema_cross"
            return
        if self.pending_entry is not None or self.pending_exit is not None:
            return
        d = self.st_dir
        if d is None or not self.vol_ok or sign != d:
            return
        if not (b.c > b.o if d > 0 else b.c < b.o):
            return
        if self.cycle_entries >= MAX_ENTRIES_PER_CYCLE:
            self.counts["cycle_cap"] = self.counts.get("cycle_cap", 0) + 1
            return
        if self.last_exit_gear1 and self.last_exit_at is not None:
            now = b.end(self.cfg.short_tf)
            cross = self.last_cross.get(d)
            if _utc(now) - _utc(self.last_exit_at) < COOLDOWN or cross is None or _utc(cross) <= _utc(self.last_exit_at):
                self.counts["cooldown"] = self.counts.get("cooldown", 0) + 1
                return
        self.pending_entry = (d, "normal", b.last_minute)

    # ---------------------------------------------------------------- fills
    def _exit(self, m: M1, minute: int, raw: float, reason: str, at: datetime) -> None:
        p = self.pos
        tr = p.trade
        tr.legs.append(Leg(minute, tr.shares, raw, raw - tr.side * SLIPPAGE, reason))
        tr.exit_reason, tr.exit_day, tr.swing, tr.mfe = reason, m.day, p.swing, p.mfe
        tr.hold_minutes = (_utc(at) - _utc(p.entry_at)).total_seconds() / 60.0
        tr.settle()
        self.equity += tr.net_pnl
        self.ref_equity += tr.ref_pnl
        self.trades.append(tr)
        self.last_exit_gear1 = not p.swing
        self.last_exit_at = at
        self.pos = None

    def _enter(self, m: M1, side: int, kind: str, signal_minute: int | None = None) -> None:
        if m.day < SCORED_START:
            return
        if m.minute < NO_ENTRY_BEFORE:
            self.counts["opening_5min"] = self.counts.get("opening_5min", 0) + 1
            return
        if is_last_bar_of_day(m):
            self.counts["last_bar"] = self.counts.get("last_bar", 0) + 1
            return
        if self.cycle_entries >= MAX_ENTRIES_PER_CYCLE:
            key = "switch_cycle_cap" if kind == "switch" else "cycle_cap_at_fill"
            self.counts[key] = self.counts.get(key, 0) + 1
            return
        raw = m.o
        fill = raw + side * SLIPPAGE
        shares = round(self.equity * FRACTION / fill, SHARE_DP)
        if shares < MIN_SHARES:
            return
        hs = self.cfg.hard_stop
        sig = m.minute - 1 if signal_minute is None else signal_minute
        tr = V3Trade(m.day, SYMBOL, side, sig, m.minute, raw, fill, shares, fill * (1 - side * hs), fill * hs,
                     0.0, kind=kind, cycle=self.cycle)
        self.pos = _Pos(tr, m.at())
        self.cycle_entries += 1
        self._entries_today[m.day] = self._entries_today.get(m.day, 0) + 1

    # ---------------------------------------------------------------- main loop
    def step(self, m: M1, is_last_of_stream: bool) -> None:
        cfg = self.cfg
        # a gear-1 position must not carry into a new day (its day's last bar was missing)
        if self.pos is not None and not self.pos.swing and self._prev is not None and m.day != self._prev.day:
            self.pending_exit = self.pending_exit or "gap_eod"
        # (1) late-completed buckets
        b = self.agg30.close_stale(m)
        if b is not None:
            self._on_30m(b)
        b = self.aggs.close_stale(m)
        if b is not None:
            self._on_short(b)
        # (2) pending actions at this open
        if self.pending_exit is not None:
            if self.pos is not None:
                self._exit(m, m.minute, m.o, self.pending_exit, m.at())
            self.pending_exit = None
        if self.pending_entry is not None:
            side, kind, sig = self.pending_entry
            self.pending_entry = None
            # spec §5.1 (J, review): a normal entry queued before a late-completed 30m bar is dropped if that bar
            # turned the direction against it or failed the volatility filter
            if kind == "normal" and (side != self.st_dir or not self.vol_ok):
                self.counts["stale_signal"] = self.counts.get("stale_signal", 0) + 1
            elif self.pos is None:
                self._enter(m, side, kind, sig)
        # (3) intrabar
        p = self.pos
        if p is not None:
            tr = p.trade
            s = tr.side
            if not p.swing:
                level = tr.entry_fill * (1 - s * cfg.hard_stop)
                hit = m.l <= level if s > 0 else m.h >= level
                reason = "hard_stop"
            else:
                level = tr.entry_fill * (1 + s * cfg.fee_lock)
                hit = m.l <= level if s > 0 else m.h >= level
                reason = "fee_lock"
            if hit:
                raw = min(level, m.o) if s > 0 else max(level, m.o)
                self._exit(m, m.minute, raw, reason, m.at())
            else:
                r = s * (m.c - tr.entry_fill) / tr.entry_fill
                p.mfe = max(p.mfe, r)
                if not p.swing and p.mfe >= cfg.mfe_latch:
                    p.swing = True
        # (4) gear-1 close-out at the day's last bar; everything at the end of the data
        end_at = m.at(m.minute + 1)
        if self.pos is not None and ((not self.pos.swing and is_last_bar_of_day(m)) or is_last_of_stream):
            self._exit(m, m.minute, m.c, "end_of_data" if is_last_of_stream else "eod_gear1", end_at)
        # (5) buckets completed by this bar
        b = self.agg30.add(m)
        if b is not None:
            self._on_30m(b)
        b = self.aggs.add(m)
        if b is not None:
            self._on_short(b)
        self._prev = m

    def mark_day(self, m: M1) -> None:
        """End-of-day valuation at the day's last stream bar close (spec §7)."""
        p = self.pos
        mtm, ref = self.equity, self.ref_equity
        if p is not None:
            tr = p.trade
            mtm += tr.side * (m.c - tr.entry_fill) * tr.shares - COMMISSION * tr.shares  # entry commission paid
            ref += tr.side * (m.c - tr.entry_raw) * tr.shares - REF_COST_RATE * tr.shares * tr.entry_raw
        self.day_end[m.day] = (mtm, ref, self._entries_today.get(m.day, 0), p is not None)


def run(stream: list[M1], cfg: Config, calendar_days: list[str], start_equity: float = 2000.0,
        engine_cls=None) -> BacktestResult:
    """Run one config over the stream; day records for the calendar days from SCORED_START. `engine_cls` (default
    Engine) lets V3B (spec EXPLORE_V3B_ASYM_SWING_2000USD.md) reuse the loop."""
    eng = (engine_cls or Engine)(cfg, start_equity)
    n = len(stream)
    for i, m in enumerate(stream):
        eng.step(m, i == n - 1)
        if i == n - 1 or stream[i + 1].day != m.day:
            eng.mark_day(m)
    records: list[DayRecord] = []
    prev = prev_ref = start_equity
    for d in (x for x in calendar_days if x >= SCORED_START):
        if d in eng.day_end:
            mtm, ref, entries, holding = eng.day_end[d]
            status = "traded" if entries else ("holding" if holding else "flat")
        else:
            mtm, ref, entries, status = prev, prev_ref, 0, "no_data"
        records.append(DayRecord(d, status, prev, mtm, ref, [SYMBOL] if entries else [], entries))
        prev, prev_ref = mtm, ref
    trades = [t for t in eng.trades if t.day >= SCORED_START]
    excluded = sum(1 for r in records if r.status == "no_data")
    return BacktestResult(records, trades, dict(sorted(eng.counts.items())), {SYMBOL: excluded}, {SYMBOL: 0}, None,
                          start_equity)
