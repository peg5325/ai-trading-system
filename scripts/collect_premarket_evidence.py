#!/usr/bin/env python3
"""Collect a read-only premarket-provider-bundle/v1 artifact once."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.premarket.contract import load_config
from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.collector import (
    CollectionContext,
    build_provider_bundle,
)
from src.premarket.providers.common import payload_sha256
from src.premarket.providers.config import load_provider_config
from src.premarket.providers.fred import FredProvider
from src.premarket.providers.http import BoundedHttpClient
from src.premarket.providers.marketaux import (
    MarketauxProvider,
    QuotaLedger,
)
from src.premarket.providers.schedule_gate import STAGES, validate_collection_window
from src.premarket.providers.sec_edgar import SecEdgarProvider
from src.premarket.report import json_bytes, write_immutable_pair


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _credential(primary: str, legacy: str) -> str:
    primary_value = os.getenv(primary, "").strip()
    legacy_value = os.getenv(legacy, "").strip()
    if primary_value and legacy_value and primary_value != legacy_value:
        raise ValueError(f"conflicting credential aliases: {primary} and {legacy}")
    return primary_value or legacy_value


def _priority_symbols(path: str | None, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if path is None:
        return allowed
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if isinstance(value, dict):
        value = value.get("symbols")
    if not isinstance(value, list):
        raise TypeError("priority symbols file must contain a JSON list or {symbols: [...]} object")
    symbols = tuple(sorted({str(item).strip().upper() for item in value if str(item).strip()}))
    if not symbols or not set(symbols).issubset(allowed):
        raise ValueError("priority symbols must be a non-empty subset of the configured universe")
    return symbols


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    parser = argparse.ArgumentParser(
        description="Marketaux/SEC/FRED -> read-only premarket provider bundle"
    )
    parser.add_argument("--trading-day", required=True, help="NYSE trading day, YYYY-MM-DD")
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--as-of", help="must equal the stage cutoff; omitted means derive it")
    parser.add_argument("--provider-config", default="config/premarket_providers.yaml")
    parser.add_argument("--brief-config", default="config/premarket_daily_brief.yaml")
    parser.add_argument("--raw-root", default="data/premarket/providers/raw")
    parser.add_argument("--quota-root", default="data/premarket/providers/quota")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--receipt", help="sanitized receipt JSON; defaults beside out-json")
    parser.add_argument("--provider", default="all", help="all or comma-separated marketaux,sec,fred")
    parser.add_argument("--priority-symbols-file")
    parser.add_argument("--dry-run", action="store_true", help="validate schedule/config without network")
    parser.add_argument("--no-dotenv", action="store_true", help="do not load project .env")
    args = parser.parse_args(argv)

    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    provider_config = load_provider_config(args.provider_config)
    brief_config = load_config(args.brief_config)
    trading_day = date.fromisoformat(args.trading_day)
    schedule, as_of = validate_collection_window(
        trading_day,
        args.stage,
        as_of=args.as_of,
        now=clock(),
        dry_run=args.dry_run,
        primary_offset_minutes=brief_config.primary_offset_minutes,
        verification_offset_minutes=brief_config.verification_offset_minutes,
        freeze_offset_minutes=brief_config.freeze_offset_minutes,
    )
    symbols = _priority_symbols(args.priority_symbols_file, brief_config.allowed_symbols)
    selected = {item.strip().lower() for item in args.provider.split(",") if item.strip()}
    if selected == {"all"}:
        selected = {"marketaux", "sec", "fred"}
    if not selected or not selected.issubset({"marketaux", "sec", "fred"}):
        raise ValueError("provider must be all or a subset of marketaux,sec,fred")
    if args.dry_run:
        print(json.dumps({
            "as_of": as_of,
            "network_calls": 0,
            "providers": sorted(selected),
            "schedule": schedule.to_dict(),
            "stage": args.stage,
            "symbol_count": len(symbols),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    context = CollectionContext(
        trading_day=trading_day,
        as_of=as_of,
        symbols=symbols,
        stage=args.stage,
        schedule=schedule.to_dict(),
        policy_config_sha256=payload_sha256(asdict(provider_config)),
    )
    raw_store = RawEvidenceStore(args.raw_root)
    http = BoundedHttpClient(provider_config.http, clock=clock)
    try:
        providers = []
        if "marketaux" in selected:
            providers.append(MarketauxProvider(
                provider_config.marketaux,
                http,
                QuotaLedger(args.quota_root, clock=clock),
                token=_credential("MARKETAUX_API_KEY", "MARKETAUX_API_TOKEN"),
            ))
        if "sec" in selected:
            providers.append(SecEdgarProvider(
                provider_config.sec_edgar,
                http,
                user_agent=_credential("SEC_USER_AGENT", "SEC_EDGAR_USER_AGENT"),
            ))
        if "fred" in selected:
            providers.append(FredProvider(
                provider_config.fred,
                http,
                api_key=os.getenv("FRED_API_KEY", ""),
            ))
        bundle = build_provider_bundle(context, providers, raw_store)
    finally:
        http.close()
    sanitized_receipt = {
        "artifact_sha256": bundle["artifact_sha256"],
        "collection_status": bundle["collection_status"],
        "provider_statuses": {
            item["provider"]: item["status"] for item in bundle["providers"]
        },
        "raw_content_sha256": bundle["raw_manifest"]["raw_content_sha256"],
        "run_id": bundle["run_id"],
        "schema": "premarket-provider-run-receipt/v1",
        "stage": args.stage,
        "trading_day": args.trading_day,
    }
    receipt_path = args.receipt or f"{args.out_json}.receipt.json"
    write_immutable_pair(
        (args.out_json, json_bytes(bundle)),
        (receipt_path, json_bytes(sanitized_receipt)),
    )
    print(json.dumps(sanitized_receipt, ensure_ascii=False, sort_keys=True))
    return 0 if bundle["collection_status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
