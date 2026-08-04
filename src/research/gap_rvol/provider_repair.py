"""Isolated Toss repair candidates for the Gap+RVOL D9 audit.

This module never mutates the canonical history database or the frozen research
snapshot.  It collects only symbol-days already present in the tracked D9
exclusion ledger and positively observed by the tracked provider probe.  Raw
provider payloads and parsed candles are written to separate, gitignored roots.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import time as wall_time
from typing import Any, Protocol

import httpx

from src.history.exchange_calendar import expected_regular_minutes, regular_close_minute
from src.history.raw_snapshot import RawSnapshotStore
from src.history.session_filter import SessionFilter
from src.history.store import HistoryStore, ImportStats
from src.models import ET, KST, SESSION_REGULAR, Candle
from src.research.gap_rvol.provider_probe import retry_after_seconds

SCHEMA = "gap-rvol-d9-toss-repair-candidate/v1"
SOURCE = "toss_gap_rvol_repair_candidate"
RUN_ID = "gap-rvol-d9-toss-repair-candidate-v1"
OPEN_MINUTE = 9 * 60 + 30
REQUIRED_THROUGH_MINUTE = 10 * 60 + 29


class ProviderPort(Protocol):
    def fetch_page(
        self,
        symbol: str,
        interval: str,
        count: int = 200,
        before: str | None = None,
        *,
        raw_sink: Callable[..., object] | None = None,
    ) -> tuple[list[Candle], str | None]: ...


@dataclass(frozen=True, order=True)
class RepairTarget:
    day: date
    symbol: str
    original_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TargetCollection:
    target: RepairTarget
    candles: tuple[Candle, ...]
    pages_fetched: int
    http_429: int
    stopped_reason: str


def build_repair_targets(
    *, audit: Mapping[str, Any], probe: Mapping[str, Any]
) -> tuple[RepairTarget, ...]:
    """Return the frozen intersection: D9 event AND probe has_target_day=True."""
    events = {
        (str(item["symbol"]), str(item["day"])): tuple(sorted(map(str, item["reasons"])))
        for item in audit["exclusion_audit"]["events"]
    }
    available = {
        (str(item["symbol"]), str(item["target_day"]))
        for item in probe["observations"]
        if item["has_target_day"] is True
    }
    targets = [
        RepairTarget(day=date.fromisoformat(day), symbol=symbol, original_reasons=reasons)
        for (symbol, day), reasons in events.items()
        if (symbol, day) in available
    ]
    return tuple(sorted(targets))


def request_before(day: date) -> str:
    """Last POST minute in the Toss API's observed KST cursor form.

    Exact UTC-day boundaries return an empty page in this API, so use 19:59 ET
    rather than the 20:00 ET boundary.
    """
    return datetime.combine(day, wall_time(19, 59), ET).astimezone(KST).isoformat(
        timespec="milliseconds"
    )


def _status_code(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", getattr(error, "status_code", None))


def _fetch_with_retry(
    provider: ProviderPort,
    *,
    symbol: str,
    before: str,
    raw_sink: Callable[..., object],
    max_retries: int,
    backoff_base: float,
    sleep: Callable[[float], None],
) -> tuple[list[Candle], str | None, int]:
    http_429 = 0
    for attempt in range(max_retries + 1):
        try:
            candles, next_before = provider.fetch_page(
                symbol, "1m", count=200, before=before, raw_sink=raw_sink
            )
            return candles, next_before, http_429
        except Exception as error:
            if _status_code(error) != 429 or attempt == max_retries:
                raise
            http_429 += 1
            retry_after = None
            if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
                retry_after = error.response.headers.get("Retry-After")
            wait = retry_after_seconds(retry_after, now=datetime.now(timezone.utc))
            sleep(max(wait if wait is not None else backoff_base * (2**attempt), 0.0))
    raise AssertionError("unreachable")


def collect_target_day(
    provider: ProviderPort,
    raw_store: RawSnapshotStore,
    target: RepairTarget,
    *,
    max_pages: int = 12,
    spacing: float = 0.4,
    max_retries: int = 5,
    backoff_base: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> TargetCollection:
    """Page backwards until the target day's PRE boundary has been crossed."""
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    before = request_before(target.day)
    seen_cursors: set[str] = set()
    by_timestamp: dict[datetime, Candle] = {}
    pages = 0
    http_429 = 0
    stopped = "max_pages"
    start = datetime.combine(target.day, wall_time(4, 0), ET)

    while pages < max_pages:
        if before in seen_cursors:
            stopped = "stale_cursor"
            break
        seen_cursors.add(before)
        candles, next_before, retries = _fetch_with_retry(
            provider,
            symbol=target.symbol,
            before=before,
            raw_sink=raw_store.emit,
            max_retries=max_retries,
            backoff_base=backoff_base,
            sleep=sleep,
        )
        http_429 += retries
        pages += 1
        for candle in candles:
            if candle.symbol != target.symbol or candle.interval != "1m":
                raise ValueError("provider returned the wrong symbol or interval")
            if candle.ts_exchange.date() == target.day:
                existing = by_timestamp.get(candle.ts_utc)
                if existing is not None and existing != candle:
                    raise ValueError("provider returned conflicting duplicate minute")
                by_timestamp[candle.ts_utc] = candle

        if candles and min(candle.ts_exchange for candle in candles) <= start:
            stopped = "crossed_pre_boundary"
            break
        if not candles:
            stopped = "empty_page"
            break
        if next_before is None:
            stopped = "nextbefore_null"
            break
        before = str(next_before)
        if spacing:
            sleep(spacing)

    return TargetCollection(
        target=target,
        candles=tuple(by_timestamp[key] for key in sorted(by_timestamp)),
        pages_fetched=pages,
        http_429=http_429,
        stopped_reason=stopped,
    )


