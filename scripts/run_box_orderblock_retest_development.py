#!/usr/bin/env python3
"""Run the frozen exploratory box/order-block development study once."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.box_orderblock_retest.contract import (
    canonical_json_bytes,
    load_contract,
)
from src.research.box_orderblock_retest.development import (
    run_development,
)
from src.research.box_orderblock_retest.loading import (
    load_development_bars,
    official_symbols,
)
from src.research.box_orderblock_retest.report import render_markdown
from src.research.development_identity import verify_daily_identity

EXPECTED_DAILY_FULL_ROWS = 76_690
EXPECTED_DAILY_DEVELOPMENT_CONTENT = (
    "96151eeb277528e1b8167e7c8c531155640cd52973c0c2f646bd23ed11018a7a"
)
EXPECTED_DAILY_DEVELOPMENT_ROWS = 69_130
_REVIEW_URL = re.compile(
    r"https://github\.com/peg5325/ai-trading-system/pull/\d+#issuecomment-\d+"
)


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--review-evidence-url", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.producer_commit != _head():
        raise SystemExit("producer commit must equal current HEAD")
    if _REVIEW_URL.fullmatch(args.review_evidence_url) is None:
        raise SystemExit("review evidence must be a stable GitHub PR comment URL")
    contract = load_contract()
    data = contract["data"]
    identity = verify_daily_identity(
        daily_db_path=args.db,
        universe_yaml=data["universe_path"],
        daily_manifest_path=data["manifest_path"],
        expected_daily_full_content=data["full_data_content_sha256"],
        expected_daily_full_rows=EXPECTED_DAILY_FULL_ROWS,
        development_end=data["development_end_inclusive"],
        expected_daily_development_content=EXPECTED_DAILY_DEVELOPMENT_CONTENT,
        expected_daily_development_rows=EXPECTED_DAILY_DEVELOPMENT_ROWS,
    )
    symbols = official_symbols(data["universe_path"])
    if len(symbols) != int(data["official_symbol_count"]):
        raise SystemExit("official symbol count mismatch")
    bars = load_development_bars(
        args.db,
        symbols=symbols,
        start=date.fromisoformat(data["development_start"]),
        end_inclusive=date.fromisoformat(data["development_end_inclusive"]),
    )
    result = run_development(
        bars,
        contract=contract,
        identity=identity,
        producer_commit=args.producer_commit,
        review_evidence_url=args.review_evidence_url,
    )
    Path(args.out_json).write_bytes(canonical_json_bytes(result))
    Path(args.out_md).write_text(render_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": result["verdict"],
                "primary_trades": result["arms"]["primary"]["cost_levels"][
                    "official"
                ]["trade_count"],
                "blind_oos_opened": result["blind_oos_opened"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
