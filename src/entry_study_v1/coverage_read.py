"""Coverage-only 1m reads for the entry study (gate, manifest and row hashes; no returns computed here).

Kept in its own module so the research_load boundary test can allow purpose="coverage" for exactly this
file (tests/test_orb_loading.py `_ALLOWED_COVERAGE_STRING`).
"""
from __future__ import annotations

from datetime import datetime

from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import Candle


def coverage_rows(db, symbol: str, start: datetime, end: datetime) -> list[Candle]:
    return research_load(str(db), symbol, "1m", start=start, end=end, purpose="coverage", track=TRACK_INTRADAY)
