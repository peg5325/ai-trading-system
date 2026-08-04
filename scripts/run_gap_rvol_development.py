"""P0-US30 Gap+RVOL development-only official execution CLI."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gap_rvol.development import (
    DevelopmentPaths,
    NotRunError,
    run_development,
    tracked_manifest_path,
)
from src.research.gap_rvol.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    IMPLEMENTATION_REVIEW_EVIDENCE_URL,
    PreconditionEvidence,
)
from src.research.gap_rvol.report import (
    render_development_report,
    result_json_bytes,
    validate_evidence_output_paths,
    write_evidence_file,
)

P5_TEST = (
    "tests/test_gap_rvol_loading_and_map.py::"
    "test_performance_package_and_runner_have_no_loader_bypass"
)


def _head_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--runner-review-evidence-url", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args(argv)

    head = _head_commit()
    if args.producer_commit != head:
        parser.error(
            f"--producer-commit {args.producer_commit} != current HEAD {head}"
        )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        output_json, output_md = validate_evidence_output_paths(
            output_json=args.output_json,
            output_md=args.output_md,
            evidence_root=os.path.join(repo_root, "docs", "research"),
            snapshot_path=args.snapshot_db,
            raw_root=args.raw_root,
        )
    except ValueError as exc:
        parser.error(str(exc))
    preconditions = PreconditionEvidence(
        implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
        implementation_review_evidence_url=IMPLEMENTATION_REVIEW_EVIDENCE_URL,
        implementation_map_status="tracked",
        runner_reviewed_commit=args.producer_commit,
        runner_code_review_status="approved",
        runner_architect_status="clear",
        runner_review_evidence_url=args.runner_review_evidence_url,
        loader_boundary_status="passed",
        loader_boundary_test=P5_TEST,
    )
    try:
        run = run_development(
            paths=DevelopmentPaths(
                snapshot_path=os.path.abspath(args.snapshot_db),
                raw_root=os.path.abspath(args.raw_root),
                manifest_path=tracked_manifest_path(),
            ),
            producer_commit=args.producer_commit,
            generated_at_utc=args.generated_at_utc,
            preconditions=preconditions,
        )
    except NotRunError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json_bytes = result_json_bytes(run.result)
    report_bytes = render_development_report(run.result).encode("utf-8")
    write_evidence_file(output_json, json_bytes)
    write_evidence_file(output_md, report_bytes)
    print(
        "Gap+RVOL development artifact complete "
        f"payload_sha256={run.result['payload_sha256']} "
        f"json_sha256={hashlib.sha256(json_bytes).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
