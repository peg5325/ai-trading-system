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


def _assert_receipt_paths_available(path: str) -> tuple[str, str]:
    target = os.path.abspath(path)
    partial = target + ".partial"
    if os.path.exists(target):
        raise FileExistsError(
            f"receipt overwrite 거부: {os.path.basename(target)}")
    if os.path.exists(partial):
        raise FileExistsError(
            f"receipt stale partial 존재: {os.path.basename(partial)}")
    return target, partial


def _write_receipt_atomic(path: str, payload: dict) -> None:
    """Receipt를 부분 파일 노출이나 기존 증거 덮어쓰기 없이 영속화한다."""
    target, partial = _assert_receipt_paths_available(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    data = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(partial, "x", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # 사전 검사 뒤 다른 writer가 final을 만든 경우에도 덮어쓰지 않는다.
        if os.path.exists(target):
            raise FileExistsError(
                f"receipt concurrent overwrite 거부: {os.path.basename(target)}")
        os.replace(partial, target)
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="canonical DB를 변경하지 않고 ORB clean-session derivative 생성")
    ap.add_argument("--source-db", required=True)
    ap.add_argument("--output-db", required=True)
    ap.add_argument("--yaml", default="config/universe_p0_us30.yaml")
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--collection-run-id", required=True)
    ap.add_argument("--sessions", default="PRE,REGULAR,POST")
    ap.add_argument("--raw-manifest", required=True)
    ap.add_argument("--generated-at", default=None)
    ap.add_argument("--source-commit", default=None)
    ap.add_argument("--receipt", required=True)
    args = ap.parse_args(argv)

    _assert_receipt_paths_available(args.receipt)
    universe = read_p0_universe(args.yaml)
    if not universe["valid"]:
        ap.error(f"invalid universe: {universe['errors']}")
    if args.as_of != universe["as_of"]:
        ap.error(f"--as-of {args.as_of} != universe.as_of {universe['as_of']}")
    sessions = [s.strip().upper() for s in args.sessions.split(",") if s.strip()]
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
    _write_receipt_atomic(args.receipt, result.receipt)
    print(json.dumps(result.receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
