"""Build a research data copy from an allowlist (preparation step, P3). Reads only the allowlisted rows.

  python scripts/build_research_copy.py --source-db data/market_history.sqlite \
      --allowlist config/research_allowlist_<id>.json --out <dir>/<id>.sqlite

The allowlist must not touch the sealed block of its interval's track (refused before the source is opened). The
copy is write-once and made read-only; `<out>.manifest.json` records the allowlist hash, the copy SHA-256 and
per-symbol row counts and hashes. Research runs take the copy and verify it; they never get the source path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evalpath.research_copy import CopyRefused, build_copy


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--source-db", type=Path, required=True)
    p.add_argument("--allowlist", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    try:
        al = json.loads(args.allowlist.read_text(encoding="utf-8"))
        man = build_copy(args.source_db, al, args.out)
    except CopyRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"copy": str(args.out), "copy_sha256": man["copy_sha256"],
                      "allowlist_sha256": man["allowlist_sha256"], "row_counts": man["row_counts"],
                      "other_track_seal_overlaps": len(man["other_track_seal_overlaps"])}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
