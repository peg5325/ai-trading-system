"""Fail-closed NYSE stage validation before any provider network call."""
from __future__ import annotations

from datetime import date, datetime

from src.premarket.schedule import (
    PremarketSchedule,
    build_premarket_schedule,
    iso_z,
    parse_iso,
)

STAGES = ("PRIMARY", "VERIFICATION", "FREEZE")


def validate_collection_window(
    trading_day: date,
    stage: str,
    *,
    as_of: str | None,
    now: datetime,
    dry_run: bool,
    primary_offset_minutes: int = 90,
    verification_offset_minutes: int = 30,
    freeze_offset_minutes: int = 20,
) -> tuple[PremarketSchedule, str]:
    normalized_stage = stage.strip().upper()
    if normalized_stage not in STAGES:
        raise ValueError(f"unsupported collection stage: {stage}")
    schedule = build_premarket_schedule(
        trading_day,
        primary_offset_minutes=primary_offset_minutes,
        verification_offset_minutes=verification_offset_minutes,
        freeze_offset_minutes=freeze_offset_minutes,
    )
    expected = {
        "PRIMARY": schedule.primary_run_utc,
        "VERIFICATION": schedule.verification_utc,
        "FREEZE": schedule.freeze_utc,
    }[normalized_stage]
    if as_of is not None and iso_z(parse_iso(as_of)) != expected:
        raise ValueError(f"as_of must equal the {normalized_stage} cutoff: {expected}")
    if dry_run:
        return schedule, expected
    now_utc = parse_iso(iso_z(now))
    start = parse_iso(expected)
    end = parse_iso({
        "PRIMARY": schedule.verification_utc,
        "VERIFICATION": schedule.freeze_utc,
        "FREEZE": schedule.market_open_utc,
    }[normalized_stage])
    if not start <= now_utc < end:
        raise ValueError(
            f"current time is outside the {normalized_stage} collection window "
            f"[{iso_z(start)}, {iso_z(end)})"
        )
    return schedule, expected
