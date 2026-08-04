#!/usr/bin/env python
"""Recompute Gap+RVOL D9 only with eligible isolated Toss repair candidates."""
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
    overlay_loader,
    render_overlay_audit,
    verify_candidate_identity,
)
from src.research.gap_rvol.development import (
    build_development_feature_pass,
    tracked_manifest_path,
)
from src.research.gap_rvol.identity import verify_inputs
from src.research.gap_rvol.provider_repair import build_repair_targets
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
    expected_url = "github.com/peg5325/ai-trading-system/pull/107#issuecomment-"
    if expected_url not in args.review_evidence_url:
        parser.error("review evidence must be a stable PR #107 issue comment URL")
    fixed_manifest = os.path.join(
        repo_root(),
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_TOSS_REPAIR_CANDIDATE.json",
    )
    if os.path.realpath(args.candidate_manifest) != os.path.realpath(fixed_manifest):
        parser.error("candidate manifest must be the tracked PR #107 manifest")

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
    candidate_manifest, candidate_identity = verify_candidate_identity(
        manifest_path=args.candidate_manifest,
        candidate_db=args.candidate_db,
        candidate_raw_root=args.candidate_raw_root,
    )
    audit_path = os.path.join(
        repo_root(), "docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json"
    )
    probe_path = os.path.join(
        repo_root(), "docs/research/PROBE_P0_US30_GAP_RVOL_D9_TOSS.json"
    )
    baseline = _read(audit_path)
    expected_targets = {
        (target.symbol, target.day.isoformat())
        for target in build_repair_targets(
            audit=baseline,
            probe=_read(probe_path),
        )
    }
    actual_targets = {
        (str(item["symbol"]), str(item["day"]))
        for item in candidate_manifest["targets"]
    }
    if actual_targets != expected_targets:
        parser.error("candidate manifest targets differ from tracked audit/probe intersection")
    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path=args.snapshot_db,
        loader=overlay_loader(
            candidate_db=args.candidate_db,
            eligible_targets=candidate_identity.eligible_targets,
        ),
    )
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
    write_evidence_file(output_json, audit_bytes(audit))
    write_evidence_file(output_md, render_overlay_audit(audit).encode("utf-8"))
    print(
        "D9 overlay audit complete "
        f"verdict={audit['corrected_d9']['verdict']} "
        f"ratio={audit['corrected_d9']['data_exclusion_ratio']:.8f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
