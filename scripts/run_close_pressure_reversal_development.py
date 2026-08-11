"""사전등록된 P0-US30 장 마감 과잉압력 반전 development 단일 실행."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.daily_manifest import build_manifest  # noqa: E402
from src.research.close_pressure_reversal.contract import (  # noqa: E402
    canonical_json_bytes,
    load_contract,
)
from src.research.close_pressure_reversal.development import run_development  # noqa: E402
from src.research.close_pressure_reversal.loading import (  # noqa: E402
    load_development_bars,
    official_symbols,
)
from src.research.close_pressure_reversal.report import render_markdown  # noqa: E402


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

    current_manifest = build_manifest(
        args.db,
        data["universe_path"],
        collection_params={},
        collection_code_commit=None,
        generated_at_utc="identity-check-not-result",
        as_of=data["blind_oos_end_inclusive"],
    )
    identity_verified = bool(
        current_manifest["data_content_sha256"] == data["data_content_sha256"]
        and current_manifest["summary"]["verdict_counts"] == {"OK": 30}
        and current_manifest["summary"]["bars_beyond_as_of_total"] == 0
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
        data_identity_verified=identity_verified,
    )

    Path(args.out_json).write_bytes(canonical_json_bytes(result))
    Path(args.out_md).write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "blind_oos_opened": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
