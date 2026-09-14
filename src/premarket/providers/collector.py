"""Deterministic one-shot aggregation of isolated provider results."""
from __future__ import annotations

from typing import Any, Protocol

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector_types import CollectionContext
from src.premarket.providers.common import payload_sha256, result
from src.premarket.schedule import iso_z, parse_iso


class EvidenceProvider(Protocol):
    provider: str
    kind: str

    def collect(self, context: CollectionContext, raw_store: RawEvidenceStore) -> dict[str, Any]: ...


def _valid_provider_result(value: Any, provider: EvidenceProvider) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("provider") != provider.provider or value.get("kind") != provider.kind:
        return False
    if value.get("status") not in {"OK", "ERROR", "UNAVAILABLE"}:
        return False
    if not isinstance(value.get("source"), dict):
        return False
    if not isinstance(value.get("items"), list) or not isinstance(value.get("receipt_ids"), list):
        return False
    return value.get("error_code") is None or isinstance(value.get("error_code"), str)


def _provider_source_url(provider: EvidenceProvider) -> str:
    config = getattr(provider, "config", None)
    value = getattr(config, "base_url", None)
    return value if isinstance(value, str) else "https://invalid.local/provider-error"


def build_provider_bundle(
    context: CollectionContext,
    providers: list[EvidenceProvider],
    raw_store: RawEvidenceStore,
) -> dict[str, Any]:
    as_of = iso_z(parse_iso(context.as_of))
    if as_of[:10] != context.trading_day.isoformat():
        raise ValueError("as_of date must match trading_day")
    normalized_context = CollectionContext(
        trading_day=context.trading_day,
        as_of=as_of,
        symbols=tuple(sorted({symbol.strip().upper() for symbol in context.symbols if symbol.strip()})),
        stage=context.stage.strip().upper(),
        schedule=context.schedule,
        policy_config_sha256=context.policy_config_sha256,
    )
    if normalized_context.stage not in {"PRIMARY", "VERIFICATION", "FREEZE"}:
        raise ValueError("unsupported collection stage")
    collected = []
    for provider in providers:
        try:
            item = provider.collect(normalized_context, raw_store)
            if not _valid_provider_result(item, provider):
                item = result(
                    provider.provider,
                    provider.kind,
                    "ERROR",
                    _provider_source_url(provider),
                    normalized_context.as_of,
                    [],
                    [],
                    "INVALID_PROVIDER_RESULT",
                )
        except Exception as error:  # noqa: BLE001 - provider boundary must fail closed
            item = result(
                provider.provider,
                provider.kind,
                "ERROR",
                _provider_source_url(provider),
                normalized_context.as_of,
                [],
                [],
                f"UNEXPECTED_{type(error).__name__.upper()}",
            )
        collected.append(item)
    collected.sort(key=lambda item: item["provider"])
    defects = sorted(
        f"{item['provider']}:{item['error_code'] or item['status']}"
        for item in collected if item["status"] != "OK"
    )
    payload = {
        "schema": "premarket-provider-bundle/v1",
        "trading_day": normalized_context.trading_day.isoformat(),
        "as_of": normalized_context.as_of,
        "stage": normalized_context.stage,
        "schedule": normalized_context.schedule,
        "policy_config_sha256": normalized_context.policy_config_sha256,
        "symbols": list(normalized_context.symbols),
        "collection_status": "OK" if not defects else "FAIL_CLOSED",
        "collection_defects": defects,
        "capabilities": {
            "order_authority": False,
            "broker_access": False,
            "candidate_scoring": False,
            "market_regime_decision": False,
            "strategy_promotion": False,
        },
        "providers": collected,
        "raw_manifest": raw_store.manifest(),
    }
    identity = {
        "as_of": payload["as_of"],
        "policy_config_sha256": payload["policy_config_sha256"],
        "providers": [item["provider"] for item in collected],
        "raw_content_sha256": payload["raw_manifest"]["raw_content_sha256"],
        "stage": payload["stage"],
        "symbols": payload["symbols"],
        "trading_day": payload["trading_day"],
    }
    payload["run_id"] = (
        f"premarket-evidence-{payload['trading_day']}-"
        f"{payload['stage'].lower()}-{payload_sha256(identity)[:12]}"
    )
    payload["artifact_sha256"] = payload_sha256(payload)
    return payload


__all__ = ["CollectionContext", "build_provider_bundle"]
