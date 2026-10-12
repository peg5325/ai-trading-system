"""Daily same-day screen v1: record the user's decision not to run the GEX family (PREREG §12 step 5, §3 order).

Writes the write-once marker <out>/gex/skipped_by_user.json, which lets the main HOLDOUT open without a GEX DEV
result and permanently blocks the GEX precount/dev. Refused once the GEX precount or dev has run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daily_screen_v1 import pipeline as PL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reason", required=True)
    ap.add_argument("--out-dir", type=Path, default=PL.OUT_DIR)
    ap.add_argument("--i-have-user-approval", action="store_true")
    a = ap.parse_args(argv)
    sha, out = PL.record_gex_skip(out_dir=a.out_dir, approved=a.i_have_user_approval, reason=a.reason)
    print(f"{out} sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
