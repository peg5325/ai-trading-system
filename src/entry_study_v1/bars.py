"""DB candle -> Bar conversion and row hashing (PREREG §2 bar labels, §5.1 data manifest).

The DB `ts_exchange` is the bar END minute; `store._bar` converts it to the bar start (label - 1) and classes the
session by the exchange calendar. This module only wraps that function; it does not re-implement it.
"""
from __future__ import annotations

from src.daytrade_paper.core import Bar
from src.daytrade_paper.store import _bar
from src.models import Candle

# Fields of each DB row covered by the content hash (one line per row, ts_utc order) - same set as v1.1.
ROW_HASH_FIELDS = ("ts_utc", "ts_exchange", "session", "open", "high", "low", "close", "volume", "market",
                   "adjusted")


class StudyRefused(Exception):
    """Base: the run must not proceed."""


class DevBoundError(StudyRefused):
    """A development-code load reached 2026-01-21 00:00 ET or later."""


class DataMismatch(StudyRefused):
    """DB rows, gate, mask or manifest differ from the fixed ones."""


class InvalidRun(StudyRefused):
    """A scored day lacked a bar or had a bad value: the whole run is void (PREREG §5.1, §9)."""


def candle_bar(c: Candle) -> tuple[str, str, Bar] | None:
    """`store._bar` applied to a Candle (the DB stores `Candle.ts_exchange.isoformat()`).
    Returns (day, session, Bar with t = label - 1) or None for a label before 00:01."""
    if c.ts_exchange.second or c.ts_exchange.microsecond:
        raise StudyRefused(f"{c.symbol} bar at {c.ts_exchange.isoformat()} is not on the minute grid")
    return _bar((c.ts_exchange.isoformat(), c.session, c.open, c.high, c.low, c.close, c.volume))


def label_minute(c: Candle) -> int:
    """The DB label (END minute) as ET minute of day."""
    return c.ts_exchange.hour * 60 + c.ts_exchange.minute


def row_line(c: Candle) -> bytes:
    return (f"{c.ts_utc.isoformat()}|{c.ts_exchange.isoformat()}|{c.session}|{c.open!r}|{c.high!r}|"
            f"{c.low!r}|{c.close!r}|{c.volume!r}|{c.market}|{int(c.adjusted)}\n").encode()
