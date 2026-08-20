"""사전등록된 P0 섹터 ETF 주간 상대강도 development 실행."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.daily_manifest import build_manifest  # noqa: E402
from src.research.sector_weekly_momentum.contract import (  # noqa: E402
    canonical_json_bytes,
    load_contract,
)
from src.research.sector_weekly_momentum.development import run_development  # noqa: E402
from src.research.sector_weekly_momentum.loading import (  # noqa: E402
    load_development_bars,
    sector_symbols,
)
from src.research.sector_weekly_momentum.report import render_markdown  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P0 sector ETF weekly relative-strength development"
    )
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--contract",
        default="docs/research/PREREG_P0_SECTOR_ETF_WEEKLY_MOMENTUM_V1.json",
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
    symbols = sector_symbols(data["universe_path"])
    identity_verified = bool(
        list(symbols) == data["expected_symbols"]
        and current_manifest["data_content_sha256"] == data["data_content_sha256"]
        and current_manifest["summary"]["verdict_counts"] == {"OK": 30}
        and current_manifest["summary"]["bars_beyond_as_of_total"] == 0
    )
    bars = load_development_bars(
        args.db,
        symbols=symbols,
        calendar_symbol=data["calendar_symbol"],
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
