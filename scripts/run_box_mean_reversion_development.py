"""P0-US30 박스권 RSI·거래량 평균회귀 development-only 실행."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.research.box_mean_reversion.development import run, write_result  # noqa: E402
from src.research.box_mean_reversion.report import render  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--md-out", required=True)
    args = parser.parse_args()
    result = run(args.snapshot, args.raw_root)
    write_result(result, args.json_out)
    Path(args.md_out).write_text(render(result), encoding="utf-8")
    print(f"verdict={result['gate']['verdict']} trades={result['trade_count']} "
          f"payload={result['payload_sha256']}")


if __name__ == "__main__":
    main()

