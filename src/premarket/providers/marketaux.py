"""Marketaux Free NEWS adapter with a persistent 60/day hard cap."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result, unavailable
from src.premarket.providers.config import MarketauxConfig
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError
from src.premarket.schedule import iso_z, parse_iso


class QuotaLedger:
    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def reserve(self, *, limit: int) -> bool:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("quota clock must be timezone-aware")
        day = now.astimezone(timezone.utc).date().isoformat()
        path = self.root / f"marketaux-{day}.json"
        lock_path = self.root / f"marketaux-{day}.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("schema") != "marketaux-quota/v1" or data.get("day") != day:
                    raise ValueError("invalid Marketaux quota ledger")
                used = int(data.get("used", -1))
                if used < 0 or used > limit:
                    raise ValueError("invalid Marketaux quota usage")
            else:
                used = 0
            if used >= limit:
                return False
            payload = {"schema": "marketaux-quota/v1", "day": day, "used": used + 1}
            partial = path.with_name(
                f"{path.name}.{os.getpid()}.{threading.get_ident()}.partial"
            )
            try:
                with open(partial, "x", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(partial, path)
                self._fsync_directory(path.parent)
            finally:
                if partial.exists():
                    partial.unlink()
            return True

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class MarketauxProvider:
    provider = "marketaux"
    kind = "NEWS"

    def __init__(self, config: MarketauxConfig, http: BoundedHttpClient, quota: QuotaLedger, *, token: str):
        self.config = config
        self.http = http
        self.quota = quota
        self.token = token.strip()

    def collect(self, context: CollectionContext, raw_store: RawEvidenceStore) -> dict[str, Any]:
        if not self.config.enabled:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "MARKETAUX_DISABLED")
        if not self.token:
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "MARKETAUX_CREDENTIAL_MISSING")
        published_after = iso_z(
            parse_iso(context.as_of) - timedelta(minutes=self.config.lookback_minutes)
        ).removesuffix("Z")
        base_params = {
            "api_token": self.token,
            "symbols": ",".join(context.symbols),
            "language": "en",
            "filter_entities": "true",
            "must_have_entities": "true",
            "group_similar": "true",
            "published_after": published_after,
            "published_before": context.as_of.removesuffix("Z"),
            "limit": self.config.max_articles,
        }
        stage = context.stage.upper()
        max_pages = self.config.max_pages_by_stage.get(stage)
        if max_pages is None:
            return unavailable(
                self.provider,
                self.kind,
                self.config.base_url,
                context.as_of,
                "MARKETAUX_STAGE_UNSUPPORTED",
            )
        receipts: list[dict[str, Any]] = []
        items_by_id: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        allowed = set(context.symbols)
        complete = False
        expected_found: int | None = None
        for page in range(1, max_pages + 1):
            try:
                payload, receipt = self.http.get_json(
                    self.config.base_url,
                    params=dict(base_params, page=page),
                    provider=self.provider,
                    resource=f"news-all-page-{page}",
                    raw_store=raw_store,
                    before_attempt=lambda: self.quota.reserve(
                        limit=self.config.daily_limit
                    ),
                )
                receipts.append(receipt)
            except HttpCollectionError as error:
                if error.receipt:
                    receipts.append(error.receipt)
                errors.append(
                    "MARKETAUX_INCOMPLETE_QUOTA"
                    if error.code == "HTTP_REQUEST_BUDGET_EXHAUSTED"
                    else error.code
                )
                break
            rows = payload.get("data", [])
            meta = payload.get("meta")
            if not isinstance(rows, list):
                errors.append("MARKETAUX_DATA_NOT_LIST")
                break
            if not isinstance(meta, dict):
                errors.append("MARKETAUX_META_NOT_OBJECT")
                break
            try:
                found = int(meta["found"])
                returned = int(meta["returned"])
                response_page = int(meta["page"])
                response_limit = int(meta["limit"])
            except (KeyError, TypeError, ValueError):
                errors.append("MARKETAUX_META_INVALID")
                break
            if (
                found < 0
                or returned < 0
                or returned > found
                or returned != len(rows)
                or response_page != page
                or response_limit != self.config.max_articles
            ):
                errors.append("MARKETAUX_META_MISMATCH")
                break
            if expected_found is None:
                expected_found = found
            elif found != expected_found:
                errors.append("MARKETAUX_RESULT_SET_CHANGED")
                break
            for row in rows:
                safe = self._normalize_row(row, allowed, context.as_of, errors)
                if safe is None:
                    continue
                item_id = safe["item_id"]
                existing = items_by_id.get(item_id)
                if existing is not None and existing != safe:
                    errors.append("MARKETAUX_ARTICLE_CONFLICT")
                    continue
                items_by_id[item_id] = safe
            if returned < response_limit or page * response_limit >= found:
                complete = True
                break
        if not complete and not errors:
            errors.append("MARKETAUX_INCOMPLETE_STAGE_PAGE_CAP")
        return result(
            self.provider,
            self.kind,
            "ERROR" if errors else "OK",
            self.config.base_url,
            context.as_of,
            list(items_by_id.values()),
            receipts,
            ";".join(sorted(set(errors))) or None,
        )

    @staticmethod
    def _normalize_row(
        row: Any,
        allowed: set[str],
        as_of: str,
        errors: list[str],
    ) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            errors.append("MARKETAUX_ARTICLE_NOT_OBJECT")
            return None
        entities = row.get("entities", [])
        if not isinstance(entities, list):
            errors.append("MARKETAUX_ENTITIES_NOT_LIST")
            return None
        symbols = sorted({
            str(entity.get("symbol", "")).strip().upper()
            for entity in entities if isinstance(entity, dict)
        } & allowed)
        if not symbols:
            return None
        sentiments = [
            float(entity["sentiment_score"])
            for entity in entities
            if isinstance(entity, dict) and entity.get("symbol", "").upper() in symbols
            and isinstance(entity.get("sentiment_score"), (int, float))
        ]
        safe = {
            "item_type": "NEWS",
            "item_id": f"marketaux:{str(row.get('uuid', '')).strip()}",
            "published_at": str(row.get("published_at", "")).strip(),
            "source_url": str(row.get("url", "")).strip(),
            "source_name": str(row.get("source", "")).strip(),
            "symbols": symbols,
            "sentiment": round(sum(sentiments) / len(sentiments), 12) if sentiments else 0.0,
            "headline_sha256": payload_sha256(str(row.get("title", "")).strip()),
        }
        if not safe["item_id"].removeprefix("marketaux:"):
            errors.append("MARKETAUX_ARTICLE_ID_MISSING")
            return None
        try:
            published_at = iso_z(parse_iso(safe["published_at"]))
        except (TypeError, ValueError):
            errors.append("MARKETAUX_PUBLISHED_AT_INVALID")
            return None
        if published_at > as_of:
            errors.append("MARKETAUX_FUTURE_ARTICLE")
            return None
        if not safe["source_url"].startswith(("https://", "http://")):
            errors.append("MARKETAUX_SOURCE_URL_INVALID")
            return None
        safe["published_at"] = published_at
        safe["content_sha256"] = payload_sha256(safe)
        return safe
