"""Daily same-day screen v1 DEV run (PREREG_DAILY_SAMEDAY_SCREEN_V1 §7), once per family.

Needs the config and precount SHA-256, user approval, and for `--family gex` the main DEV result (§3 run order).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.daily_screen_v1_precount import add_common, add_main_order
from src.daily_screen_v1 import pipeline as PL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common(ap)
    add_main_order(ap)
    ap.add_argument("--precount", type=Path, required=True)
    ap.add_argument("--precount-sha256", required=True)
    ap.add_argument("--i-have-user-approval", action="store_true")
    a = ap.parse_args(argv)
    sha, out = PL.dev_step(a.family, config_path=a.config, config_sha256=a.config_sha256,
                           precount_path=a.precount, precount_sha256=a.precount_sha256,
                           approved=a.i_have_user_approval, out_dir=a.out_dir, main_dev_result=a.main_dev_result,
                           main_dev_result_sha256=a.main_dev_result_sha256,
                           main_addendum_commit=a.main_addendum_commit, allow_dirty=a.allow_dirty,
                           main_ref=a.main_ref)
    print(f"{out} sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
