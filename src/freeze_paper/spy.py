"""SPY close evidence for the FREEZE Paper benchmark (shared by the evaluator and receipt).

Evidence (issue #165 PR 3 review): Toss candles carry no adjusted flag; the provider stores
every 1d and 1m candle with adjusted=True, and PROBE_REPORT_TOSS_CAPABILITY_V1 §4 shows both
series are split back-adjusted while dividend adjustment is unverified (R1).  No unadjusted
official daily close is available, so the benchmark uses the 15:59/12:59 ET 1m bar close.
Back-adjustment is applied retroactively, so a bar is "as traded" only if the store's first
write (candle_history.created_at, set once on insert by src/history/store.py _upsert_row)
happened between that day's regular close and 00:00 America/New_York of the next calendar
day, and the row was never overwritten (updated_at == created_at).  The rule is checked per
close, never assumed.  Reads are read-only.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.history.exchange_calendar import is_trading_day
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET
from src.premarket.schedule import build_premarket_schedule, parse_iso

SPY_CLOSE_SOURCE = ("Toss REGULAR 1m bar starting one minute before the scheduled close "
                    "(15:59 ET, 12:59 ET on early closes), provider series split-adjusted, "
                    "dividend adjustment unverified, used only when first written after that "
                    "day's close and before 00:00 America/New_York of the next day and never "
                    "overwritten; not the official closing auction and not an official "
                    "unadjusted daily close")
DISCONTINUITY_BOUNDS = (0.75, 1.25)


def spy_close_evidence(db: Path, day: date) -> dict[str, Any] | None:
    """The 15:59/12:59 ET SPY 1m close via the OOS-guarded loader, plus its store capture
    timestamps (read-only metadata query on the same primary key)."""
    if not Path(db).is_file():
        return None
    close = parse_iso(build_premarket_schedule(day).market_close_utc)
    last_start = close - timedelta(minutes=1)
    bars = research_load(str(db), "SPY", "1m", start=last_start, end=close,
                         purpose="performance", track=TRACK_INTRADAY, market="US")
    bars = [b for b in bars if b.ts_utc == last_start]
    if len(bars) != 1:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT created_at, updated_at FROM candle_history "
            "WHERE symbol='SPY' AND interval='1m' AND ts_utc=?",
            (bars[0].ts_utc.isoformat(),)).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {"close": float(bars[0].close), "bar_ts_utc": bars[0].ts_utc.isoformat(),
            "captured_at": row[0], "updated_at": row[1]}


def capture_defect(day: str, evidence: Any) -> str | None:
    """None when the close was first written after that day's regular close (early closes
    included) and before 00:00 America/New_York of the next day, and never overwritten."""
    if not isinstance(evidence, Mapping) or not positive(evidence.get("close")):
        return f"MISSING_SPY_CLOSE:{day}"
    trading_day = date.fromisoformat(day)
    session_close = parse_iso(build_premarket_schedule(trading_day).market_close_utc)
    midnight = datetime.combine(trading_day + timedelta(days=1), time(0, 0), tzinfo=ET)
    try:
        captured = datetime.fromisoformat(str(evidence["captured_at"]))
    except (KeyError, ValueError):
        return f"SPY_CLOSE_NOT_CAPTURED_SAME_DAY:{day}"
    if captured.tzinfo is None or not session_close <= captured < midnight:
        return f"SPY_CLOSE_NOT_CAPTURED_SAME_DAY:{day}"
    if evidence.get("updated_at") != evidence["captured_at"]:
        return f"SPY_CLOSE_OVERWRITTEN:{day}"
    return None


def discontinuity_defects(start_day: str, end_day: str, closes: Mapping[str, Any]) -> list[str]:
    """Adjacent available closes in [start_day, end_day] must stay within 0.75..1.25."""
    series = [(day, float(ev["close"])) for day, ev in sorted(closes.items())
              if start_day <= day <= end_day and isinstance(ev, Mapping)
              and positive(ev.get("close"))]
    lo, hi = DISCONTINUITY_BOUNDS
    return [f"SPY_PRICE_DISCONTINUITY:{day}"
            for (_, prev), (day, value) in pairwise(series)
            if not lo <= value / prev <= hi]


def shift_trading_days(day: date, count: int) -> date:
    step = 1 if count > 0 else -1
    moved = day
    for _ in range(abs(count)):
        moved += timedelta(days=step)
        while not is_trading_day(moved):
            moved += timedelta(days=step)
    return moved


def positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
