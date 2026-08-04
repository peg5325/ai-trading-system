#!/usr/bin/env python
"""Freeze the exact prior-close/RVOL dependency repair target set."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gap_rvol.contract import load_contract, repo_root, require
from src.research.gap_rvol.dependency_repair import (
    build_dependency_plan,
    build_dependency_targets,
    plan_bytes,
    render_dependency_plan,
    sha256_path,
)
from src.research.gap_rvol.loading import load_symbol_day
from src.research.gap_rvol.report import write_evidence_file


def _safe_output(path: str, suffix: str) -> str:
    evidence = (Path(repo_root()) / "docs" / "research").resolve()
    target = Path(path).resolve()
    if target.parent != evidence or target.suffix != suffix:
        raise ValueError(f"output must be {suffix} directly under docs/research")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument(
        "--audit", default="docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json"
    )
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--generated-at-utc")
    args = parser.parse_args(argv)

    output_json = _safe_output(args.output_json, ".json")
    output_md = _safe_output(args.output_md, ".md")
    audit_path = str(Path(args.audit).resolve())
    snapshot_db = str(Path(args.snapshot_db).resolve())
    if not Path(snapshot_db).is_file():
        raise ValueError("frozen snapshot DB is missing")
    with open(audit_path, encoding="utf-8") as handle:
        audit = json.load(handle)
    contract = load_contract()
    targets = build_dependency_targets(
        audit=audit,
        contract=contract,
        loader=lambda symbol, day: load_symbol_day(snapshot_db, symbol, day),
    )
    plan = build_dependency_plan(
        targets=targets,
        audit_sha256=sha256_path(audit_path),
        prereg_sha256=sha256_path(
            os.path.join(repo_root(), "docs/research/PREREG_P0_US30_GAP_RVOL_V1.json")
        ),
        snapshot_identity=require(contract, "data_identity"),
        generated_at_utc=(
            args.generated_at_utc
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
        ),
    )
    write_evidence_file(output_json, plan_bytes(plan))
    write_evidence_file(output_md, render_dependency_plan(plan).encode("utf-8"))
    print(f"dependency targets frozen: {len(targets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
