#!/usr/bin/env python
"""Recompute only Gap+RVOL D9 with the Toss dependency candidate overlay."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gap_rvol.contract import load_contract, repo_root
from src.research.gap_rvol.d9_overlay import (
    audit_bytes,
    build_overlay_audit,
    render_overlay_audit,
)
from src.research.gap_rvol.dependency_candidate import (
    build_dependency_candidate_targets,
    load_tracked_dependency_probe,
    verify_dependency_candidate_identity,
)
from src.research.gap_rvol.dependency_overlay import dependency_overlay_loader
from src.research.gap_rvol.dependency_repair import load_frozen_dependency_plan
from src.research.gap_rvol.development import (
    build_development_feature_pass,
    tracked_manifest_path,
)
from src.research.gap_rvol.identity import verify_inputs
from src.research.gap_rvol.report import (
    validate_evidence_output_paths,
    write_evidence_file,
)


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--frozen-raw-root", required=True)
    parser.add_argument("--candidate-db", required=True)
    parser.add_argument("--candidate-raw-root", required=True)
    parser.add_argument("--candidate-manifest", required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--review-evidence-url", required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args(argv)
    if args.producer_commit != _head():
        parser.error("producer commit must equal current HEAD")
    if "github.com/peg5325/ai-trading-system/pull/" not in args.review_evidence_url:
        parser.error("review evidence must be a stable GitHub PR comment URL")

    output_json, output_md = validate_evidence_output_paths(
        output_json=args.output_json,
        output_md=args.output_md,
        evidence_root=os.path.join(repo_root(), "docs", "research"),
        snapshot_path=args.snapshot_db,
        raw_root=args.frozen_raw_root,
    )
    contract = load_contract()
    frozen = verify_inputs(
        contract=contract,
        snapshot_path=args.snapshot_db,
        raw_root=args.frozen_raw_root,
        manifest_path=tracked_manifest_path(),
    )
    frozen_identity = {check.name: str(check.actual) for check in frozen.checks}
    manifest, candidate_identity = verify_dependency_candidate_identity(
        manifest_path=args.candidate_manifest,
        candidate_db=args.candidate_db,
        candidate_raw_root=args.candidate_raw_root,
    )
    plan_path = os.path.join(
        repo_root(), "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json"
    )
    probe_path = os.path.join(
        repo_root(),
        "docs/research/PROBE_P0_US30_GAP_RVOL_D9_DEPENDENCIES_TOSS.json",
    )
    expected = {
        (target.symbol, target.day.isoformat())
        for target in build_dependency_candidate_targets(
            plan=load_frozen_dependency_plan(plan_path),
            probe=load_tracked_dependency_probe(probe_path),
        )
    }
    actual = {
        (str(item["symbol"]), str(item["day"])) for item in manifest["targets"]
    }
    if actual != expected:
        parser.error("candidate manifest differs from frozen Toss-available targets")

    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path=args.snapshot_db,
        loader=dependency_overlay_loader(
            candidate_db=args.candidate_db,
            eligible_targets=candidate_identity.eligible_targets,
        ),
    )
    baseline = _read(os.path.join(
        repo_root(), "docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json"
    ))
    audit = build_overlay_audit(
        feature_pass=feature_pass,
        baseline_audit=baseline,
        candidate_identity=candidate_identity,
        contract=contract,
        frozen_identity=frozen_identity,
        producer_commit=args.producer_commit,
        review_evidence_url=args.review_evidence_url,
        generated_at_utc=args.generated_at_utc,
    )
    audit["schema"] = "gap-rvol-d9-dependency-overlay-audit/v1"
    audit["identity"]["candidate_plan_sha256"] = manifest["plan_file_sha256"]
    audit["identity"]["candidate_probe_sha256"] = manifest["probe_file_sha256"]
    write_evidence_file(output_json, audit_bytes(audit))
    write_evidence_file(output_md, render_overlay_audit(audit).encode("utf-8"))
    print(
        "D9 dependency overlay audit complete "
        f"verdict={audit['corrected_d9']['verdict']} "
        f"ratio={audit['corrected_d9']['data_exclusion_ratio']:.8f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
