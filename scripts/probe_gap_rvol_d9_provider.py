#!/usr/bin/env python
"""Probe Toss availability for the frozen Gap+RVOL D9 missing window."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from src.config import load_settings
from src.history.daily_manifest import read_p0_universe
from src.providers.toss_client import TossClient
from src.research.gap_rvol.contract import repo_root
from src.research.gap_rvol.provider_probe import (
    PROBE_DAYS,
    build_report,
    report_bytes,
    request_before,
    retry_after_seconds,
    summarize_page,
)
from src.research.gap_rvol.report import write_evidence_file


def _safe_output(path: str) -> str:
    root = Path(repo_root()).resolve()
    evidence = (root / "docs" / "research").resolve()
    target = Path(path).resolve()
    if target.parent != evidence or target.suffix != ".json":
        raise ValueError("provider probe output must be JSON directly under docs/research")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--spacing", type=float, default=0.4)
    parser.add_argument("--generated-at-utc")
    args = parser.parse_args(argv)
    if args.spacing < 0:
        raise ValueError("spacing must be nonnegative")

    output = _safe_output(args.output)
    settings = load_settings()
    if not settings.has_credentials:
        raise RuntimeError("Toss credentials are unavailable")
    universe = read_p0_universe(
        os.path.join(repo_root(), "config", "universe_p0_us30.yaml")
    )
    if not universe["valid"]:
        raise ValueError(f"P0 universe is invalid: {universe['errors']}")
    symbols = tuple(sorted(str(symbol) for symbol in universe["symbols"]))
    observations = []
    http_429 = 0
    with TossClient(settings) as client:
        for target_day in PROBE_DAYS:
            for symbol in symbols:
                for attempt in range(6):
                    try:
                        result = client.get_candles(
                            symbol,
                            "1m",
                            count=200,
                            before=request_before(target_day),
                        )
                        break
                    except httpx.HTTPStatusError as error:
                        if error.response is None or error.response.status_code != 429:
                            raise
                        http_429 += 1
                        if attempt == 5:
                            raise
                        retry_after = error.response.headers.get("Retry-After")
                        wait = retry_after_seconds(
                            retry_after,
                            now=datetime.now(timezone.utc),
                        )
                        if wait is None:
                            wait = 2 ** (attempt + 1)
                        time.sleep(max(wait, 1.0))
                observations.append(summarize_page(
                    symbol=symbol,
                    target_day=target_day,
                    result=result,
                ))
                if args.spacing:
                    time.sleep(args.spacing)
    report = build_report(
        symbols=symbols,
        observations=observations,
        generated_at_utc=(
            args.generated_at_utc or datetime.now(timezone.utc).isoformat()
        ),
        http_429=http_429,
    )
    write_evidence_file(output, report_bytes(report))
    print(f"provider probe complete: {len(observations)} calls, 429={http_429}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
