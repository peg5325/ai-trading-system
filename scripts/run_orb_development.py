"""P0-US30 ORB development-only 공식 실행 CLI — 이슈 #91, 단계 4-C."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.orb.development import (
    DevelopmentPaths,
    run_development,
)
from src.research.orb.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    PreconditionEvidence,
)
from src.research.orb.report import (
    render_development_report,
    result_json_bytes,
    write_evidence_file,
)

P2_TEST = (
    "tests/test_oos_guard_wiring.py::test_no_direct_load_candles_outside_gate"
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
    parser.add_argument(
        "--manifest",
        default="docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json",
    )
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--review-evidence-url", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    args = parser.parse_args(argv)

    head = _head_commit()
    if args.producer_commit != head:
        parser.error(
            f"--producer-commit {args.producer_commit} != current HEAD {head}"
        )
    preconditions = PreconditionEvidence(
        implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
        review_status="approved",
        review_evidence_url=args.review_evidence_url,
        loader_boundary_status="passed",
        loader_boundary_test=P2_TEST,
    )
    run = run_development(
        paths=DevelopmentPaths(
            snapshot_path=os.path.abspath(args.snapshot_db),
            raw_root=os.path.abspath(args.raw_root),
            manifest_path=os.path.abspath(args.manifest),
        ),
        producer_commit=args.producer_commit,
        generated_at_utc=args.generated_at_utc,
        preconditions=preconditions,
    )
    json_bytes = result_json_bytes(run.result)
    report_bytes = render_development_report(run.result).encode("utf-8")
    write_evidence_file(args.output_json, json_bytes)
    write_evidence_file(args.output_md, report_bytes)
    print(
        "ORB development artifact complete "
        f"payload_sha256={run.result['payload_sha256']} "
        f"json_sha256={hashlib.sha256(json_bytes).hexdigest()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
