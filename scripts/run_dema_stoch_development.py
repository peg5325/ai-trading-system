#!/usr/bin/env python
"""Run the frozen DEMA/Stochastic development-only backtest once."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.dema_stoch.development import run, write_result
from src.research.dema_stoch.report import render_report


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--review-evidence-url", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args(argv)
    if args.producer_commit != _head():
        parser.error("producer commit must equal current HEAD")
    if "github.com/peg5325/ai-trading-system/pull/" not in args.review_evidence_url:
        parser.error("review evidence must be a stable GitHub PR comment URL")
    result = run(
        args.snapshot_db,
        args.raw_root,
        producer_commit=args.producer_commit,
        review_evidence_url=args.review_evidence_url,
    )
    write_result(result, args.output_json)
    Path(args.output_md).write_text(render_report(result), encoding="utf-8")
    print(
        "DEMA/Stochastic development complete "
        f"verdict={result['gate']['verdict']} trades={result['trade_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

