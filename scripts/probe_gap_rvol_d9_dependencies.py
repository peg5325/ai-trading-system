#!/usr/bin/env python
"""Probe current Toss availability for the frozen D9 dependency targets."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from src.config import load_settings
from src.providers.toss_client import TossClient
from src.research.gap_rvol.contract import repo_root
from src.research.gap_rvol.dependency_repair import (
    build_dependency_probe,
    load_frozen_dependency_plan,
    probe_bytes,
    probe_observation,
    render_dependency_probe,
)
from src.research.gap_rvol.provider_probe import request_before, retry_after_seconds
from src.research.gap_rvol.report import write_evidence_file


def _safe_output(path: str, suffix: str) -> str:
    evidence = (Path(repo_root()) / "docs" / "research").resolve()
    target = Path(path).resolve()
    if target.parent != evidence or target.suffix != suffix:
        raise ValueError(f"output must be {suffix} directly under docs/research")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--spacing", type=float, default=0.4)
    parser.add_argument("--generated-at-utc")
    args = parser.parse_args(argv)
    if args.spacing < 0:
        raise ValueError("spacing must be nonnegative")
    output_json = _safe_output(args.output_json, ".json")
    output_md = _safe_output(args.output_md, ".md")
    plan_path = Path(args.plan).resolve()
    expected_plan = (
        Path(repo_root())
        / "docs"
        / "research"
        / "MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json"
    ).resolve()
    if plan_path != expected_plan:
        raise ValueError("probe plan must be the tracked dependency manifest")
    plan = load_frozen_dependency_plan(str(plan_path))
    settings = load_settings()
    if not settings.has_credentials:
        raise RuntimeError("Toss credentials are unavailable")

    observations = []
    http_429 = 0
    with TossClient(settings) as client:
        for target in plan["targets"]:
            symbol = str(target["symbol"])
            target_day = date.fromisoformat(str(target["day"]))
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
                        retry_after, now=datetime.now(timezone.utc)
                    )
                    if wait is None:
                        wait = 2 ** (attempt + 1)
                    time.sleep(max(wait, 1.0))
            observations.append(probe_observation(
                symbol=symbol, target_day=target_day, result=result
            ))
            if args.spacing:
                time.sleep(args.spacing)

    report = build_dependency_probe(
        plan=plan,
        observations=observations,
        generated_at_utc=(
            args.generated_at_utc
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
        ),
        http_429=http_429,
    )
    write_evidence_file(output_json, probe_bytes(report))
    write_evidence_file(output_md, render_dependency_probe(report).encode("utf-8"))
    print(
        f"dependency probe complete: {len(observations)} calls, "
        f"available={report['available_count']}, 429={http_429}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
