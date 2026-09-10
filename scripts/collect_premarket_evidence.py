#!/usr/bin/env python3
"""Collect a read-only premarket-provider-bundle/v1 artifact once."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
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
from src.premarket.providers.config import load_provider_config
from src.premarket.providers.fred import FredProvider
from src.premarket.providers.http import BoundedHttpClient
from src.premarket.providers.marketaux import (
    MarketauxProvider,
    QuotaLedger,
)
from src.premarket.providers.sec_edgar import SecEdgarProvider
from src.premarket.report import json_bytes, write_immutable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Marketaux/SEC/FRED -> read-only premarket provider bundle"
    )
    parser.add_argument("--trading-day", required=True, help="NYSE trading day, YYYY-MM-DD")
    parser.add_argument("--as-of", required=True, help="point-in-time cutoff, ISO-8601 with offset")
    parser.add_argument("--provider-config", default="config/premarket_providers.yaml")
    parser.add_argument("--brief-config", default="config/premarket_daily_brief.yaml")
    parser.add_argument("--raw-root", default="data/premarket/providers/raw")
    parser.add_argument("--quota-root", default="data/premarket/providers/quota")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--no-dotenv", action="store_true", help="do not load project .env")
    args = parser.parse_args(argv)

    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    provider_config = load_provider_config(args.provider_config)
    brief_config = load_config(args.brief_config)
    context = CollectionContext(
        trading_day=date.fromisoformat(args.trading_day),
        as_of=args.as_of,
        symbols=brief_config.allowed_symbols,
    )
    raw_store = RawEvidenceStore(args.raw_root)
    http = BoundedHttpClient(provider_config.http)
    try:
        providers = [
            MarketauxProvider(
                provider_config.marketaux,
                http,
                QuotaLedger(args.quota_root),
                token=os.getenv("MARKETAUX_API_TOKEN", ""),
            ),
            SecEdgarProvider(
                provider_config.sec_edgar,
                http,
                user_agent=os.getenv("SEC_EDGAR_USER_AGENT", ""),
            ),
            FredProvider(
                provider_config.fred,
                http,
                api_key=os.getenv("FRED_API_KEY", ""),
            ),
        ]
        bundle = build_provider_bundle(context, providers, raw_store)
    finally:
        http.close()
    write_immutable(args.out_json, json_bytes(bundle))
    print(json.dumps({
        "artifact_sha256": bundle["artifact_sha256"],
        "collection_status": bundle["collection_status"],
        "provider_statuses": {
            item["provider"]: item["status"] for item in bundle["providers"]
        },
        "raw_content_sha256": bundle["raw_manifest"]["raw_content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
