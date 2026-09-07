#!/usr/bin/env python3
"""Build a read-only Daily Brief from a normalized local JSON artifact."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.premarket.contract import load_config
from src.premarket.planner import build_daily_brief
from src.premarket.report import (
    json_bytes,
    render_markdown,
    write_immutable_pair,
)
from src.premarket.schedule import build_premarket_schedule


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalized local evidence -> read-only premarket Daily Brief"
    )
    parser.add_argument("--input", required=True, help="normalized premarket-input/v1 JSON")
    parser.add_argument("--config", default="config/premarket_daily_brief.yaml")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-markdown", required=True)
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as stream:
        data = json.load(stream)
    config = load_config(args.config)
    schedule = build_premarket_schedule(
        date.fromisoformat(data["trading_day"]),
        primary_offset_minutes=config.primary_offset_minutes,
        verification_offset_minutes=config.verification_offset_minutes,
        freeze_offset_minutes=config.freeze_offset_minutes,
    )
    result = build_daily_brief(data, schedule=schedule, config=config)
    write_immutable_pair(
        (args.out_json, json_bytes(result)),
        (args.out_markdown, render_markdown(result).encode("utf-8")),
    )
    print(json.dumps({
        "status": result["brief"]["status"],
        "run_id": result["brief"]["run_id"],
        "snapshot_sha256": result["brief"]["snapshot_sha256"],
        "watchlist_count": len(result["brief"]["watchlist"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
