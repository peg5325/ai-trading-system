#!/usr/bin/env python3
"""Combine provider and market bundles into premarket-input/v2."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.premarket.contract import attachable_template, load_config, quality_defects
from src.premarket.normalizer import normalize_bundles
from src.premarket.report import json_bytes, write_immutable
from src.premarket.schedule import build_premarket_schedule


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="provider+market bundles -> premarket-input/v2")
    parser.add_argument("--provider-bundle", required=True)
    parser.add_argument("--market-bundle", required=True)
    parser.add_argument("--config", default="config/premarket_daily_brief.yaml")
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args(argv)
    with open(args.provider_bundle, encoding="utf-8") as stream:
        provider_bundle = json.load(stream)
    with open(args.market_bundle, encoding="utf-8") as stream:
        market_bundle = json.load(stream)
    config = load_config(args.config)
    result = normalize_bundles(
        provider_bundle,
        market_bundle,
        strategy_template_id=attachable_template(config),
        news_title_relevance_required=config.news_title_relevance_required,
    )
    write_immutable(args.out_json, json_bytes(result))
    schedule = build_premarket_schedule(
        date.fromisoformat(result["trading_day"]),
        primary_offset_minutes=config.primary_offset_minutes,
        verification_offset_minutes=config.verification_offset_minutes,
        freeze_offset_minutes=config.freeze_offset_minutes,
    )
    defects = quality_defects(result, schedule, config)
    print(json.dumps({
        "artifact_sha256": result["artifact_sha256"],
        "candidate_count": len(result["candidates"]),
        "quality_defects": defects,
        "regime": result["market_regime"]["label"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not defects else 2


if __name__ == "__main__":
    raise SystemExit(main())
