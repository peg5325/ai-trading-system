#!/usr/bin/env python3
"""Run one scheduled FREEZE Paper session after the regular close (Paper only, no orders)."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.freeze_paper.runner import (
    COMPLETE_STATUS,
    DRY_RUN,
    NOT_ACTIVATED,
    SKIPPED,
    run_paper_session,
)
from src.premarket.operations import trading_day_from_clock

OK_STATUSES = {COMPLETE_STATUS, SKIPPED, NOT_ACTIVATED, DRY_RUN}


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="FREEZE Paper session runner (post-close)")
    parser.add_argument("--trading-day", help="NYSE day YYYY-MM-DD; default is current ET date")
    parser.add_argument("--reports-root", type=Path, default=ROOT / "reports",
                        help="reports directory; its parent must hold data/, docs/ and config/")
    parser.add_argument("--dry-run", action="store_true",
                        help="no backfill and no writes; print the computed result")
    parser.add_argument("--recompute", action="store_true",
                        help="re-derive an existing session from the current DB (no backfill); "
                             "a difference is written to the .drift.json file only")
    parser.add_argument("--no-dotenv", action="store_true")
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv(ROOT / ".env")
    trading_day = date.fromisoformat(args.trading_day) if args.trading_day else trading_day_from_clock(now)
    result = run_paper_session(trading_day=trading_day, root=args.reports_root.parent, now=now,
                               dry_run=args.dry_run, recompute=args.recompute)
    summary = {k: v for k, v in result.items() if k != "session"}
    if "session" in result:
        summary["session_status"] = result["session"]["status"]
        summary["session_artifact_sha256"] = result["session"]["artifact_sha256"]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in OK_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
