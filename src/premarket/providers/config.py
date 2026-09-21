"""Configuration contract for bounded premarket evidence collection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

CONFIG_SCHEMA = "premarket-provider-config/v1"


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _positive(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _https_url(value: Any, name: str) -> str:
    text = str(value).strip()
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{name} must be an https URL")
    return text.rstrip("/")


@dataclass(frozen=True)
class HttpPolicy:
    allowed_hosts: tuple[str, ...]
    timeout_seconds: float
    max_response_bytes: int
    max_retries: int
    retry_backoff_seconds: float
    max_retry_after_seconds: float = 30.0


@dataclass(frozen=True)
class MarketauxConfig:
    enabled: bool
    base_url: str
    daily_limit: int
    lookback_minutes: int
    max_articles: int
    max_pages_by_stage: dict[str, int]


@dataclass(frozen=True)
class SecEdgarConfig:
    enabled: bool
    base_url: str
    requests_per_second: float
    forms: tuple[str, ...]
    symbol_ciks: dict[str, str]


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    label: str
    max_age_days: int = 7
    freshness_reason: str = ""
    required_for_gate: bool = True
    observation_lookback_days: int = 45


@dataclass(frozen=True)
class FredConfig:
    enabled: bool
    base_url: str
    release_dates_url: str
    series: tuple[FredSeries, ...]


@dataclass(frozen=True)
class ProviderConfig:
    http: HttpPolicy
    marketaux: MarketauxConfig
    sec_edgar: SecEdgarConfig
    fred: FredConfig

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ProviderConfig:
        if raw.get("schema") != CONFIG_SCHEMA:
            raise ValueError(f"config schema must be {CONFIG_SCHEMA}")
        http = _mapping(raw.get("http"), "http")
        hosts = tuple(sorted({str(item).strip().lower() for item in http.get("allowed_hosts", []) if str(item).strip()}))
        if not hosts:
            raise ValueError("http.allowed_hosts must be non-empty")
        http_policy = HttpPolicy(
            allowed_hosts=hosts,
            timeout_seconds=_positive(http.get("timeout_seconds", 10), "http.timeout_seconds"),
            max_response_bytes=int(_positive(http.get("max_response_bytes", 2_000_000), "http.max_response_bytes")),
            max_retries=max(0, int(http.get("max_retries", 2))),
            retry_backoff_seconds=max(0.0, float(http.get("retry_backoff_seconds", 1))),
            max_retry_after_seconds=_positive(
                http.get("max_retry_after_seconds", 30),
                "http.max_retry_after_seconds",
            ),
        )

        marketaux = _mapping(raw.get("marketaux"), "marketaux")
        pages_raw = _mapping(
            marketaux.get("max_pages_by_stage", {"PRIMARY": 30, "VERIFICATION": 20, "FREEZE": 10}),
            "marketaux.max_pages_by_stage",
        )
        pages_by_stage = {
            stage: int(_positive(pages_raw.get(stage), f"marketaux.max_pages_by_stage.{stage}"))
            for stage in ("PRIMARY", "VERIFICATION", "FREEZE")
        }
        if sum(pages_by_stage.values()) > 60:
            raise ValueError("Marketaux stage page caps must sum to at most 60")
        marketaux_config = MarketauxConfig(
            enabled=bool(marketaux.get("enabled", True)),
            base_url=_https_url(marketaux.get("base_url"), "marketaux.base_url"),
            daily_limit=int(_positive(marketaux.get("daily_limit", 60), "marketaux.daily_limit")),
            lookback_minutes=int(_positive(marketaux.get("lookback_minutes", 180), "marketaux.lookback_minutes")),
            max_articles=int(_positive(marketaux.get("max_articles", 3), "marketaux.max_articles")),
            max_pages_by_stage=pages_by_stage,
        )
        if marketaux_config.daily_limit > 60:
            raise ValueError("marketaux.daily_limit cannot exceed the hard cap of 60")
        if marketaux_config.max_articles > 3:
            raise ValueError("Marketaux Free cannot return more than 3 articles per request")

        sec = _mapping(raw.get("sec_edgar"), "sec_edgar")
        ciks = {
            str(symbol).strip().upper(): str(cik).strip().zfill(10)
            for symbol, cik in _mapping(sec.get("symbol_ciks", {}), "sec_edgar.symbol_ciks").items()
        }
        if any(not cik.isdigit() or len(cik) != 10 for cik in ciks.values()):
            raise ValueError("SEC CIK values must be 10 digits")
        sec_config = SecEdgarConfig(
            enabled=bool(sec.get("enabled", True)),
            base_url=_https_url(sec.get("base_url"), "sec_edgar.base_url"),
            requests_per_second=_positive(sec.get("requests_per_second", 2), "sec_edgar.requests_per_second"),
            forms=tuple(sorted({str(item).strip().upper() for item in sec.get("forms", []) if str(item).strip()})),
            symbol_ciks=dict(sorted(ciks.items())),
        )
        if sec_config.requests_per_second > 2:
            raise ValueError("sec_edgar.requests_per_second cannot exceed 2")

        fred = _mapping(raw.get("fred"), "fred")
        series_items: list[FredSeries] = []
        for item in fred.get("series", []):
            row = _mapping(item, "fred.series item")
            series_id = str(row.get("series_id", "")).strip().upper()
            label = str(row.get("label", "")).strip()
            if not series_id or not label:
                raise ValueError("fred series_id and label must be non-empty")
            max_age_days = int(_positive(
                row.get("max_age_days", 7),
                "fred.series.max_age_days",
            ))
            freshness_reason = str(row.get("freshness_reason", "")).strip()
            if max_age_days > 7 and not freshness_reason:
                raise ValueError(
                    "fred series with max_age_days > 7 requires freshness_reason"
                )
            observation_lookback_days = int(_positive(
                row.get("observation_lookback_days", max(45, max_age_days)),
                "fred.series.observation_lookback_days",
            ))
            if observation_lookback_days < max_age_days:
                raise ValueError(
                    "fred series observation_lookback_days must be >= max_age_days"
                )
            if observation_lookback_days > 370:
                raise ValueError(
                    "fred series observation_lookback_days cannot exceed 370"
                )
            series_items.append(FredSeries(
                series_id=series_id,
                label=label,
                max_age_days=max_age_days,
                freshness_reason=freshness_reason,
                required_for_gate=bool(row.get("required_for_gate", True)),
                observation_lookback_days=observation_lookback_days,
            ))
        if len({item.series_id for item in series_items}) != len(series_items):
            raise ValueError("fred series IDs must be unique")
        fred_config = FredConfig(
            enabled=bool(fred.get("enabled", True)),
            base_url=_https_url(fred.get("base_url"), "fred.base_url"),
            release_dates_url=_https_url(
                fred.get(
                    "release_dates_url",
                    "https://api.stlouisfed.org/fred/releases/dates",
                ),
                "fred.release_dates_url",
            ),
            series=tuple(sorted(series_items, key=lambda item: item.series_id)),
        )
        if not fred_config.series:
            raise ValueError("fred.series allowlist must be non-empty")
        return cls(http=http_policy, marketaux=marketaux_config, sec_edgar=sec_config, fred=fred_config)


def load_provider_config(path: str | Path) -> ProviderConfig:
    with open(path, encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    return ProviderConfig.from_mapping(_mapping(raw, "config root"))
