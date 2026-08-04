#!/usr/bin/env python
"""Collect isolated Toss repair candidates for tracked Gap+RVOL D9 exclusions."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings
from src.history.raw_snapshot import RawSnapshotStore
from src.history.session_filter import SessionFilter
from src.history.store import HistoryStore
from src.providers.toss_market_data import TossMarketDataProvider
from src.research.gap_rvol.contract import repo_root
from src.research.gap_rvol.provider_repair import (
    build_manifest,
    build_repair_targets,
    collect_target_day,
    data_content_sha256,
    manifest_bytes,
    persist_collection,
    render_manifest_report,
    summarize_collection,
)
from src.research.gap_rvol.report import write_evidence_file


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _under_data(path: str) -> str:
    root = Path(repo_root()).resolve()
    target = Path(path).resolve()
    data = (root / "data" / "repair_candidates" / "gap-rvol-d9-toss").resolve()
    if data not in target.parents:
        raise ValueError("candidate DB/raw root must be below data/repair_candidates/gap-rvol-d9-toss")
    return str(target)


def _evidence_path(path: str, suffix: str) -> str:
    root = Path(repo_root()).resolve()
    target = Path(path).resolve()
    if target.parent != (root / "docs" / "research").resolve() or target.suffix != suffix:
        raise ValueError(f"evidence must be {suffix} directly under docs/research")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = repo_root()
    parser.add_argument(
        "--audit",
        default=os.path.join(root, "docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json"),
    )
    parser.add_argument(
        "--probe",
        default=os.path.join(root, "docs/research/PROBE_P0_US30_GAP_RVOL_D9_TOSS.json"),
    )
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--output-md")
    parser.add_argument("--spacing", type=float, default=0.4)
    parser.add_argument("--generated-at-utc")
    args = parser.parse_args(argv)
    if args.spacing < 0:
        parser.error("--spacing must be nonnegative")

    candidate_db = _under_data(args.candidate_db)
    raw_root = _under_data(args.raw_root)
    output = _evidence_path(args.output, ".json")
    output_md = _evidence_path(args.output_md, ".md") if args.output_md else None
    settings = load_settings()
    if not settings.has_credentials:
        raise RuntimeError("Toss credentials are unavailable")
    targets = build_repair_targets(audit=_load(args.audit), probe=_load(args.probe))
    if not targets:
        raise RuntimeError("no tracked repair targets")
    os.makedirs(os.path.dirname(candidate_db), exist_ok=True)
    raw_store = RawSnapshotStore(raw_root)
    session_filter = SessionFilter.default()
    summaries = []
    with HistoryStore(candidate_db) as store, TossMarketDataProvider(settings) as provider:
        for index, target in enumerate(targets, start=1):
            collection = collect_target_day(
                provider, raw_store, target, spacing=args.spacing
            )
            persist_collection(store, collection, session_filter)
            summaries.append(summarize_collection(collection))
            print(
                f"[{index}/{len(targets)}] {target.day} {target.symbol} "
                f"rows={len(collection.candles)} pages={collection.pages_fetched}",
                file=sys.stderr,
            )
        manifest = build_manifest(
            targets=targets,
            summaries=summaries,
            raw_manifest=raw_store.manifest(),
            repair_db_content_sha256=data_content_sha256(store),
            repair_db_rows=store.total_rows(),
            generated_at_utc=(
                args.generated_at_utc or datetime.now(timezone.utc).isoformat()
            ),
        )
    write_evidence_file(output, manifest_bytes(manifest))
    if output_md:
        write_evidence_file(output_md, render_manifest_report(manifest).encode("utf-8"))
    print(
        f"repair candidate collection complete: targets={len(targets)} "
        f"full={manifest['full_regular_session_count']} "
        f"required={manifest['required_feature_minutes_complete_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
