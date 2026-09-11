"""FRED/ALFRED point-in-time macro observations adapter."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result, unavailable
from src.premarket.providers.config import FredConfig
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError


class FredProvider:
    provider = "fred-alfred"
    kind = "MACRO"

    def __init__(self, config: FredConfig, http: BoundedHttpClient, *, api_key: str):
        self.config = config
        self.http = http
        self.api_key = api_key.strip()

    def collect(self, context: CollectionContext, raw_store: RawEvidenceStore) -> dict[str, Any]:
        if not self.config.enabled:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "FRED_DISABLED")
        if not self.api_key:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "FRED_CREDENTIAL_MISSING")
        vintage_day = context.as_of[:10]
        receipts: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for series in self.config.series:
            params = {
                "api_key": self.api_key,
                "file_type": "json",
                "series_id": series.series_id,
                "realtime_start": vintage_day,
                "realtime_end": vintage_day,
                "observation_start": (context.trading_day - timedelta(days=45)).isoformat(),
                "observation_end": context.trading_day.isoformat(),
                "sort_order": "asc",
            }
            try:
                payload, receipt = self.http.get_json(
                    self.config.base_url,
                    params=params,
                    provider=self.provider,
                    resource=f"series-{series.series_id}",
                    raw_store=raw_store,
                )
                receipts.append(receipt)
            except HttpCollectionError as error:
                if error.receipt:
                    receipts.append(error.receipt)
                errors.append(f"{series.series_id}:{error.code}")
                continue
            observations = payload.get("observations", [])
            if not isinstance(observations, list):
                errors.append(f"{series.series_id}:FRED_OBSERVATIONS_NOT_LIST")
                continue
            eligible: list[dict[str, Any]] = []
            for row in observations:
                if not isinstance(row, dict):
                    errors.append(f"{series.series_id}:FRED_OBSERVATION_NOT_OBJECT")
                    continue
                try:
                    observation_day = date.fromisoformat(str(row.get("date", "")))
                    value = Decimal(str(row.get("value", "")))
                except (InvalidOperation, ValueError):
                    errors.append(f"{series.series_id}:FRED_OBSERVATION_INVALID")
                    continue
                if not value.is_finite():
                    errors.append(f"{series.series_id}:FRED_OBSERVATION_INVALID")
                    continue
                if observation_day <= context.trading_day:
                    eligible.append(row)
            if not eligible:
                errors.append(f"{series.series_id}:FRED_OBSERVATION_MISSING")
                continue
            row = max(eligible, key=lambda item: str(item.get("date", "")))
            observation_day = date.fromisoformat(str(row["date"]))
            if (context.trading_day - observation_day).days > series.max_age_days:
                errors.append(f"{series.series_id}:FRED_OBSERVATION_STALE")
                continue
            safe = {
                "item_type": "MACRO",
                "item_id": f"fred:{series.series_id}:{row['date']}:{vintage_day}",
                "series_id": series.series_id,
                "label": series.label,
                "observation_date": str(row["date"]),
                "value": str(row["value"]),
                "realtime_start": str(row.get("realtime_start", payload.get("realtime_start", vintage_day))),
                "realtime_end": str(row.get("realtime_end", payload.get("realtime_end", vintage_day))),
                "retrieved_as_of": context.as_of,
                "source_url": f"https://fred.stlouisfed.org/series/{series.series_id}",
            }
            safe["content_sha256"] = payload_sha256(safe)
            items.append(safe)

        self._collect_release_dates(context, raw_store, receipts, items, errors)
        status = "ERROR" if errors else "OK"
        error_code = ";".join(sorted(errors)) or None
        return result(self.provider, self.kind, status, self.config.base_url, context.as_of, items, receipts, error_code)

    def _collect_release_dates(
        self,
        context: CollectionContext,
        raw_store: RawEvidenceStore,
        receipts: list[dict[str, Any]],
        items: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        page_size = 1000
        complete = False
        for offset in range(0, 5000, page_size):
            params = {
                "api_key": self.api_key,
                "file_type": "json",
                "realtime_start": context.trading_day.isoformat(),
                "realtime_end": context.trading_day.isoformat(),
                "include_release_dates_with_no_data": "true",
                "limit": page_size,
                "offset": offset,
                "order_by": "release_date",
                "sort_order": "desc",
            }
            try:
                payload, receipt = self.http.get_json(
                    self.config.release_dates_url,
                    params=params,
                    provider=self.provider,
                    resource=f"release-dates-{offset}",
                    raw_store=raw_store,
                )
                receipts.append(receipt)
            except HttpCollectionError as error:
                if error.receipt:
                    receipts.append(error.receipt)
                errors.append(f"RELEASE_DATES:{error.code}")
                return
            release_rows = payload.get("release_dates")
            if not isinstance(release_rows, list):
                errors.append("FRED_RELEASE_DATES_NOT_LIST")
                return
            try:
                count = int(payload["count"])
                response_offset = int(payload["offset"])
                response_limit = int(payload["limit"])
            except (KeyError, TypeError, ValueError):
                errors.append("FRED_RELEASE_META_INVALID")
                return
            if (
                count < 0
                or response_offset != offset
                or response_limit != page_size
                or len(release_rows) > page_size
            ):
                errors.append("FRED_RELEASE_META_MISMATCH")
                return
            self._append_release_rows(context, release_rows, items, errors)
            if offset + len(release_rows) >= count:
                complete = True
                break
        if not complete:
            errors.append("FRED_RELEASE_DATES_INCOMPLETE")

    @staticmethod
    def _append_release_rows(
        context: CollectionContext,
        release_rows: list[Any],
        items: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        for row in release_rows:
            if not isinstance(row, dict):
                errors.append("FRED_RELEASE_DATE_NOT_OBJECT")
                continue
            release_id = str(row.get("release_id", "")).strip()
            release_name = str(row.get("release_name", "")).strip()
            release_date = str(row.get("date", "")).strip()
            try:
                parsed_release_date = date.fromisoformat(release_date)
            except ValueError:
                errors.append("FRED_RELEASE_DATE_INVALID")
                continue
            if parsed_release_date != context.trading_day:
                continue
            if not release_id or not release_name:
                errors.append("FRED_RELEASE_DATE_INVALID")
                continue
            safe = {
                "item_type": "MACRO_RELEASE",
                "item_id": f"fred-release:{release_id}:{release_date}",
                "release_id": release_id,
                "release_name": release_name,
                "release_date": release_date,
                "retrieved_as_of": context.as_of,
                "availability_semantics": "SCHEDULE_DATE_NOT_DATA_AVAILABILITY",
                "source_url": "https://fred.stlouisfed.org/releases/calendar",
            }
            safe["content_sha256"] = payload_sha256(safe)
            items.append(safe)
