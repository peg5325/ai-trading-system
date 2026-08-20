"""사전등록된 P0-US30 장 마감 과잉압력 반전 development 단일 실행."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.close_pressure_reversal.contract import (
    canonical_json_bytes,
    load_contract,
)
from src.research.close_pressure_reversal.development import run_development
from src.research.close_pressure_reversal.loading import (
    load_development_bars,
    official_symbols,
)
from src.research.close_pressure_reversal.report import render_markdown
from src.research.development_identity import verify_daily_identity

EXPECTED_DAILY_FULL_ROWS = 76_690
EXPECTED_DAILY_DEVELOPMENT_CONTENT = (
    "96151eeb277528e1b8167e7c8c531155640cd52973c0c2f646bd23ed11018a7a"
)
EXPECTED_DAILY_DEVELOPMENT_ROWS = 69_130


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P0-US30 close-pressure reversal development-only backtest"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--contract",
        default="docs/research/PREREG_P0_US30_CLOSE_PRESSURE_REVERSAL_V1.json",
    )
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    contract = load_contract(args.contract)
    data = contract["data"]

    identity = verify_daily_identity(
        daily_db_path=args.db,
        universe_yaml=data["universe_path"],
        daily_manifest_path=data["manifest_path"],
        expected_daily_full_content=data["data_content_sha256"],
        expected_daily_full_rows=EXPECTED_DAILY_FULL_ROWS,
        development_end=data["development_end_inclusive"],
        expected_daily_development_content=EXPECTED_DAILY_DEVELOPMENT_CONTENT,
        expected_daily_development_rows=EXPECTED_DAILY_DEVELOPMENT_ROWS,
    )

    symbols = official_symbols(data["universe_path"])
    if len(symbols) != data["official_symbol_count"]:
        raise RuntimeError("공식 비레버리지/비인버스 유니버스 수 불일치")
    bars = load_development_bars(
        args.db,
        symbols=symbols,
        start=date.fromisoformat(data["development_start"]),
        end_inclusive=date.fromisoformat(data["development_end_inclusive"]),
    )
    result = run_development(
        bars,
        contract=contract,
        data_identity=identity,
    )

    Path(args.out_json).write_bytes(canonical_json_bytes(result))
    Path(args.out_md).write_text(render_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": result["verdict"],
                "blind_oos_opened": result["blind_oos_opened"],
                "current_execution_blind_oos_ohlcv_accessed": result[
                    "current_execution_blind_oos_ohlcv_accessed"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
