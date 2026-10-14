"""V3B: the V3A engine with the four corrections of spec docs/research/EXPLORE_V3B_ASYM_SWING_2000USD.md §4.

Stage 0 (gear 1): hard stop + 15m reverse cross, held overnight (no end-of-day exit). Stage 1 (MFE >= latch_1): floor
at lock_1, crosses ignored. Stage 2 (MFE >= latch_2): floor at lock_2. Stages 1-2 also exit when a completed 30m bar's
direction is against the position (no switch). One entry per SuperTrend cycle; anti-chase distance to the 15m EMA21.

Order at one 1m close (same as V3A): the close-based stage promotion (step 3) happens before the buckets completed by
that bar are handled (step 5), so those bucket checks already see the new stage; the new floor applies from the next bar.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.explore_orb_vwap_v0.risk import SHARE_DP, SLIPPAGE

from .engine import (
    COOLDOWN,
    FRACTION,
    MIN_SHARES,
    NO_ENTRY_BEFORE,
    SCORED_START,
    SYMBOL,
    Engine,
    V3Trade,
    _Pos,
    _utc,
)
from .indicators import M1, Bucket, is_last_bar_of_day


@dataclass(frozen=True)
class ConfigB:
    name: str
    st_period: int
    st_mult: float
    vol_min: float
    max_entries: int
    max_ema_dist: float
    hard_stop: float
    latch_1: float
    lock_1: float
    latch_2: float
    lock_2: float
    short_tf: int = 15


CONFIGS_B = (
    ConfigB("V3B_BASE", 14, 3.0, 0.0028, 1, 0.0060, 0.0045, 0.0050, 0.0000, 0.0120, 0.0035),
    ConfigB("V3B_WIDE_HOLD", 14, 3.5, 0.0030, 1, 0.0060, 0.0050, 0.0060, -0.0010, 0.0150, 0.0040),
    ConfigB("V3B_STRICT_SELECT", 14, 3.0, 0.0034, 1, 0.0035, 0.0040, 0.0050, 0.0000, 0.0110, 0.0035),
)


@dataclass
class V3BTrade(V3Trade):
    stage: int = 0  # stage at exit (0 = gear 1)


@dataclass
class _PosB(_Pos):
    stage: int = 0


class EngineB(Engine):
    trade_cls = V3BTrade  # V3C swaps in its own trade / position records
    pos_cls = _PosB

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _on_30m(self, b: Bucket) -> None:
        d = self.st.update(b.h, b.l, b.c)
        atr = self.atr14.update(b.h, b.l, b.c)
        self.vol_ok = atr is not None and atr / b.c >= self.cfg.vol_min
        if d is None:
            return
        if self.st_dir is not None and d != self.st_dir:
            self.cycle += 1
            self.cycle_entries = 0
        self.st_dir = d
        p = self.pos
        if p is not None and p.stage >= 1 and d == -p.trade.side and self.pending_exit is None:
            self.pending_exit = "st_flip"  # spec V3B §4.3-4: exit only, no switch

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
            if p.stage == 0 and sign == -p.trade.side and self.pending_exit is None:
                self.pending_exit = "ema_cross"
            return
        if self.pending_entry is not None or self.pending_exit is not None:
            return
        d = self.st_dir
        if d is None or not self.vol_ok or sign != d:
            return
        if not (b.c > b.o if d > 0 else b.c < b.o):
            return
        if self.cycle_entries >= self.cfg.max_entries:
            self._bump("cycle_cap")
            return
        if abs(b.c - e21) / e21 > self.cfg.max_ema_dist:
            self._bump("chase")
            return
        if self.last_exit_gear1 and self.last_exit_at is not None:
            now = b.end(self.cfg.short_tf)
            cross = self.last_cross.get(d)
            if _utc(now) - _utc(self.last_exit_at) < COOLDOWN or cross is None or _utc(cross) <= _utc(self.last_exit_at):
                self._bump("cooldown")
                return
        self.pending_entry = (d, "normal", b.last_minute)

    def _enter(self, m: M1, side: int, kind: str, signal_minute: int | None = None) -> None:
        if m.day < SCORED_START:
            return
        if m.minute < NO_ENTRY_BEFORE:
            self._bump("opening_5min")
            return
        if is_last_bar_of_day(m):
            self._bump("last_bar")
            return
        if self.cycle_entries >= self.cfg.max_entries:
            self._bump("cycle_cap_at_fill")
            return
        raw = m.o
        fill = raw + side * SLIPPAGE
        shares = round(self.equity * FRACTION / fill, SHARE_DP)
        if shares < MIN_SHARES:
            return
        hs = self.cfg.hard_stop
        sig = m.minute - 1 if signal_minute is None else signal_minute
        tr = self.trade_cls(m.day, SYMBOL, side, sig, m.minute, raw, fill, shares, fill * (1 - side * hs), fill * hs,
                            0.0, kind=kind, cycle=self.cycle)
        self.pos = self.pos_cls(tr, m.at())
        self.cycle_entries += 1
        self._entries_today[m.day] = self._entries_today.get(m.day, 0) + 1

    def _exit(self, m: M1, minute: int, raw: float, reason: str, at) -> None:
        self.pos.trade.stage = self.pos.stage
        super()._exit(m, minute, raw, reason, at)

    def step(self, m: M1, is_last_of_stream: bool) -> None:
        cfg = self.cfg
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
            if kind == "normal" and (side != self.st_dir or not self.vol_ok):
                self._bump("stale_signal")
            elif self.pos is None:
                self._enter(m, side, kind, sig)
        # (3) intrabar: stage floor / hard stop, then close-based MFE and stage promotion
        p = self.pos
        if p is not None:
            tr = p.trade
            s = tr.side
            if p.stage == 0:
                level, reason = tr.entry_fill * (1 - s * cfg.hard_stop), "hard_stop"
            elif p.stage == 1:
                level, reason = tr.entry_fill * (1 + s * cfg.lock_1), "lock_1"
            else:
                level, reason = tr.entry_fill * (1 + s * cfg.lock_2), "lock_2"
            hit = m.l <= level if s > 0 else m.h >= level
            if hit:
                raw = min(level, m.o) if s > 0 else max(level, m.o)
                self._exit(m, m.minute, raw, reason, m.at())
            else:
                r = s * (m.c - tr.entry_fill) / tr.entry_fill
                p.mfe = max(p.mfe, r)
                if p.stage < 2 and p.mfe >= cfg.latch_2:
                    p.stage = 2
                elif p.stage < 1 and p.mfe >= cfg.latch_1:
                    p.stage = 1
                p.swing = p.stage >= 1
        # (4) only the end of the data closes a position (no end-of-day exit, spec V3B §4.1)
        if self.pos is not None and is_last_of_stream:
            self._exit(m, m.minute, m.c, "end_of_data", m.at(m.minute + 1))
        # (5) buckets completed by this bar
        b = self.agg30.add(m)
        if b is not None:
            self._on_30m(b)
        b = self.aggs.add(m)
        if b is not None:
            self._on_short(b)
        self._prev = m
