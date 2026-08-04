"""Plan and probe only the historical dependencies behind Gap+RVOL D9.

The planner is read-only.  It turns the tracked D9 exclusion ledger into the
smallest *exhaustive missing-input set*: prior trading days with no usable
regular close and prior 20-day RVOL windows whose first 15 minutes are
incomplete.  It never treats an event-day repair as a dependency repair.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.history.exchange_calendar import expected_regular_minutes
from src.models import Candle
from src.research.gap_rvol.contract import rvol_spec
from src.research.gap_rvol.features import window_sum_for_lookback
from src.research.gap_rvol.loading import regular_1m
from src.research.gap_rvol.provider_probe import summarize_page
from src.research.gap_rvol.windows import (
    development_data_days,
    minute_of_day,
    previous_trading_day,
    regular_close_minute_checked,
)

PLAN_SCHEMA = "gap-rvol-d9-dependency-plan/v1"
PROBE_SCHEMA = "gap-rvol-d9-dependency-probe/v1"
FROZEN_PLAN_FILE_SHA256 = (
    "7a27ee176f910e943c955ac2db034dfebf47d7f2ffa5583c3edf244d576014c0"
)
FROZEN_TARGET_COUNT = 458
FROZEN_ROLE_COUNTS = {
    "prior_regular_close": 193,
    "rvol_15m_lookback": 448,
}


@dataclass(frozen=True, order=True)
class DependencyTarget:
    day: date
    symbol: str
    roles: tuple[str, ...]
    required_by: tuple[tuple[str, str], ...]
    regular_minutes: int
    expected_regular_minutes: int
    has_usable_regular_close: bool
    has_complete_rvol_window: bool


def sha256_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _usable_regular_close(candles: tuple[Candle, ...], day: date) -> bool:
    close = regular_close_minute_checked(day)
    return any(minute_of_day(candle) < close for candle in regular_1m(candles))


def _rvol_window_complete(
    candles: tuple[Candle, ...], *, day: date, minutes: int
) -> bool:
    return window_sum_for_lookback(
        regular_1m(candles), day=day, minutes=minutes
    ) is not None


def build_dependency_targets(
    *,
    audit: Mapping[str, Any],
    contract: Mapping[str, Any],
    loader: Callable[[str, date], tuple[Candle, ...]],
) -> tuple[DependencyTarget, ...]:
    """Derive missing prior-close and RVOL inputs from frozen evidence."""
    if audit["scope"]["blind_oos_opened"] is not False:
        raise ValueError("dependency planning requires the tracked OOS-closed audit")
    if audit["exclusion_audit"]["all_specs_identical"] is not True:
        raise ValueError("dependency planning requires identical exclusions across specs")
    days = development_data_days(contract)
    day_index = {day: index for index, day in enumerate(days)}
    rvol = rvol_spec(contract)
    if rvol.lookback_days != 20 or rvol.min_valid_days != 15:
        raise ValueError("unexpected frozen RVOL dependency contract")

    roles: dict[tuple[str, date], set[str]] = defaultdict(set)
    required_by: dict[tuple[str, date], set[tuple[str, str]]] = defaultdict(set)
    observations: dict[tuple[str, date], tuple[int, int, bool, bool]] = {}
    cache: dict[tuple[str, date], tuple[Candle, ...]] = {}

    def candles(symbol: str, day: date) -> tuple[Candle, ...]:
        key = (symbol, day)
        if key not in cache:
            cache[key] = loader(symbol, day)
        return cache[key]

    def observe(symbol: str, day: date) -> tuple[int, int, bool, bool]:
        key = (symbol, day)
        if key not in observations:
            day_candles = candles(symbol, day)
            regular = regular_1m(day_candles)
            observations[key] = (
                len({minute_of_day(candle) for candle in regular}),
                expected_regular_minutes(day),
                _usable_regular_close(day_candles, day),
                _rvol_window_complete(
                    day_candles, day=day, minutes=rvol.minutes
                ),
            )
        return observations[key]

    for event in audit["exclusion_audit"]["events"]:
        symbol = str(event["symbol"])
        event_day = date.fromisoformat(str(event["day"]))
        reasons = set(map(str, event["reasons"]))
        if event_day not in day_index:
            raise ValueError(f"D9 event is outside development data days: {event_day}")

        if "no_previous_regular_close" in reasons:
            prior_day = previous_trading_day(event_day)
            _count, _expected, has_close, _has_rvol = observe(symbol, prior_day)
            if not has_close:
                key = (symbol, prior_day)
                roles[key].add("prior_regular_close")
                required_by[key].add((event_day.isoformat(), "no_previous_regular_close"))

        if "insufficient_rvol_lookback" in reasons:
            index = day_index[event_day]
            lookback = days[max(0, index - rvol.lookback_days):index]
            if len(lookback) != rvol.lookback_days:
                raise ValueError("evaluation event lacks the frozen 20-day warmup")
            valid = 0
            missing: list[date] = []
            for dependency_day in lookback:
                _count, _expected, _has_close, has_rvol = observe(
                    symbol, dependency_day
                )
                if has_rvol:
                    valid += 1
                else:
                    missing.append(dependency_day)
            if valid >= rvol.min_valid_days:
                raise ValueError(
                    "insufficient_rvol_lookback is not explained by missing windows"
                )
            for dependency_day in missing:
                key = (symbol, dependency_day)
                roles[key].add("rvol_15m_lookback")
                required_by[key].add(
                    (event_day.isoformat(), "insufficient_rvol_lookback")
                )

    targets = []
    for (symbol, day), target_roles in roles.items():
        count, expected, has_close, has_rvol = observe(symbol, day)
        if "prior_regular_close" in target_roles and has_close:
            raise ValueError("prior-close repair target already has a usable close")
        if "rvol_15m_lookback" in target_roles and has_rvol:
            raise ValueError("RVOL repair target already has a complete 15m window")
        targets.append(DependencyTarget(
            day=day,
            symbol=symbol,
            roles=tuple(sorted(target_roles)),
            required_by=tuple(sorted(required_by[(symbol, day)])),
            regular_minutes=count,
            expected_regular_minutes=expected,
            has_usable_regular_close=has_close,
            has_complete_rvol_window=has_rvol,
        ))
    return tuple(sorted(targets))


def build_dependency_plan(
    *,
    targets: Sequence[DependencyTarget],
    audit_sha256: str,
    prereg_sha256: str,
    snapshot_identity: Mapping[str, str],
    generated_at_utc: str,
) -> dict[str, Any]:
    role_counts = Counter(role for target in targets for role in target.roles)
    years = Counter(target.day.year for target in targets)
    return {
        "schema": PLAN_SCHEMA,
        "generated_at_utc": generated_at_utc,
        "scope": {
            "read_only": True,
            "development_only": True,
            "blind_oos_opened": False,
            "performance_computed": False,
            "forward_returns_computed": False,
            "canonical_db_written": False,
            "frozen_snapshot_written": False,
        },
        "selection_rule": (
            "tracked D9 dependency reason AND frozen snapshot missing the exact "
            "prior-close or prior-20-day RVOL input"
        ),
        "input_identity": {
            "audit_file_sha256": audit_sha256,
            "prereg_file_sha256": prereg_sha256,
            "frozen_snapshot": dict(sorted(snapshot_identity.items())),
        },
        "target_count": len(targets),
        "role_counts": {key: role_counts[key] for key in sorted(role_counts)},
        "target_year_counts": {str(key): years[key] for key in sorted(years)},
        "target_min_day": min(target.day for target in targets).isoformat(),
        "target_max_day": max(target.day for target in targets).isoformat(),
        "symbols": sorted({target.symbol for target in targets}),
        "targets": [
            {
                "day": target.day.isoformat(),
                "symbol": target.symbol,
                "roles": list(target.roles),
                "required_by": [
                    {"day": event_day, "reason": reason}
                    for event_day, reason in target.required_by
                ],
                "snapshot_regular_minutes": target.regular_minutes,
                "expected_regular_minutes": target.expected_regular_minutes,
                "snapshot_has_usable_regular_close": (
                    target.has_usable_regular_close
                ),
                "snapshot_has_complete_rvol_window": (
                    target.has_complete_rvol_window
                ),
            }
            for target in targets
        ],
    }


def plan_bytes(plan: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def load_frozen_dependency_plan(path: str) -> dict[str, Any]:
    """Load only the reviewed 458-target plan; arbitrary probe scope is refused."""
    with open(path, "rb") as handle:
        payload = handle.read()
    if hashlib.sha256(payload).hexdigest() != FROZEN_PLAN_FILE_SHA256:
        raise ValueError("dependency plan file hash mismatch")
    plan = json.loads(payload)
    if plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("unexpected dependency plan schema")
    if int(plan.get("target_count", -1)) != FROZEN_TARGET_COUNT:
        raise ValueError("dependency plan target count mismatch")
    if plan.get("role_counts") != FROZEN_ROLE_COUNTS:
        raise ValueError("dependency plan role counts mismatch")
    targets = {
        (str(item["symbol"]), str(item["day"])) for item in plan["targets"]
    }
    if len(targets) != FROZEN_TARGET_COUNT:
        raise ValueError("dependency plan unique target set mismatch")
    return plan


def render_dependency_plan(plan: Mapping[str, Any]) -> str:
    roles = plan["role_counts"]
    return "\n".join([
        "# P0-US30 Gap+RVOL D9 dependency repair plan",
        "",
        f"> generated: `{plan['generated_at_utc']}`",
        "",
        "## 판정",
        "",
        "**DEPENDENCY_TARGETS_FROZEN**",
        "",
        f"- unique dependency symbol-days: `{plan['target_count']}`",
        f"- prior regular close targets: `{roles.get('prior_regular_close', 0)}`",
        f"- RVOL 15m lookback targets: `{roles.get('rvol_15m_lookback', 0)}`",
        f"- date range: `{plan['target_min_day']}` → `{plan['target_max_day']}`",
        f"- symbols: `{len(plan['symbols'])}`",
        "",
        "## 범위",
        "",
        "- frozen snapshot read-only: `True`",
        "- blind OOS opened: `False`",
        "- performance / forward returns computed: `False`",
        "- canonical DB / frozen snapshot written: `False`",
        "",
        "## 해석",
        "",
        (
            "이 목록은 event-day 재수집 목록이 아니다. 각 D9 제외 이벤트를 실제로 "
            "만든 선행 거래일 종가 또는 직전 20거래일 09:30–09:44 RVOL 입력 중 "
            "고정 스냅샷에 없는 날만 중복 제거해 고정한 목록이다."
        ),
    ]) + "\n"


def build_dependency_probe(
    *,
    plan: Mapping[str, Any],
    observations: Sequence[Mapping[str, object]],
    generated_at_utc: str,
    http_429: int,
) -> dict[str, Any]:
    expected = {
        (str(item["symbol"]), str(item["day"])) for item in plan["targets"]
    }
    actual = {
        (str(item["symbol"]), str(item["target_day"]))
        for item in observations
    }
    if actual != expected or len(observations) != len(expected):
        raise ValueError("dependency probe observations do not cover the frozen target set")
    available = {
        (str(item["symbol"]), str(item["target_day"]))
        for item in observations if item["has_target_day"] is True
    }
    role_by_target = {
        (str(item["symbol"]), str(item["day"])): tuple(map(str, item["roles"]))
        for item in plan["targets"]
    }
    available_roles = Counter(
        role for key in available for role in role_by_target[key]
    )
    unavailable_roles = Counter(
        role for key in expected - available for role in role_by_target[key]
    )
    if not available:
        verdict = "DEPENDENCY_SOURCE_UNAVAILABLE"
    elif available == expected:
        verdict = "DEPENDENCY_SOURCE_AVAILABLE"
    else:
        verdict = "DEPENDENCY_SOURCE_PARTIAL"
    return {
        "schema": PROBE_SCHEMA,
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
        "plan_sha256": hashlib.sha256(plan_bytes(plan)).hexdigest(),
        "target_count": len(expected),
        "available_count": len(available),
        "unavailable_count": len(expected - available),
        "available_role_counts": {
            key: available_roles[key] for key in sorted(available_roles)
        },
        "unavailable_role_counts": {
            key: unavailable_roles[key] for key in sorted(unavailable_roles)
        },
        "http_429": http_429,
        "verdict": verdict,
        "observations": sorted(
            (dict(item) for item in observations),
            key=lambda item: (str(item["target_day"]), str(item["symbol"])),
        ),
    }


def probe_observation(
    *, symbol: str, target_day: date, result: Mapping[str, Any]
) -> dict[str, object]:
    return summarize_page(symbol=symbol, target_day=target_day, result=result)


def probe_bytes(report: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def render_dependency_probe(report: Mapping[str, Any]) -> str:
    return "\n".join([
        "# P0-US30 Gap+RVOL D9 dependency Toss probe",
        "",
        f"> generated: `{report['generated_at_utc']}`",
        "",
        "## 판정",
        "",
        f"**{report['verdict']}**",
        "",
        f"- frozen targets: `{report['target_count']}`",
        f"- available now: `{report['available_count']}`",
        f"- unavailable now: `{report['unavailable_count']}`",
        f"- HTTP 429 recovered: `{report['http_429']}`",
        "",
        "## 범위",
        "",
        "- provider metadata probe only; prices were not persisted",
        "- canonical/frozen databases were not written",
        "- blind OOS and performance remained closed",
    ]) + "\n"