def _minute(candle: Candle) -> int:
    return candle.ts_exchange.hour * 60 + candle.ts_exchange.minute


def summarize_collection(collection: TargetCollection) -> dict[str, object]:
    target = collection.target
    regular = {
        _minute(candle)
        for candle in collection.candles
        if candle.session == SESSION_REGULAR
    }
    close = regular_close_minute(target.day)
    if close is None:
        raise ValueError(f"repair target is not an NYSE trading day: {target.day}")
    expected = set(range(OPEN_MINUTE, close))
    required = set(range(OPEN_MINUTE, REQUIRED_THROUGH_MINUTE + 1))
    missing = sorted(expected - regular)
    return {
        "day": target.day.isoformat(),
        "symbol": target.symbol,
        "original_reasons": list(target.original_reasons),
        "pages_fetched": collection.pages_fetched,
        "http_429": collection.http_429,
        "stopped_reason": collection.stopped_reason,
        "target_rows": len(collection.candles),
        "regular_minutes": len(regular),
        "expected_regular_minutes": expected_regular_minutes(target.day),
        "missing_regular_minutes": len(missing),
        "first_missing_regular_minute": missing[0] if missing else None,
        "full_regular_session": regular == expected,
        "required_feature_minutes_complete": required.issubset(regular),
    }


def _data_content_sha256(rows: Any) -> str:
    """Hash market data rows; SQLite page layout and ingest times are excluded."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    digest.update(f"rows={count}\n".encode())
    return digest.hexdigest()


def data_content_sha256(store: HistoryStore) -> str:
    """Hash a live candidate store after its page commits."""
    rows = store._con.execute(
        "SELECT symbol,interval,ts_utc,ts_exchange,session,open,high,low,close,volume,"
        "currency,adjusted,source FROM candle_history ORDER BY symbol,interval,ts_utc"
    )
    return _data_content_sha256(rows)


def data_content_sha256_path(db_path: str) -> str:
    """Verify an existing candidate DB through a read-only connection."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT symbol,interval,ts_utc,ts_exchange,session,open,high,low,close,volume,"
            "currency,adjusted,source FROM candle_history ORDER BY symbol,interval,ts_utc"
        )
        return _data_content_sha256(rows)
    finally:
        con.close()


