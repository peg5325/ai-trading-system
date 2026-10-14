"""V3C: the V3B engine with the three corrections of spec docs/research/EXPLORE_V3C_ASYM_SWING_2000USD.md §4.

(1) Rebound confirmation on the 15m signal bar: EMA9 slope in the trade direction, close beyond EMA9, and a fresh
EMA9/21 cross or a close beyond the previous 15m bar's high (long) / low (short). (2) 30m EMA200 regime: shorts only in
the bear regime (`bear_only`, `bear_only_vol` adds ATR14/close >= short_vol_min), or `tactical` shorts entered in the
bull regime close at the day's last bar unless they reached Stage 3. (3) Three-stage step latch (floors lock_1..lock_3).

Order at one 1m close (same as V3B): close-based stage promotion (step 3) before the day-end bull-short check (step 4)
and the buckets completed by that bar (step 5); a new floor applies from the next bar.
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import COOLDOWN, _utc
from .engine_v3b import EngineB, V3BTrade, _PosB
from .indicators import EMA, M1, Bucket, is_last_bar_of_day

REGIME_EMA = 200
SHORT_REGIMES = ("bear_only", "bear_only_vol", "tactical")


@dataclass(frozen=True)
class ConfigC:
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
    latch_3: float
    lock_3: float
    short_regime: str
    short_vol_min: float = 0.0  # bear_only_vol only
    short_tf: int = 15


CONFIGS_C = (
    ConfigC("V3C_REGIME_ASYM", 14, 3.0, 0.0032, 1, 0.0040, 0.0040,
            0.0045, 0.0000, 0.0075, 0.0030, 0.0125, 0.0060, "bear_only"),
    ConfigC("V3C_TACTICAL_SHORT", 14, 3.0, 0.0034, 1, 0.0035, 0.0040,
            0.0045, 0.0000, 0.0075, 0.0030, 0.0120, 0.0055, "tactical"),
    ConfigC("V3C_STRICT_RUNNER", 14, 3.0, 0.0035, 1, 0.0035, 0.0040,
            0.0050, 0.0005, 0.0080, 0.0035, 0.0130, 0.0060, "bear_only_vol", 0.0042),
)


@dataclass
class V3CTrade(V3BTrade):
    bull_short: bool = False  # tactical short entered in the bull regime


@dataclass
class _PosC(_PosB):
    bull_short: bool = False


class EngineC(EngineB):
    trade_cls = V3CTrade
    pos_cls = _PosC

    def __init__(self, cfg: ConfigC, start_equity: float = 2000.0) -> None:
        if cfg.short_regime not in SHORT_REGIMES:
            raise ValueError(f"unknown short_regime {cfg.short_regime!r}")
        super().__init__(cfg, start_equity)
        self.ema200 = EMA(REGIME_EMA)
        self.bear = False  # last completed 30m close < EMA200 (close == EMA200 or not ready counts as bull, spec §4.2)
        self.atr_ratio: float | None = None
        self.prev15: Bucket | None = None

    def _short_ok(self) -> bool:
        mode = self.cfg.short_regime
        if mode == "tactical":
            return True
        if mode == "bear_only":
            return self.bear
        return self.bear and self.atr_ratio is not None and self.atr_ratio >= self.cfg.short_vol_min

    def _on_30m(self, b: Bucket) -> None:
        super()._on_30m(b)
        atr = self.atr14.value
        self.atr_ratio = None if atr is None else atr / b.c
        e = self.ema200.update(b.c)
        self.bear = e is not None and b.c < e

    def _on_short(self, b: Bucket) -> None:
        prev, self.prev15 = self.prev15, b
        e9_prev = self.ema9.value
        e9, e21 = self.ema9.update(b.c), self.ema21.update(b.c)
        if e9 is None or e21 is None:
            return
        sign = 1 if e9 > e21 else -1 if e9 < e21 else 0
        fresh = self.sign is not None and sign != 0 and sign != self.sign
        if fresh:
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
        if d < 0 and not self._short_ok():
            self._bump("regime_short_block")
            return
        if self.cycle_entries >= self.cfg.max_entries:
            self._bump("cycle_cap")
            return
        if abs(b.c - e21) / e21 > self.cfg.max_ema_dist:
            self._bump("chase")
            return
        slope = e9_prev is not None and d * (e9 - e9_prev) > 0
        beyond_e9 = d * (b.c - e9) > 0
        breakout = prev is not None and (b.c > prev.h if d > 0 else b.c < prev.l)
        if not (slope and beyond_e9 and (fresh or breakout)):
            self._bump("no_rebound_confirm")
            return
        if self.last_exit_gear1 and self.last_exit_at is not None:
            now = b.end(self.cfg.short_tf)
            cross = self.last_cross.get(d)
            if _utc(now) - _utc(self.last_exit_at) < COOLDOWN or cross is None or _utc(cross) <= _utc(self.last_exit_at):
                self._bump("cooldown")
                return
        self.pending_entry = (d, "normal", b.last_minute)

    def _enter(self, m: M1, side: int, kind: str, signal_minute: int | None = None) -> None:
        had = self.pos
        super()._enter(m, side, kind, signal_minute)
        p = self.pos
        if p is not None and p is not had and side < 0 and self.cfg.short_regime == "tactical" and not self.bear:
            p.bull_short = p.trade.bull_short = True

    def step(self, m: M1, is_last_of_stream: bool) -> None:
        cfg = self.cfg
        p = self.pos
        # a bull-regime tactical short below Stage 3 must not carry into a new day (its day's last bar was missing)
        if p is not None and p.bull_short and p.stage < 3 and self._prev is not None and m.day != self._prev.day:
            self.pending_exit = self.pending_exit or "gap_eod_bull_short"
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
            elif kind == "normal" and side < 0 and not self._short_ok():
                self._bump("stale_regime")  # spec §4.2: dropped like stale_signal, counted separately
            elif self.pos is None:
                self._enter(m, side, kind, sig)
        # (3) intrabar: stage floor / hard stop, then close-based MFE and stage promotion
        p = self.pos
        if p is not None:
            tr = p.trade
            s = tr.side
            if p.stage == 0:
                level, reason = tr.entry_fill * (1 - s * cfg.hard_stop), "hard_stop"
            else:
                lock = (cfg.lock_1, cfg.lock_2, cfg.lock_3)[p.stage - 1]
                level, reason = tr.entry_fill * (1 + s * lock), f"lock_{p.stage}"
            hit = m.l <= level if s > 0 else m.h >= level
            if hit:
                raw = min(level, m.o) if s > 0 else max(level, m.o)
                self._exit(m, m.minute, raw, reason, m.at())
            else:
                r = s * (m.c - tr.entry_fill) / tr.entry_fill
                p.mfe = max(p.mfe, r)
                for k, latch in ((3, cfg.latch_3), (2, cfg.latch_2), (1, cfg.latch_1)):
                    if p.stage < k and p.mfe >= latch:
                        p.stage = k
                        break
                p.swing = p.stage >= 1
        # (4) day-end exit of a bull-regime tactical short below Stage 3; everything at the end of the data
        p = self.pos
        if p is not None:
            end_at = m.at(m.minute + 1)
            if is_last_of_stream:
                self._exit(m, m.minute, m.c, "end_of_data", end_at)
            elif p.bull_short and p.stage < 3 and is_last_bar_of_day(m):
                self._exit(m, m.minute, m.c, "eod_bull_short", end_at)
        # (5) buckets completed by this bar
        b = self.agg30.add(m)
        if b is not None:
            self._on_30m(b)
        b = self.aggs.add(m)
        if b is not None:
            self._on_short(b)
        self._prev = m
