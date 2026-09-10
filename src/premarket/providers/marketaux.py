"""Marketaux Free NEWS adapter with a persistent 60/day hard cap."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result, unavailable
from src.premarket.providers.config import MarketauxConfig
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError
from src.premarket.schedule import iso_z, parse_iso


class QuotaLedger:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def reserve(self, day: str, *, limit: int) -> bool:
        path = self.root / f"marketaux-{day}.json"
        lock_path = self.root / f"marketaux-{day}.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("schema") != "marketaux-quota/v1" or data.get("day") != day:
                    raise ValueError("invalid Marketaux quota ledger")
                used = int(data.get("used", 0))
            else:
                used = 0
            if used >= limit:
                return False
            payload = {"schema": "marketaux-quota/v1", "day": day, "used": used + 1}
            partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
            with open(partial, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, path)
            return True


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
        if not self.quota.reserve(context.trading_day.isoformat(), limit=self.config.daily_limit):
            return unavailable(self.provider, self.kind, self.config.base_url, context.as_of, "MARKETAUX_DAILY_QUOTA_EXHAUSTED")
        published_after = iso_z(
            parse_iso(context.as_of) - timedelta(minutes=self.config.lookback_minutes)
        ).removesuffix("Z")
        params = {
            "api_token": self.token,
            "symbols": ",".join(context.symbols),
            "language": "en",
            "filter_entities": "true",
            "must_have_entities": "true",
            "published_after": published_after,
            "published_before": context.as_of.removesuffix("Z"),
            "limit": self.config.max_articles,
        }
        try:
            payload, receipt = self.http.get_json(
                self.config.base_url,
                params=params,
                provider=self.provider,
                resource="news-all",
                collected_at=context.as_of,
                raw_store=raw_store,
            )
        except HttpCollectionError as error:
            receipts = [error.receipt] if error.receipt else []
            return result(self.provider, self.kind, "ERROR", self.config.base_url, context.as_of, [], receipts, error.code)
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return result(self.provider, self.kind, "ERROR", self.config.base_url, context.as_of, [], [receipt], "MARKETAUX_DATA_NOT_LIST")
        items = []
        errors = []
        allowed = set(context.symbols)
        for row in rows:
            if not isinstance(row, dict):
                errors.append("MARKETAUX_ARTICLE_NOT_OBJECT")
                continue
            entities = row.get("entities", [])
            if not isinstance(entities, list):
                errors.append("MARKETAUX_ENTITIES_NOT_LIST")
                continue
            symbols = sorted({
                str(entity.get("symbol", "")).strip().upper()
                for entity in entities if isinstance(entity, dict)
            } & allowed)
            if not symbols:
                continue
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
                continue
            try:
                published_at = iso_z(parse_iso(safe["published_at"]))
            except (TypeError, ValueError):
                errors.append("MARKETAUX_PUBLISHED_AT_INVALID")
                continue
            if published_at > context.as_of:
                errors.append("MARKETAUX_FUTURE_ARTICLE")
                continue
            if not safe["source_url"].startswith(("https://", "http://")):
                errors.append("MARKETAUX_SOURCE_URL_INVALID")
                continue
            safe["published_at"] = published_at
            safe["content_sha256"] = payload_sha256(safe)
            items.append(safe)
        return result(
            self.provider,
            self.kind,
            "ERROR" if errors else "OK",
            self.config.base_url,
            context.as_of,
            items,
            [receipt],
            ";".join(sorted(set(errors))) or None,
        )
