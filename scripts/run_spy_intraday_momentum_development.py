"""SPY first-to-last half-hour intraday momentum development-only runner."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.research.spy_intraday_momentum.development import run, write_result  # noqa: E402
from src.research.spy_intraday_momentum.report import render  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--daily-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()
    result = run(args.snapshot, args.daily_db, args.raw_root)
    write_result(result, args.json_out)
    Path(args.md_out).write_text(render(result), encoding="utf-8")
    print(
        f"verdict={result['gate']['verdict']} trades={result['trade_count']} "
        f"payload={result['payload_sha256']}"
    )


if __name__ == "__main__":
    main()
