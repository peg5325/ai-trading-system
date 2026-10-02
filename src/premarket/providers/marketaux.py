"""Marketaux Free NEWS adapter with a persistent 60/day hard cap."""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result, unavailable
from src.premarket.providers.config import MarketauxConfig
from src.premarket.providers.http import BoundedHttpClient, HttpCollectionError
from src.premarket.schedule import iso_z, parse_iso

# --- NEWS title relevance (paper-contract v1.3, effective 2026-10-02) -------------
# A Marketaux article is title-relevant to symbol S iff its title mentions S by
#   (a) the ticker S as a whole word, case-sensitive, optionally prefixed by "$"
#       (word boundary = not adjacent to an ASCII letter or digit), or
#   (b) the "name core" of an entity with symbol S as a whole-word,
#       case-insensitive phrase.
# Name core: entity ``name`` lowercased, every run of non-alphanumeric characters
# replaced by one space, a leading "the" removed, then trailing tokens in
# _NAME_SUFFIXES (and "class a|b|c") removed repeatedly. If the core is empty or a
# single word in _GENERIC_NAME_WORDS, the name-core rule is skipped. (c) SYMBOL_ALIASES
# below are a third, fixed phrase source for stocks. The title is
# normalized the same way before the phrase test. No thresholds are involved.
# Only the per-symbol result (``title_relevant_symbols``) is stored; the title
# itself stays a hash. The brief config flag ``news_title_relevance_required``
# decides whether the normalizer applies it.
_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "plc", "holdings", "llc", "lp", "com", "etf", "fund", "trust",
})
_SHARE_CLASS_LETTERS = frozenset({"a", "b", "c"})
_GENERIC_NAME_WORDS = frozenset({
    "american", "capital", "energy", "financial", "first", "fund", "general",
    "global", "group", "index", "international", "invesco", "ishares",
    "national", "spdr", "technologies", "technology", "united", "vanguard",
})
# Fixed hand-written aliases (orchestrator decision 2026-10-02, before any v1.3
# result), unioned with the ticker and name-core rules; matched as whole-word,
# case-insensitive phrases after the same normalization. ETFs have no aliases.
SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "AAPL": ("apple",),
    "MSFT": ("microsoft",),
    "NVDA": ("nvidia",),
    "AMD": ("advanced micro devices",),
    "AVGO": ("broadcom",),
    "AMZN": ("amazon",),
    "META": ("meta", "facebook"),
    "GOOGL": ("google", "alphabet"),
    "TSLA": ("tesla",),
    "MU": ("micron",),
    "NFLX": ("netflix",),
    "CRM": ("salesforce",),
    "PLTR": ("palantir",),
    "VST": ("vistra",),
}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _normalize_words(text: str) -> list[str]:
    return _NON_ALNUM.sub(" ", text.lower()).split()


def name_core(name: str) -> str:
    """Company/fund name core used by the title-relevance rule ("" = ticker only)."""
    words = _normalize_words(name)
    if words[:1] == ["the"]:
        words = words[1:]
    while words:
        if words[-1] in _NAME_SUFFIXES:
            words = words[:-1]
        elif (
            len(words) >= 2
            and words[-2] == "class"
            and words[-1] in _SHARE_CLASS_LETTERS
        ):
            words = words[:-2]
        else:
            break
    if len(words) == 1 and words[0] in _GENERIC_NAME_WORDS:
        return ""
    return " ".join(words)


def title_mentions(title: str, symbol: str, names: Iterable[str] = ()) -> bool:
    """True iff ``title`` mentions ``symbol`` by ticker or by an entity name core."""
    ticker = re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(symbol)}(?![A-Za-z0-9])")
    if symbol and ticker.search(title):
        return True
    padded_title = f" {' '.join(_normalize_words(title))} "
    phrases = [name_core(name) for name in names]
    phrases += [" ".join(_normalize_words(alias)) for alias in SYMBOL_ALIASES.get(symbol, ())]
    return any(phrase and f" {phrase} " in padded_title for phrase in phrases)


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
                    timeout_seconds=self.config.timeout_seconds,
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
        title = str(row.get("title", "")).strip()
        names_by_symbol: dict[str, list[str]] = {symbol: [] for symbol in symbols}
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            symbol = str(entity.get("symbol", "")).strip().upper()
            if symbol in names_by_symbol and isinstance(entity.get("name"), str):
                names_by_symbol[symbol].append(entity["name"])
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
            "headline_sha256": payload_sha256(title),
            "title_relevant_symbols": [
                symbol for symbol in symbols
                if title_mentions(title, symbol, names_by_symbol[symbol])
            ],
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