def build_manifest(
    *,
    targets: Sequence[RepairTarget],
    summaries: Sequence[Mapping[str, object]],
    raw_manifest: Mapping[str, object],
    repair_db_content_sha256: str,
    repair_db_rows: int,
    generated_at_utc: str,
) -> dict[str, object]:
    if len(summaries) != len(targets):
        raise ValueError("every repair target must have one summary")
    full = sum(item["full_regular_session"] is True for item in summaries)
    required = sum(
        item["required_feature_minutes_complete"] is True for item in summaries
    )
    return {
        "schema": SCHEMA,
        "generated_at_utc": generated_at_utc,
        "scope": {
            "canonical_db_written": False,
            "frozen_snapshot_written": False,
            "isolated_candidate_db_written": True,
            "blind_oos_opened": False,
            "performance_computed": False,
            "forward_returns_computed": False,
        },
        "selection_rule": "tracked D9 exclusion event AND tracked probe has_target_day=true",
        "provider": "toss",
        "interval": "1m",
        "sessions": ["PRE", "REGULAR", "POST"],
        "target_count": len(targets),
        "full_regular_session_count": full,
        "required_feature_minutes_complete_count": required,
        "repair_db_rows": repair_db_rows,
        "repair_db_data_content_sha256": repair_db_content_sha256,
        "raw_content_file_count": raw_manifest["content_file_count"],
        "raw_content_sha256": raw_manifest["raw_content_sha256"],
        "http_429": sum(int(item["http_429"]) for item in summaries),
        "targets": sorted(
            (dict(item) for item in summaries),
            key=lambda item: (str(item["day"]), str(item["symbol"])),
        ),
    }


def manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def render_manifest_report(manifest: Mapping[str, object]) -> str:
    incomplete = [
        item for item in manifest["targets"]
        if item["full_regular_session"] is not True
    ]
    lines = [
        "# P0-US30 Gap+RVOL D9 Toss repair candidate",
        "",
        f"> generated: `{manifest['generated_at_utc']}`",
        "",
        "## 판정",
        "",
        "**PARTIAL_REPAIR_CANDIDATE**",
        "",
        f"- frozen targets: `{manifest['target_count']}`",
        f"- full regular session: `{manifest['full_regular_session_count']}`",
        (
            "- required feature minutes complete: "
            f"`{manifest['required_feature_minutes_complete_count']}`"
        ),
        f"- incomplete and ineligible: `{len(incomplete)}`",
        f"- isolated candidate rows: `{manifest['repair_db_rows']}`",
        f"- HTTP 429 recovered: `{manifest['http_429']}`",
        "",
        "## 불변식",
        "",
        "- canonical DB written: `False`",
        "- frozen research snapshot written: `False`",
        "- blind OOS opened: `False`",
        "- performance / forward returns computed: `False`",
        "- selection: tracked D9 exclusion event AND tracked probe availability",
        "",
        "## 식별자",
        "",
        f"- repair data content: `{manifest['repair_db_data_content_sha256']}`",
        f"- raw content: `{manifest['raw_content_sha256']}`",
        f"- raw files: `{manifest['raw_content_file_count']}`",
        "",
        "## 불완전 후보",
        "",
        "| day | symbol | regular | missing | first missing |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for item in incomplete:
        lines.append(
            f"| {item['day']} | {item['symbol']} | {item['regular_minutes']} "
            f"| {item['missing_regular_minutes']} "
            f"| {item['first_missing_regular_minute']} |"
        )
    lines.extend([
        "",
        (
            "이번 산출물은 복구 후보 계층이다. full regular session을 충족한 66개 "
            "symbol-day만 별도 overlay 재감사 후보이며, 389/390인 10개는 결과에 "
            "사용하지 않는다. 이 단계 자체는 D9 재판정이나 경제 성과 판정이 아니다."
        ),
        "",
    ])
    return "\n".join(lines)


def persist_collection(
    store: HistoryStore,
    collection: TargetCollection,
    session_filter: SessionFilter,
) -> ImportStats:
    """Persist one target to the isolated candidate DB and append provenance."""
    kept, excluded = session_filter.partition(list(collection.candles))
    stats = ImportStats(source=SOURCE)
    store.upsert_candles(kept, source=SOURCE, run_id=RUN_ID, stats=stats)
    earliest = min((c.ts_utc.isoformat() for c in kept), default=None)
    latest = max((c.ts_utc.isoformat() for c in kept), default=None)
    note = json.dumps({
        "day": collection.target.day.isoformat(),
        "sessions": sorted(session_filter.allowed),
        "excluded": dict(sorted(excluded.items())),
        "stopped_reason": collection.stopped_reason,
    }, sort_keys=True, separators=(",", ":"))
    store.write_ingest_log(
        RUN_ID,
        source=SOURCE,
        stats=stats,
        rows_seen=len(collection.candles),
        earliest=earliest,
        latest=latest,
        note=note,
        symbol=collection.target.symbol,
        interval="1m",
    )
    store.commit()
    return stats
