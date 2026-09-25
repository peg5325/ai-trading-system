#!/usr/bin/env python3
"""Run one local premarket stage or the post-close diagnostic without an agent."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.premarket.operations import run_postclose, run_stage, trading_day_from_clock


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministic local premarket operation runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stage", choices=("PRIMARY", "VERIFICATION", "FREEZE"))
    group.add_argument("--postclose", action="store_true")
    parser.add_argument("--trading-day", help="NYSE day YYYY-MM-DD; default is current ET date")
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    trading_day = date.fromisoformat(args.trading_day) if args.trading_day else trading_day_from_clock(now)
    result = (
        run_postclose(trading_day=trading_day, root=ROOT)
        if args.postclose
        else run_stage(trading_day=trading_day, stage=args.stage, root=ROOT)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"COMPLETE", "SKIPPED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
