#!/usr/bin/env python3
"""Run the frozen P0-US30 development-only gross-edge screen v2 (one official run).

Refuses to start if either result file already exists, and never overwrites one.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gross_edge.contract import repo_root
from src.research.gross_edge_v2.report import write_outputs
from src.research.gross_edge_v2.run import (
    RESULT_JSON_PATH,
    RESULT_MD_PATH,
    default_manifest_path,
    run_screen,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--manifest", default=default_manifest_path())
    parser.add_argument("--output-json", default=os.path.join(repo_root(), RESULT_JSON_PATH))
    parser.add_argument("--output-md", default=os.path.join(repo_root(), RESULT_MD_PATH))
    args = parser.parse_args(argv)
    for path in (args.output_json, args.output_md):
        if os.path.exists(path):
            print(f"refusing to overwrite existing result: {path}", file=sys.stderr)
            return 2
    result = run_screen(
        snapshot_path=os.path.abspath(args.snapshot_db),
        raw_root=os.path.abspath(args.raw_root),
        manifest_path=os.path.abspath(args.manifest),
    )
    write_outputs(result, json_path=args.output_json, md_path=args.output_md)
    print(
        f"gross-edge screen v2 complete verdict={result['verdict']} "
        f"payload_sha256={result['payload_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
