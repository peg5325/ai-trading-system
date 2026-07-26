"""P0-US30 ORB clean-session immutable SQLite snapshot 생성 CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.daily_manifest import read_p0_universe  # noqa: E402
from src.history.research_snapshot import create_clean_snapshot  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="canonical DB를 변경하지 않고 ORB clean-session derivative 생성")
    ap.add_argument("--source-db", required=True)
    ap.add_argument("--output-db", required=True)
    ap.add_argument("--yaml", default="config/universe_p0_us30.yaml")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--collection-run-id", required=True)
    ap.add_argument("--sessions", default="PRE,REGULAR,POST")
    ap.add_argument("--raw-manifest", default=None)
    ap.add_argument("--generated-at", default=None)
    ap.add_argument("--source-commit", default=None)
    ap.add_argument("--receipt", required=True)
    args = ap.parse_args(argv)

    universe = read_p0_universe(args.yaml)
    if not universe["valid"]:
        ap.error(f"invalid universe: {universe['errors']}")
    if args.as_of != universe["as_of"]:
        ap.error(f"--as-of {args.as_of} != universe.as_of {universe['as_of']}")
    sessions = [s.strip().upper() for s in args.sessions.split(",") if s.strip()]
    raw_hash = None
    if args.raw_manifest:
        with open(args.raw_manifest, encoding="utf-8") as fh:
            raw_hash = json.load(fh).get("raw_content_sha256")
    generated = args.generated_at or datetime.now(timezone.utc).isoformat()
    result = create_clean_snapshot(
        source_db=args.source_db, output_db=args.output_db,
        symbols=universe["symbols"], as_of=args.as_of,
        allowed_sessions=sessions, collection_run_id=args.collection_run_id,
        generated_at_utc=generated, source_commit=args.source_commit,
        raw_content_sha256=raw_hash,
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.receipt)), exist_ok=True)
    with open(args.receipt, "w", encoding="utf-8") as fh:
        json.dump(result.receipt, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(result.receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
