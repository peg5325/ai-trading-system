#!/usr/bin/env python3
"""Collect the last completed NYSE day of US 1m bars for the full P0-US30 universe."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.premarket.collection import DEFAULT_MAX_PAGES, run_history_collection


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="daily P0-US30 1m history collection")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    result = run_history_collection(root=ROOT, now=now, max_pages=args.max_pages)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"COMPLETE", "SKIPPED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
