"""Read-only provider capability probe for the Gap+RVOL D9 missing window."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = "gap-rvol-d9-provider-probe/v1"
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

PROBE_DAYS = (
    date(2024, 7, 23),  # control: last visible day for some symbols
    date(2024, 7, 24),
    date(2024, 7, 25),
    date(2024, 7, 26),
    date(2024, 7, 29),
    date(2024, 7, 30),
    date(2024, 7, 31),
    date(2024, 8, 1),
    date(2024, 8, 2),
    date(2024, 8, 5),  # control: source resumes, snapshot is missing
    date(2024, 8, 6),
)


def request_before(day: date) -> str:
    """Return a stable instant late enough to include the target ET session."""
    moment = datetime.combine(day, time(23, 59), timezone.utc).astimezone(KST)
    return moment.isoformat(timespec="milliseconds")


def retry_after_seconds(value: str | None, *, now: datetime) -> float | None:
    """Parse Retry-After seconds or HTTP-date; return None for invalid input."""
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max((moment - now.astimezone(timezone.utc)).total_seconds(), 0.0)


def summarize_page(
    *,
    symbol: str,
    target_day: date,
    result: Mapping[str, Any],
) -> dict[str, object]:
    """Summarize availability without persisting price or volume values."""
    candles = result.get("candles", []) or []
    if not isinstance(candles, Sequence) or isinstance(candles, (str, bytes)):
        raise TypeError("provider candles must be an array")
    timestamps = [str(row["timestamp"]) for row in candles]
    returned_days = sorted({
        datetime.fromisoformat(timestamp).astimezone(ET).date().isoformat()
        for timestamp in timestamps
    })
    target = target_day.isoformat()
    return {
        "symbol": symbol,
        "target_day": target,
        "before": request_before(target_day),
        "row_count": len(candles),
        "returned_et_days": returned_days,
        "has_target_day": target in returned_days,
        "oldest_timestamp": min(timestamps) if timestamps else None,
        "newest_timestamp": max(timestamps) if timestamps else None,
        "next_before_present": bool(result.get("nextBefore")),
    }


def build_report(
    *,
    symbols: Sequence[str],
    observations: Sequence[Mapping[str, object]],
    generated_at_utc: str,
    http_429: int,
) -> dict[str, object]:
    expected = {(symbol, day.isoformat()) for symbol in symbols for day in PROBE_DAYS}
    actual = {
        (str(item["symbol"]), str(item["target_day"])) for item in observations
    }
    if actual != expected or len(observations) != len(expected):
        raise ValueError("provider probe observations do not cover the frozen matrix")
    by_day = {}
    for day in PROBE_DAYS:
        target = day.isoformat()
        available = sorted(
            str(item["symbol"])
            for item in observations
            if item["target_day"] == target and item["has_target_day"] is True
        )
        by_day[target] = {
            "available_count": len(available),
            "missing_count": len(symbols) - len(available),
            "available_symbols": available,
        }
    return {
        "schema": SCHEMA,
        "generated_at_utc": generated_at_utc,
        "scope": {
            "read_only": True,
            "database_written": False,
            "prices_persisted": False,
            "blind_oos_opened": False,
            "performance_computed": False,
        },
        "provider": "toss",
        "interval": "1m",
        "page_size": 200,
        "symbols": list(symbols),
        "probe_days": [day.isoformat() for day in PROBE_DAYS],
        "http_429": http_429,
        "by_day": by_day,
        "observations": sorted(
            (dict(item) for item in observations),
            key=lambda item: (str(item["target_day"]), str(item["symbol"])),
        ),
    }


def report_bytes(report: Mapping[str, object]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
