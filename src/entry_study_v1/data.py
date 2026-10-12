"""Historical reads for the entry study (PREREG §5.1, §11).

Prices come only through `src.history.oos_guard.research_load` (purpose="performance" wherever returns are
computed, "coverage" only for gate / manifest / row hashes), in month chunks. Development code additionally
refuses any request that reaches 2026-01-21 00:00 ET (the sealed block is also blocked by the guard, but the
period after the seal is not, so this code enforces the upper bound itself).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta

from src.daytrade_paper.core import Bar
from src.entry_study_v1.bars import (
    DevBoundError,
    StudyRefused,
    candle_bar,
    label_minute,
    row_line,
)
from src.history.oos_guard import OOSExecutionGrant, research_load
from src.models import ET

DEV_CUTOFF = datetime(2026, 1, 21, tzinfo=ET)


@dataclass(frozen=True)
class Segment:
    """data_*: rows read (lookbacks included). score_*: scored-date window, both ends inclusive."""

    name: str
    data_start: Date
    data_end: Date
    score_start: Date
    score_end: Date

    def bounds(self) -> tuple[datetime, datetime]:
        return et_midnight(self.data_start), et_midnight(self.data_end + timedelta(days=1))


# PREREG §5.1: data segments A/B (v1.1 SEGMENTS) and the mask scoring windows.
DEV_SEGMENTS = (
    Segment("A", Date(2023, 4, 11), Date(2024, 7, 24), Date(2023, 6, 22), Date(2024, 7, 24)),
    Segment("B", Date(2024, 8, 5), Date(2026, 1, 20), Date(2024, 10, 15), Date(2026, 1, 20)),
)
# Lookbacks for the OOS days come from segment B (continuous with 2026-01-21); the OOS rows start at OOS_START.
OOS_START = Date(2026, 1, 21)
OOS_END = Date(2026, 10, 9)
OOS_EXCLUDED = tuple(Date(2026, 10, d) for d in (5, 6, 7, 8, 9))  # Paper week already seen (PREREG §5.3, §8)
OOS_SEGMENT = Segment("OOS", Date(2024, 8, 5), OOS_END, OOS_START, OOS_END)


def et_midnight(d: Date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def assert_dev_window(lo: datetime, hi: datetime) -> None:
    """Development loads must lie strictly before 2026-01-21 00:00 ET (`hi` is exclusive)."""
    if lo >= DEV_CUTOFF or hi > DEV_CUTOFF:
        raise DevBoundError(f"development load [{lo}, {hi}) reaches {DEV_CUTOFF}; refused")


@dataclass
class SymbolData:
    symbol: str
    regular: dict[str, list[Bar]] = field(default_factory=dict)  # ET day -> REGULAR bars, ts_utc order
    pre: dict[str, list[Bar]] = field(default_factory=dict)  # ET day -> PRE bars
    labels: dict[str, list[int]] = field(default_factory=dict)  # ET day -> DB label minutes of REGULAR bars
    row_count: int = 0
    row_sha256: str = ""


def merge(first: SymbolData, second: SymbolData) -> SymbolData:
    """Concatenate two reads of one symbol with disjoint dates (hashes are kept by the callers)."""
    if first.symbol != second.symbol:
        raise StudyRefused("cannot merge different symbols")
    out = SymbolData(first.symbol)
    for src in (first, second):
        for name in ("regular", "pre", "labels"):
            dst = getattr(out, name)
            for day, v in getattr(src, name).items():
                if day in dst:
                    raise StudyRefused(f"{first.symbol}: {day} present in both parts of a merge")
                dst[day] = list(v)
    out.row_count = first.row_count + second.row_count
    return out


def month_chunks(d0: Date, d1: Date):
    """[lo, hi) ET-midnight month chunks covering the inclusive dates d0..d1."""
    lo = d0
    while lo <= d1:
        nxt = Date(lo.year + (lo.month == 12), lo.month % 12 + 1, 1)
        hi = min(nxt, d1 + timedelta(days=1))
        yield et_midnight(lo), et_midnight(hi)
        lo = hi


def load_symbol(db, symbol: str, d0: Date, d1: Date, *, purpose: str, dev: bool = True,
                strategy_id: str | None = None,
                grant: OOSExecutionGrant | None = None) -> SymbolData:
    """One symbol's 1m bars for the inclusive dates d0..d1, via research_load only.

    dev=True enforces the development upper bound before any read. A bar dated outside d0..d1 is refused.
    """
    if purpose not in ("performance", "coverage"):
        raise ValueError(f"purpose must be 'performance' or 'coverage', got {purpose!r}")
    out = SymbolData(symbol)
    h = hashlib.sha256()
    first, last = d0.isoformat(), d1.isoformat()
    for lo, hi in month_chunks(d0, d1):
        if dev:
            assert_dev_window(lo, hi)
        kwargs = {"strategy_id": strategy_id, "execution_grant": grant} if grant is not None else {}
        for c in research_load(str(db), symbol, "1m", start=lo, end=hi, purpose=purpose, **kwargs):
            out.row_count += 1
            h.update(row_line(c))
            parsed = candle_bar(c)
            if parsed is None:
                continue
            day, session, bar = parsed
            if not first <= day <= last:
                raise StudyRefused(f"{symbol} bar dated {day} read in chunk [{lo}, {hi}) outside {first}..{last}")
            if session == "REGULAR":
                out.regular.setdefault(day, []).append(bar)
                out.labels.setdefault(day, []).append(label_minute(c))
            elif session == "PRE":
                out.pre.setdefault(day, []).append(bar)
    out.row_sha256 = h.hexdigest()
    return out
