#!/usr/bin/env python3
"""Run the frozen P0-US30 development-only gross-edge screen v2.

Modes (mutually exclusive):
  default             the single official run; writes the result JSON + Markdown.
  --preflight         contract, registry, statistics, windows, identity, clean git tree
                      and output-path checks only; writes nothing, loads no prices.
  --count-conditions  after identity passes, loads exactly like the official run and
                      prints signal-condition counts only (no fills, no returns) as JSON
                      to stdout; writes nothing.

Setup failures exit non-zero with a stderr message and write no result file. Refuses to
start if either result file already exists, and never overwrites one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.research.gross_edge.contract import repo_root
from src.research.gross_edge_v2.counts import condition_counts
from src.research.gross_edge_v2.data import DataDefectError
from src.research.gross_edge_v2.report import write_outputs
from src.research.gross_edge_v2.run import (
    RESULT_JSON_PATH,
    RESULT_MD_PATH,
    SetupError,
    default_manifest_path,
    load_all,
    prepare,
    run_screen,
)


def _fail(message: str) -> int:
    print(f"gross-edge screen v2: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-db", required=True)
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--manifest", default=default_manifest_path())
    parser.add_argument("--output-json", default=os.path.join(repo_root(), RESULT_JSON_PATH))
    parser.add_argument("--output-md", default=os.path.join(repo_root(), RESULT_MD_PATH))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--count-conditions", action="store_true")
    args = parser.parse_args(argv)
    paths = {
        "snapshot_path": os.path.abspath(args.snapshot_db),
        "raw_root": os.path.abspath(args.raw_root),
        "manifest_path": os.path.abspath(args.manifest),
    }

    if not args.count_conditions:
        for path in (args.output_json, args.output_md):
            if os.path.exists(path):
                print(f"refusing to overwrite existing result: {path}", file=sys.stderr)
                return 2

    try:
        if args.preflight:
            _, _, identity, implementation = prepare(**paths)
            print(json.dumps({
                "preflight": "ok",
                "identity": {name: check["status"] for name, check in identity.items()},
                "implementation": implementation,
                "outputs_absent": [args.output_json, args.output_md],
            }, indent=2, sort_keys=True))
            return 0
        if args.count_conditions:
            _, windows, _, implementation = prepare(**paths, require_clean_tree=False)
            counts = condition_counts(load_all(paths["snapshot_path"]), windows.evaluated_days)
            counts["implementation"] = implementation
            print(json.dumps(counts, indent=2, sort_keys=True))
            return 0
        result = run_screen(**paths)
    except SetupError as error:
        return _fail(f"setup failed, nothing written: {error}")
    except DataDefectError as error:  # count mode only; the official run records it
        return _fail(f"data defect, nothing written: {error}")
    write_outputs(result, json_path=args.output_json, md_path=args.output_md)
    print(
        f"gross-edge screen v2 complete verdict={result['verdict']} "
        f"payload_sha256={result['payload_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
