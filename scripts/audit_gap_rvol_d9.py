#!/usr/bin/env python
"""Rebuild Gap+RVOL D9 evidence from the frozen development snapshot only."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gap_rvol.contract import load_contract, repo_root
from src.research.gap_rvol.d9_audit import (
    audit_json_bytes,
    build_d9_audit,
    render_d9_audit,
)
from src.research.gap_rvol.development import (
    build_development_feature_pass,
    tracked_manifest_path,
)
from src.research.gap_rvol.identity import require_identity, verify_inputs
from src.research.gap_rvol.manifest import SnapshotDefectEvidence
from src.research.gap_rvol.report import (
    validate_evidence_output_paths,
    write_evidence_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args(argv)

    root = repo_root()
    output_json, output_md = validate_evidence_output_paths(
        output_json=args.output_json,
        output_md=args.output_md,
        evidence_root=os.path.join(root, "docs", "research"),
        snapshot_path=args.snapshot_db,
        raw_root=args.raw_root,
    )
    contract = load_contract()
    manifest_path = tracked_manifest_path()
    identity = verify_inputs(
        contract=contract,
        snapshot_path=args.snapshot_db,
        raw_root=args.raw_root,
        manifest_path=manifest_path,
    )
    require_identity(identity)
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    with open(
        os.path.join(root, "docs/research/RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.json"),
        encoding="utf-8",
    ) as handle:
        tracked_result = json.load(handle)
    defects = SnapshotDefectEvidence.from_sources(
        manifest=manifest,
        identity=identity,
        contract=contract,
    )
    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path=args.snapshot_db,
    )
    audit = build_d9_audit(
        feature_pass=feature_pass,
        tracked_result=tracked_result,
        snapshot_defects=defects,
        contract=contract,
        generated_at_utc=args.generated_at_utc,
    )
    write_evidence_file(output_json, audit_json_bytes(audit))
    write_evidence_file(output_md, render_d9_audit(audit).encode("utf-8"))
    print(
        "Gap+RVOL D9 audit complete "
        f"corrected_verdict={audit['corrected_d9']['verdict']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
