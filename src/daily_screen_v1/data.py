"""Guarded data access (PREREG §5.1): prices only via `load_daily(purpose="performance")` under a per-stage code
cut-off that is stricter than the loader; calendar and dividends via `load_aux` (dates/amounts, no cut-off);
HOLDOUT date counts via `purpose="coverage"` (date column only).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from src.longhist.loader import _as_date, load_aux, load_daily


class StudyRefused(Exception):
    """The run must not proceed (cut-off, order, SHA, approval or write-once violation)."""


# Code cut-offs on price requests (§5.1): main DEV, GEX DEV, HOLDOUT (both families).
CUTOFFS = {"main_dev": date(2015, 12, 31), "gex_dev": date(2018, 12, 31), "holdout": date(2025, 7, 18)}
HOLDOUT_START = {"main": date(2016, 1, 1), "gex": date(2019, 1, 1)}
GEX_DEV_START = date(2011, 5, 2)
CALENDAR_FILE = "norm/calendar/nyse_sessions.csv"
NO_DIVIDENDS = ("^SOX",)  # ^SOX is a price index: div = 0 (§4.1)


def check_window(stage: str, end) -> date:
    """The inclusive price-request end must be explicit and on/before the stage cut-off."""
    if stage not in CUTOFFS:
        raise StudyRefused(f"unknown stage {stage!r}; known: {sorted(CUTOFFS)}")
    try:
        e = _as_date(end)
    except (TypeError, ValueError) as exc:
        raise StudyRefused(f"unreadable end {end!r}") from exc
    if e is None or e > CUTOFFS[stage]:
        raise StudyRefused(f"{stage} price request ending {e} is after the code cut-off {CUTOFFS[stage]}")
    return e


def load_prices(symbol: str, stage: str, end=None, *, root: Path | None = None,
                expected_manifest_sha256: str | None = None) -> pd.DataFrame:
    """All rows of `symbol` from its first row through `end` (default: the stage cut-off)."""
    e = check_window(stage, CUTOFFS[stage] if end is None else end)
    return load_daily(symbol, None, e, purpose="performance", root=root,
                      expected_manifest_sha256=expected_manifest_sha256)


def load_calendar(*, root: Path | None = None, expected_manifest_sha256: str | None = None) -> list[str]:
    """NYSE session dates (known in advance; no cut-off applies)."""
    return list(load_aux(CALENDAR_FILE, root=root, expected_manifest_sha256=expected_manifest_sha256)["date"])


def load_dividends(symbol: str, *, root: Path | None = None,
                   expected_manifest_sha256: str | None = None) -> dict[str, float]:
    """{ex-date: summed cash amount}; empty for ^SOX."""
    if symbol in NO_DIVIDENDS:
        return {}
    rel = f"norm/yahoo/{symbol.replace('^', 'IDX_')}_dividends.csv"
    df = load_aux(rel, root=root, expected_manifest_sha256=expected_manifest_sha256)
    return {str(d): float(a) for d, a in df.groupby("date")["amount"].sum().items()}


def holdout_dates(symbol: str, family: str, *, root: Path | None = None,
                  expected_manifest_sha256: str | None = None) -> list[str]:
    """Dates (no prices) of `symbol` rows in the family's HOLDOUT window, for precount counts (§6)."""
    df = load_daily(symbol, purpose="coverage", root=root, expected_manifest_sha256=expected_manifest_sha256)
    lo, hi = HOLDOUT_START[family].isoformat(), CUTOFFS["holdout"].isoformat()
    return [d for d in df["date"] if lo <= d <= hi]
