#!/usr/bin/env python
"""Collect the 265 frozen Toss-available Gap+RVOL D9 dependencies."""
from __future__ import annotations

import argparse
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
from src.research.gap_rvol.dependency_candidate import (
    build_dependency_candidate_manifest,
    build_dependency_candidate_targets,
    live_candidate_hash,
    load_tracked_dependency_probe,
    manifest_bytes,
    render_dependency_candidate_manifest,
    summarize_dependency_collection,
)
from src.research.gap_rvol.dependency_repair import load_frozen_dependency_plan
from src.research.gap_rvol.provider_repair import collect_target_day, persist_collection
from src.research.gap_rvol.report import write_evidence_file


def _under_candidate_root(path: str) -> str:
    root = Path(repo_root()).resolve()
    target = Path(path).resolve()
    allowed = (
        root / "data" / "repair_candidates" / "gap-rvol-d9-toss-dependencies"
    ).resolve()
    if allowed not in target.parents:
        raise ValueError("candidate data must remain below the dependency repair root")
    return str(target)


def _evidence(path: str, suffix: str) -> str:
    target = Path(path).resolve()
    root = (Path(repo_root()) / "docs" / "research").resolve()
    if target.parent != root or target.suffix != suffix:
        raise ValueError(f"evidence must be {suffix} directly under docs/research")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = repo_root()
    parser.add_argument(
        "--plan",
        default=os.path.join(
            root, "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json"
        ),
    )
    parser.add_argument(
        "--probe",
        default=os.path.join(
            root, "docs/research/PROBE_P0_US30_GAP_RVOL_D9_DEPENDENCIES_TOSS.json"
        ),
    )
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--spacing", type=float, default=0.4)
    parser.add_argument("--generated-at-utc")
    args = parser.parse_args(argv)
    if args.spacing < 0:
        parser.error("--spacing must be nonnegative")

    candidate_db = _under_candidate_root(args.candidate_db)
    raw_root = _under_candidate_root(args.raw_root)
    output_json = _evidence(args.output_json, ".json")
    output_md = _evidence(args.output_md, ".md")
    plan = load_frozen_dependency_plan(args.plan)
    probe = load_tracked_dependency_probe(args.probe)
    targets = build_dependency_candidate_targets(plan=plan, probe=probe)
    settings = load_settings()
    if not settings.has_credentials:
        raise RuntimeError("Toss credentials are unavailable")
    os.makedirs(os.path.dirname(candidate_db), exist_ok=True)
    raw_store = RawSnapshotStore(raw_root)
    session_filter = SessionFilter.default()
    summaries = []
    with HistoryStore(candidate_db) as store, TossMarketDataProvider(settings) as provider:
        for index, target in enumerate(targets, start=1):
            collection = collect_target_day(
                provider,
                raw_store,
                target.as_repair_target(),
                spacing=args.spacing,
            )
            persist_collection(store, collection, session_filter)
            summary = summarize_dependency_collection(collection, target)
            summaries.append(summary)
            print(
                f"[{index}/{len(targets)}] {target.day} {target.symbol} "
                f"rows={len(collection.candles)} "
                f"eligible={summary['eligible_dependency']}",
                file=sys.stderr,
            )
        manifest = build_dependency_candidate_manifest(
            targets=targets,
            summaries=summaries,
            raw_manifest=raw_store.manifest(),
            repair_db_content_sha256=live_candidate_hash(store),
            repair_db_rows=store.total_rows(),
            generated_at_utc=(
                args.generated_at_utc
                or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"
                )
            ),
        )
    write_evidence_file(output_json, manifest_bytes(manifest))
    write_evidence_file(
        output_md, render_dependency_candidate_manifest(manifest).encode("utf-8")
    )
    print(
        "dependency candidate collection complete "
        f"targets={manifest['target_count']} "
        f"eligible={manifest['eligible_dependency_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
