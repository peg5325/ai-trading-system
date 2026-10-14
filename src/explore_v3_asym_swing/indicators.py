"""Incremental indicators and clock-bucket aggregation on the continuous 1m stream (spec §3, §4)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime

from src.daytrade_paper.core import Bar
from src.history.exchange_calendar import regular_close_minute
from src.models import ET

OPEN_MIN = 570


@dataclass(frozen=True)
class M1:
    """One valid regular-session 1m bar on the continuous stream. `minute` is the bar START minute of day (ET)."""

    day: str
    minute: int
    o: float
    h: float
    l: float
    c: float
    v: float

    def at(self, minute: int | None = None) -> datetime:
        d = Date.fromisoformat(self.day)
        m = self.minute if minute is None else minute
        return datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)


def valid(b: Bar) -> bool:
    return (min(b.open, b.high, b.low, b.close) > 0 and b.low <= min(b.open, b.close)
            and max(b.open, b.close) <= b.high and b.volume >= 0)


def build_stream(regular: dict[str, list[Bar]]) -> list[M1]:
    """Spec §3: valid regular bars (start minute 570 .. calendar close - 1) of every day, in time order, no reset."""
    out: list[M1] = []
    for day in sorted(regular):
        close = regular_close_minute(Date.fromisoformat(day))
        if close is None:
            continue
        for b in sorted(regular[day], key=lambda x: x.t):
            if OPEN_MIN <= b.t < close and valid(b):
                out.append(M1(day, b.t, b.open, b.high, b.low, b.close, b.volume))
    return out


def last_minute_of_day(day: str) -> int:
    return regular_close_minute(Date.fromisoformat(day)) - 1


@dataclass
class Bucket:
    day: str
    index: int  # (minute - 570) // tf
    o: float
    h: float
    l: float
    c: float
    last_minute: int  # last 1m bar START minute seen in the bucket

    def end(self, tf: int) -> datetime:
        """Clock end of the bucket (start of its following minute)."""
        d = Date.fromisoformat(self.day)
        m = OPEN_MIN + (self.index + 1) * tf
        return datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=ET)


class Aggregator:
    """Clock buckets of `tf` minutes from 09:30 within a day (spec §3). A bucket completes when its last clock minute's
    bar has been processed (`add`), or, if that minute is missing, when the first bar of a later bucket arrives
    (`close_stale`, called before that bar is used)."""

    def __init__(self, tf: int) -> None:
        self.tf = tf
        self.cur: Bucket | None = None

    def key(self, m: M1) -> tuple[str, int]:
        return m.day, (m.minute - OPEN_MIN) // self.tf

    def close_stale(self, m: M1) -> Bucket | None:
        if self.cur is not None and (self.cur.day, self.cur.index) != self.key(m):
            b, self.cur = self.cur, None
            return b
        return None

    def add(self, m: M1) -> Bucket | None:
        day, idx = self.key(m)
        if self.cur is None:
            self.cur = Bucket(day, idx, m.o, m.h, m.l, m.c, m.minute)
        else:
            c = self.cur
            c.h, c.l, c.c, c.last_minute = max(c.h, m.h), min(c.l, m.l), m.c, m.minute
        if (m.minute - OPEN_MIN) % self.tf == self.tf - 1:
            b, self.cur = self.cur, None
            return b
        return None


class EMA:
    """Seeded with the simple mean of the first n values, then alpha = 2 / (n + 1)."""

    def __init__(self, n: int) -> None:
        self.n, self.seed, self.value = n, [], None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.value = sum(self.seed) / self.n
        else:
            self.value += 2.0 / (self.n + 1) * (x - self.value)
        return self.value


class WilderATR:
    """True range against the previous bar's close (continuous); first value = mean of the first n TRs, then
    (atr * (n - 1) + tr) / n. The very first bar's TR is high - low."""

    def __init__(self, n: int) -> None:
        self.n, self.seed, self.value, self.prev_close = n, [], None, None

    def update(self, h: float, l: float, c: float) -> float | None:
        pc = self.prev_close
        tr = h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        if self.value is None:
            self.seed.append(tr)
            if len(self.seed) == self.n:
                self.value = sum(self.seed) / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        self.prev_close = c
        return self.value


class SuperTrend:
    """Spec §4 SuperTrend(p, m) with its own Wilder ATR(p). `update` returns the direction (+1 / -1) or None until
    the ATR is seeded."""

    def __init__(self, period: int, mult: float) -> None:
        self.atr = WilderATR(period)
        self.mult = mult
        self.upper = self.lower = None
        self.dir: int | None = None
        self.prev_close: float | None = None

    def update(self, h: float, l: float, c: float) -> int | None:
        atr = self.atr.update(h, l, c)
        pc = self.prev_close
        self.prev_close = c
        if atr is None:
            return None
        hl2 = (h + l) / 2.0
        bu, bl = hl2 + self.mult * atr, hl2 - self.mult * atr
        if self.upper is None:
            self.upper, self.lower = bu, bl
            self.dir = 1 if c > (bu + bl) / 2.0 else -1
            return self.dir
        pu, plo = self.upper, self.lower
        self.upper = bu if (bu < pu or pc > pu) else pu
        self.lower = bl if (bl > plo or pc < plo) else plo
        if self.dir == 1 and c < self.lower:
            self.dir = -1
        elif self.dir == -1 and c > self.upper:
            self.dir = 1
        return self.dir


def is_last_bar_of_day(m: M1) -> bool:
    return m.minute == last_minute_of_day(m.day)
