"""FRED/ALFRED point-in-time macro observations adapter."""
from __future__ import annotations

from datetime import timedelta
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
                    collected_at=context.as_of,
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
            eligible = [
                row for row in observations
                if isinstance(row, dict)
                and str(row.get("date", "")) <= context.trading_day.isoformat()
                and str(row.get("value", "")) not in {"", "."}
            ]
            if not eligible:
                continue
            row = max(eligible, key=lambda item: str(item.get("date", "")))
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
        status = "ERROR" if errors else "OK"
        error_code = ";".join(sorted(errors)) or None
        return result(self.provider, self.kind, status, self.config.base_url, context.as_of, items, receipts, error_code)
