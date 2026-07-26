#!/usr/bin/env python3
"""Immutable raw snapshot 디렉터리의 결정론 manifest를 JSON으로 기록한다."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.raw_snapshot import RawSnapshotStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="raw snapshot content manifest 생성")
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    manifest = RawSnapshotStore(args.root).manifest()
    output = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    partial = output + ".partial"
    if os.path.exists(partial):
        ap.error(f"stale partial 존재: {os.path.basename(partial)}")
    with open(partial, "x", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, output)
    print(
        f"[raw-manifest] files={manifest['content_file_count']} "
        f"sha256={manifest['raw_content_sha256']} out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
