"""Normalized point-in-time input contract and immutable snapshot helpers."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from src.premarket.schedule import PremarketSchedule, parse_iso

INPUT_SCHEMA = "premarket-input/v1"
INPUT_SCHEMA_V2 = "premarket-input/v2"
CONFIG_SCHEMA = "premarket-config/v1"
SNAPSHOT_SCHEMA = "premarket-snapshot/v1"
ALLOWED_SOURCE_KINDS = frozenset({"MARKET", "NEWS", "MACRO", "FILING"})
ALLOWED_SOURCE_STATUSES = frozenset({"OK", "ERROR", "UNAVAILABLE"})
ALLOWED_REGIMES = frozenset({"RISK_ON", "RISK_OFF", "UNCERTAIN"})
ALLOWED_STAGES = frozenset({"PRIMARY", "VERIFICATION", "FREEZE"})
SCORE_FIELDS = (
    "catalyst_score",
    "trend_score",
    "relative_strength_score",
    "volume_score",
    "liquidity_score",
)


# The only strategy template the normalizer may attach.  It is attached only when the
# Daily Brief config already approves it (LONG and/or SHORT list); with the default
# empty allowlists nothing changes, so snapshots and brief hashes stay identical.
FREEZE_PAPER_TEMPLATE_ID = "premarket_freeze_long_intraday_v1"


def attachable_template(config: PremarketConfig) -> str | None:
    approved = set(config.approved_strategy_templates) | set(config.approved_short_strategy_templates)
    return FREEZE_PAPER_TEMPLATE_ID if FREEZE_PAPER_TEMPLATE_ID in approved else None


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require(mapping: Mapping[str, Any], name: str, expected_type: type) -> Any:
    if name not in mapping:
        raise ValueError(f"missing required field: {name}")
    value = mapping[name]
    if not isinstance(value, expected_type):
        raise TypeError(f"{name} must be {expected_type.__name__}")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must be non-empty")
    return text


def _sha(value: Any, name: str) -> str:
    text = _nonempty(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _url(value: Any, name: str) -> str:
    text = _nonempty(value, name)
    if not text.startswith(("https://", "http://")):
        raise ValueError(f"{name} must be an http(s) evidence URL")
    return text


@dataclass(frozen=True)
class PremarketConfig:
    required_source_kinds: tuple[str, ...]
    source_freshness_minutes: int
    source_freshness_minutes_by_kind: dict[str, int]
    max_candidates: int
    min_liquidity_score: float
    min_catalyst_score: float
    approved_strategy_templates: tuple[str, ...]
    approved_short_strategy_templates: tuple[str, ...]
    allowed_symbols: tuple[str, ...]
    excluded_universe_classes: tuple[str, ...]
    weights: dict[str, float]
    primary_offset_minutes: int
    verification_offset_minutes: int
    freeze_offset_minutes: int
    # v1.3 (2026-10-02): NEWS counts only for symbols its title mentions
    # (see src/premarket/providers/marketaux.py). False = pre-v1.3 behaviour.
    news_title_relevance_required: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PremarketConfig:
        if raw.get("schema") != CONFIG_SCHEMA:
            raise ValueError(f"config schema must be {CONFIG_SCHEMA}")
        required = tuple(sorted({_nonempty(v, "required_source_kinds").upper()
                                 for v in _require(raw, "required_source_kinds", list)}))
        if not required or not set(required).issubset(ALLOWED_SOURCE_KINDS):
            raise ValueError("required_source_kinds contains an unsupported kind")
        weights_raw = _require(raw, "weights", dict)
        weights = {name: float(weights_raw.get(name, -1.0)) for name in SCORE_FIELDS}
        if any(value < 0 for value in weights.values()) or abs(sum(weights.values()) - 1.0) > 1e-9:
            raise ValueError("all score weights are required, non-negative, and must sum to 1")
        max_candidates = int(raw.get("max_candidates", 3))
        if not 1 <= max_candidates <= 3:
            raise ValueError("max_candidates must be between 1 and 3")
        freshness = int(raw.get("source_freshness_minutes", 180))
        if freshness <= 0:
            raise ValueError("source_freshness_minutes must be positive")
        freshness_by_kind_raw = raw.get("source_freshness_minutes_by_kind", {})
        if not isinstance(freshness_by_kind_raw, dict):
            raise TypeError("source_freshness_minutes_by_kind must be an object")
        freshness_by_kind: dict[str, int] = {}
        for key, value in freshness_by_kind_raw.items():
            kind = _nonempty(key, "source_freshness_minutes_by_kind key").upper()
            if kind not in ALLOWED_SOURCE_KINDS:
                raise ValueError("source_freshness_minutes_by_kind contains an unsupported kind")
            minutes = int(value)
            if minutes <= 0:
                raise ValueError("source_freshness_minutes_by_kind values must be positive")
            freshness_by_kind[kind] = minutes
        min_liquidity_score = float(raw.get("min_liquidity_score", 0.5))
        if not 0.0 <= min_liquidity_score <= 1.0:
            raise ValueError("min_liquidity_score must be in [0,1]")
        min_catalyst_score = float(raw.get("min_catalyst_score", 0.0))
        if not 0.0 <= min_catalyst_score <= 1.0:
            raise ValueError("min_catalyst_score must be in [0,1]")
        return cls(
            required_source_kinds=required,
            source_freshness_minutes=freshness,
            source_freshness_minutes_by_kind=freshness_by_kind,
            max_candidates=max_candidates,
            min_liquidity_score=min_liquidity_score,
            min_catalyst_score=min_catalyst_score,
            approved_strategy_templates=tuple(sorted({
                _nonempty(v, "approved_strategy_templates")
                for v in raw.get("approved_strategy_templates", [])
            })),
            approved_short_strategy_templates=tuple(sorted({
                _nonempty(v, "approved_short_strategy_templates")
                for v in raw.get("approved_short_strategy_templates", [])
            })),
            allowed_symbols=tuple(sorted({
                _nonempty(v, "allowed_symbols").upper()
                for v in raw.get("allowed_symbols", [])
            })),
            excluded_universe_classes=tuple(sorted({
                _nonempty(v, "excluded_universe_classes")
                for v in raw.get("excluded_universe_classes", [])
            })),
            weights=weights,
            primary_offset_minutes=int(raw.get("primary_offset_minutes", 90)),
            verification_offset_minutes=int(raw.get("verification_offset_minutes", 30)),
            freeze_offset_minutes=int(raw.get("freeze_offset_minutes", 20)),
            news_title_relevance_required=_flag(
                raw.get("news_title_relevance_required", False),
                "news_title_relevance_required",
            ),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        # Omitted while off so pre-v1.3 configs keep a byte-identical policy_config.
        if not data["news_title_relevance_required"]:
            del data["news_title_relevance_required"]
        return data


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def load_config(path: str | Path) -> PremarketConfig:
    config_path = Path(path)
    with open(config_path, encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise TypeError("config root must be an object")
    universe_path = raw.get("universe_path")
    if universe_path:
        universe_file = (config_path.parent / str(universe_path)).resolve()
        with open(universe_file, encoding="utf-8") as stream:
            universe = yaml.safe_load(stream)
        if not isinstance(universe, dict) or universe.get("market") != "US":
            raise ValueError("universe_path must point to a US universe object")
        excluded = set(raw.get("excluded_universe_classes", []))
        symbols = universe.get("symbols")
        if not isinstance(symbols, list):
            raise ValueError("universe symbols must be a list")
        raw = dict(raw)
        raw["allowed_symbols"] = [
            item["symbol"] for item in symbols
            if isinstance(item, dict) and item.get("class") not in excluded
        ]
    return PremarketConfig.from_mapping(raw)


def _unique_sorted(items: list[dict], id_field: str) -> list[dict]:
    unique = {canonical_bytes(item): item for item in items}
    return sorted(unique.values(), key=lambda item: (item[id_field], canonical_bytes(item)))


def normalize_input(raw: Mapping[str, Any]) -> dict:
    """Validate structural fields and return a deterministic normalized value.

    Point-in-time quality failures are evaluated separately so they can produce
    an auditable NO_TRADE artifact.  Missing/invalid schema fields are contract
    errors and stop the CLI.
    """
    schema = raw.get("schema")
    if schema not in {INPUT_SCHEMA, INPUT_SCHEMA_V2}:
        raise ValueError(f"input schema must be {INPUT_SCHEMA} or {INPUT_SCHEMA_V2}")
    if raw.get("market") != "US":
        raise ValueError("first PR supports market=US only")
    trading_day = date.fromisoformat(_nonempty(raw.get("trading_day"), "trading_day"))
    as_of = _nonempty(raw.get("as_of"), "as_of")
    parse_iso(as_of)
    generated_at = raw.get("generated_at")
    if schema == INPUT_SCHEMA_V2:
        generated_at = _nonempty(generated_at, "generated_at")
        parse_iso(generated_at)
    stage = raw.get("stage")
    if schema == INPUT_SCHEMA_V2:
        stage = _nonempty(stage, "stage").upper()
        if stage not in ALLOWED_STAGES:
            raise ValueError(f"stage must be one of {sorted(ALLOWED_STAGES)}")

    sources: list[dict] = []
    for item in _require(raw, "sources", list):
        if not isinstance(item, dict):
            raise TypeError("each source must be an object")
        kind = _nonempty(item.get("kind"), "source.kind").upper()
        if kind not in ALLOWED_SOURCE_KINDS:
            raise ValueError(f"unsupported source kind: {kind}")
        source = {
            "source_id": _nonempty(item.get("source_id"), "source.source_id"),
            "kind": kind,
            "status": _nonempty(item.get("status"), "source.status").upper(),
            "observed_at": _nonempty(item.get("observed_at"), "source.observed_at"),
            "available_at": _nonempty(item.get("available_at"), "source.available_at"),
            "ingested_at": _nonempty(item.get("ingested_at"), "source.ingested_at"),
            "source_url": _url(item.get("source_url"), "source.source_url"),
            "content_sha256": _sha(item.get("content_sha256"), "source.content_sha256"),
        }
        if source["status"] not in ALLOWED_SOURCE_STATUSES:
            raise ValueError(f"unsupported source status: {source['status']}")
        for field in ("observed_at", "available_at", "ingested_at"):
            parse_iso(source[field])
        sources.append(source)

    events: list[dict] = []
    for item in _require(raw, "events", list):
        if not isinstance(item, dict):
            raise TypeError("each event must be an object")
        symbols_raw = item.get("symbols", [])
        if not isinstance(symbols_raw, list):
            raise TypeError("event.symbols must be a list")
        event = {
            "event_id": _nonempty(item.get("event_id"), "event.event_id"),
            "source_id": _nonempty(item.get("source_id"), "event.source_id"),
            "published_at": _nonempty(item.get("published_at"), "event.published_at"),
            "available_at": _nonempty(item.get("available_at"), "event.available_at"),
            "ingested_at": _nonempty(item.get("ingested_at"), "event.ingested_at"),
            "source_url": _url(item.get("source_url"), "event.source_url"),
            "content_sha256": _sha(item.get("content_sha256"), "event.content_sha256"),
            "symbols": sorted({_nonempty(v, "event.symbols").upper() for v in symbols_raw}),
            "sentiment": float(item.get("sentiment", 0.0)),
        }
        if not -1.0 <= event["sentiment"] <= 1.0:
            raise ValueError("event.sentiment must be in [-1,1]")
        for field in ("published_at", "available_at", "ingested_at"):
            parse_iso(event[field])
        events.append(event)

    candidates: list[dict] = []
    for item in _require(raw, "candidates", list):
        if not isinstance(item, dict):
            raise TypeError("each candidate must be an object")
        evidence_ids_raw = item.get("evidence_event_ids", [])
        source_ids_raw = item.get("source_ids", [])
        if not isinstance(evidence_ids_raw, list):
            raise TypeError("candidate.evidence_event_ids must be a list")
        if not isinstance(source_ids_raw, list):
            raise TypeError("candidate.source_ids must be a list")
        candidate = {
            "symbol": _nonempty(item.get("symbol"), "candidate.symbol").upper(),
            "strategy_template_id": (
                _nonempty(item.get("strategy_template_id"), "candidate.strategy_template_id")
                if item.get("strategy_template_id") is not None else None
            ),
            "available_at": _nonempty(item.get("available_at"), "candidate.available_at"),
            "ingested_at": _nonempty(item.get("ingested_at"), "candidate.ingested_at"),
            "evidence_event_ids": sorted({
                _nonempty(v, "candidate.evidence_event_ids") for v in evidence_ids_raw
            }),
            "source_ids": sorted({_nonempty(v, "candidate.source_ids") for v in source_ids_raw}),
        }
        for field in SCORE_FIELDS:
            candidate[field] = float(item.get(field, -1.0))
            lower = -1.0 if field == "catalyst_score" else 0.0
            if not lower <= candidate[field] <= 1.0:
                raise ValueError(f"candidate.{field} must be in [{lower},1]")
        parse_iso(candidate["available_at"])
        parse_iso(candidate["ingested_at"])
        candidates.append(candidate)

    regime_raw = _require(raw, "market_regime", dict)
    regime_source_ids = regime_raw.get("source_ids", [])
    if not isinstance(regime_source_ids, list):
        raise TypeError("market_regime.source_ids must be a list")
    regime = {
        "label": _nonempty(regime_raw.get("label"), "market_regime.label").upper(),
        "available_at": _nonempty(regime_raw.get("available_at"), "market_regime.available_at"),
        "ingested_at": _nonempty(regime_raw.get("ingested_at"), "market_regime.ingested_at"),
        "source_ids": sorted({
            _nonempty(v, "market_regime.source_ids") for v in regime_source_ids
        }),
    }
    if regime["label"] not in ALLOWED_REGIMES:
        raise ValueError(f"market_regime.label must be one of {sorted(ALLOWED_REGIMES)}")
    parse_iso(regime["available_at"])
    parse_iso(regime["ingested_at"])

    return {
        "schema": schema,
        "market": "US",
        "trading_day": trading_day.isoformat(),
        "as_of": as_of,
        **({"stage": stage, "generated_at": generated_at} if schema == INPUT_SCHEMA_V2 else {}),
        "sources": _unique_sorted(sources, "source_id"),
        "events": _unique_sorted(events, "event_id"),
        "candidates": _unique_sorted(candidates, "symbol"),
        "market_regime": regime,
        **({"provenance": raw.get("provenance", {})} if schema == INPUT_SCHEMA_V2 and "provenance" in raw else {}),
    }


def quality_defects(data: Mapping[str, Any], schedule: PremarketSchedule,
                    config: PremarketConfig) -> list[str]:
    as_of = parse_iso(data["as_of"])
    generated_at = parse_iso(data["generated_at"]) if data.get("schema") == INPUT_SCHEMA_V2 else as_of
    defects: set[str] = set()
    if data["trading_day"] != schedule.trading_day:
        defects.add("TRADING_DAY_MISMATCH")
    if as_of < parse_iso(schedule.primary_run_utc):
        defects.add("BEFORE_PRIMARY_RUN")
    if as_of > parse_iso(schedule.freeze_utc):
        defects.add("AFTER_FREEZE_CUTOFF")
    if data.get("schema") == INPUT_SCHEMA_V2:
        expected_as_of = {
            "PRIMARY": schedule.primary_run_utc,
            "VERIFICATION": schedule.verification_utc,
            "FREEZE": schedule.freeze_utc,
        }.get(str(data.get("stage")))
        stage_end = {
            "PRIMARY": schedule.verification_utc,
            "VERIFICATION": schedule.freeze_utc,
            "FREEZE": schedule.market_open_utc,
        }.get(str(data.get("stage")))
        if expected_as_of is None or stage_end is None:
            defects.add("INVALID_STAGE")
        else:
            if data["as_of"] != expected_as_of:
                defects.add("STAGE_AS_OF_MISMATCH")
            if not (as_of <= generated_at < parse_iso(stage_end)):
                defects.add("GENERATED_AFTER_STAGE_WINDOW")
        if generated_at < as_of:
            defects.add("GENERATED_BEFORE_AS_OF")

    source_by_id: dict[str, dict] = {}
    duplicate_source_ids: set[str] = set()
    for source in data["sources"]:
        sid = source["source_id"]
        if sid in source_by_id and source_by_id[sid] != source:
            duplicate_source_ids.add(sid)
        source_by_id.setdefault(sid, source)
        observed = parse_iso(source["observed_at"])
        available = parse_iso(source["available_at"])
        ingested = parse_iso(source["ingested_at"])
        if observed > as_of or available > as_of or ingested > generated_at:
            defects.add("FUTURE_SOURCE_EVIDENCE")
        if available < observed or ingested < available:
            defects.add("INVALID_SOURCE_TIME_ORDER")
    if duplicate_source_ids:
        defects.add("CONFLICTING_SOURCE_REVISION")

    def source_freshness(source: Mapping[str, Any]) -> timedelta:
        kind = str(source.get("kind", "")).upper()
        minutes = config.source_freshness_minutes_by_kind.get(
            kind,
            config.source_freshness_minutes,
        )
        return timedelta(minutes=minutes)

    def source_usable(source_id: str, *, prefix: str) -> None:
        source = source_by_id.get(source_id)
        if source is None:
            defects.add(f"UNKNOWN_{prefix}_SOURCE")
            return
        if source["status"] != "OK":
            defects.add(f"UNHEALTHY_{prefix}_SOURCE")
        if as_of - parse_iso(source["observed_at"]) > source_freshness(source):
            defects.add(f"STALE_{prefix}_SOURCE")

    for kind in config.required_source_kinds:
        matching = [s for s in source_by_id.values() if s["kind"] == kind and s["status"] == "OK"]
        if not matching:
            defects.add(f"MISSING_OR_UNHEALTHY_{kind}_SOURCE")
        elif not any(as_of - parse_iso(s["observed_at"]) <= source_freshness(s) for s in matching):
            defects.add(f"STALE_{kind}_SOURCE")

    event_by_id: dict[str, dict] = {}
    for event in data["events"]:
        eid = event["event_id"]
        if eid in event_by_id and event_by_id[eid] != event:
            defects.add("CONFLICTING_EVENT_REVISION")
        event_by_id.setdefault(eid, event)
        if event["source_id"] not in source_by_id:
            defects.add("UNKNOWN_EVENT_SOURCE")
        else:
            source_usable(event["source_id"], prefix="EVENT")
        published, available, ingested = (
            parse_iso(event[field]) for field in ("published_at", "available_at", "ingested_at")
        )
        if published > as_of or available > as_of or ingested > generated_at:
            defects.add("FUTURE_EVENT_EVIDENCE")
        if available < published or ingested < available:
            defects.add("INVALID_EVENT_TIME_ORDER")

    known_events = set(event_by_id)
    seen_candidates: dict[str, dict] = {}
    for candidate in data["candidates"]:
        symbol = candidate["symbol"]
        if symbol in seen_candidates and seen_candidates[symbol] != candidate:
            defects.add("CONFLICTING_CANDIDATE_REVISION")
        seen_candidates.setdefault(symbol, candidate)
        if parse_iso(candidate["available_at"]) > as_of or parse_iso(candidate["ingested_at"]) > generated_at:
            defects.add("FUTURE_CANDIDATE_EVIDENCE")
        if parse_iso(candidate["ingested_at"]) < parse_iso(candidate["available_at"]):
            defects.add("INVALID_CANDIDATE_TIME_ORDER")
        if not set(candidate["evidence_event_ids"]).issubset(known_events):
            defects.add("UNKNOWN_CANDIDATE_EVENT")
        if not candidate["evidence_event_ids"]:
            defects.add("MISSING_CANDIDATE_EVENT")
        if not candidate["source_ids"]:
            defects.add("UNKNOWN_CANDIDATE_SOURCE")
        for source_id in candidate["source_ids"]:
            source_usable(source_id, prefix="CANDIDATE")
        if config.allowed_symbols and symbol not in config.allowed_symbols:
            defects.add("SYMBOL_OUTSIDE_ALLOWED_UNIVERSE")
        for event_id in candidate["evidence_event_ids"]:
            if event_id in event_by_id and symbol not in event_by_id[event_id]["symbols"]:
                defects.add("CANDIDATE_EVENT_SYMBOL_MISMATCH")

    regime = data["market_regime"]
    if parse_iso(regime["available_at"]) > as_of or parse_iso(regime["ingested_at"]) > generated_at:
        defects.add("FUTURE_REGIME_EVIDENCE")
    if parse_iso(regime["ingested_at"]) < parse_iso(regime["available_at"]):
        defects.add("INVALID_REGIME_TIME_ORDER")
    if not regime["source_ids"]:
        defects.add("UNKNOWN_REGIME_SOURCE")
    for source_id in regime["source_ids"]:
        source_usable(source_id, prefix="REGIME")
    return sorted(defects)


def build_snapshot(data: Mapping[str, Any], schedule: PremarketSchedule,
                   config: PremarketConfig) -> dict:
    normalized = normalize_input(data)
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "schedule": schedule.to_dict(),
        "policy_config": config.to_dict(),
        "input": normalized,
        "quality_defects": quality_defects(normalized, schedule, config),
    }
    digest = sha256_payload(payload)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "run_id": f"premarket-{schedule.trading_day}-{digest[:12]}",
        "snapshot_sha256": digest,
        "payload": payload,
    }
