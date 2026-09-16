#!/usr/bin/env python3
"""Collect read-only Toss candles for a provider bundle cutoff."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.premarket.market_snapshot import build_market_bundle, write_market_bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toss candles -> premarket-market-bundle/v1")
    parser.add_argument("--provider-bundle", required=True)
    parser.add_argument("--config", default="config/premarket_daily_brief.yaml")
    parser.add_argument("--raw-root", default="data/premarket/market/raw")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--receipt")
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    with open(args.provider_bundle, encoding="utf-8") as stream:
        provider_bundle = json.load(stream)
    bundle = build_market_bundle(provider_bundle, raw_root=args.raw_root, config_path=args.config)
    receipt = write_market_bundle(bundle, args.out_json, args.receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if bundle["collection_status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
