"""NYSE-relative premarket schedule (pure, deterministic, and DST-safe)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.history.exchange_calendar import is_trading_day, regular_close_minute

ET = ZoneInfo("America/New_York")


def iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timezone offset required: {value}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PremarketSchedule:
    trading_day: str
    market_open_utc: str
    primary_run_utc: str
    verification_utc: str
    freeze_utc: str
    market_close_utc: str
    timezone: str = "America/New_York"
    primary_offset_minutes: int = 90
    verification_offset_minutes: int = 30
    freeze_offset_minutes: int = 20

    def to_dict(self) -> dict:
        return asdict(self)


def build_premarket_schedule(
    trading_day: date,
    *,
    primary_offset_minutes: int = 90,
    verification_offset_minutes: int = 30,
    freeze_offset_minutes: int = 20,
) -> PremarketSchedule:
    """Build the run/freeze schedule relative to the NYSE open.

    The tracked exchange-calendar artifact is the authority for whether the
    date trades and for the regular close (including early closes).  Fixed KST
    clock times are intentionally not accepted because they break across DST.
    """
    offsets = (primary_offset_minutes, verification_offset_minutes, freeze_offset_minutes)
    if any(value <= 0 for value in offsets):
        raise ValueError("schedule offsets must be positive")
    if not (primary_offset_minutes > verification_offset_minutes > freeze_offset_minutes):
        raise ValueError("expected primary > verification > freeze offsets")
    if not is_trading_day(trading_day):
        raise ValueError(f"not an NYSE trading day: {trading_day.isoformat()}")

    market_open = datetime.combine(trading_day, time(9, 30), tzinfo=ET)
    close_minute = regular_close_minute(trading_day)
    if close_minute is None:  # guarded by is_trading_day; retained fail-closed.
        raise ValueError(f"NYSE close unavailable: {trading_day.isoformat()}")
    market_close = datetime.combine(
        trading_day,
        time(close_minute // 60, close_minute % 60),
        tzinfo=ET,
    )
    return PremarketSchedule(
        trading_day=trading_day.isoformat(),
        market_open_utc=iso_z(market_open),
        primary_run_utc=iso_z(market_open - timedelta(minutes=primary_offset_minutes)),
        verification_utc=iso_z(market_open - timedelta(minutes=verification_offset_minutes)),
        freeze_utc=iso_z(market_open - timedelta(minutes=freeze_offset_minutes)),
        market_close_utc=iso_z(market_close),
        primary_offset_minutes=primary_offset_minutes,
        verification_offset_minutes=verification_offset_minutes,
        freeze_offset_minutes=freeze_offset_minutes,
    )
