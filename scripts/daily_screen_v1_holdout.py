"""Daily same-day screen v1 HOLDOUT run (PREREG_DAILY_SAMEDAY_SCREEN_V1 §8), once per family.

Needs the config, precount and dev_result SHA-256, the merged addendum commit (which must record those SHA-256
plus the manifest and events yaml SHA-256) and user approval.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.daily_screen_v1_precount import add_common
from src.daily_screen_v1 import pipeline as PL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common(ap)
    ap.add_argument("--precount", type=Path, required=True)
    ap.add_argument("--precount-sha256", required=True)
    ap.add_argument("--dev-result", type=Path, required=True)
    ap.add_argument("--dev-result-sha256", required=True)
    ap.add_argument("--addendum-commit", required=True, help="merged commit holding this family's addendum")
    ap.add_argument("--gex-dev-result", type=Path, help="main only: GEX dev_result.json (else the GEX-skip marker)")
    ap.add_argument("--gex-dev-result-sha256", help="main only")
    ap.add_argument("--gex-addendum-commit", help="main only: merged GEX addendum commit (unless GEX NO_PASS)")
    ap.add_argument("--main-ref", default=PL.MAIN_REF, help="ref the addendum must be merged into (fetched)")
    ap.add_argument("--i-have-user-approval", action="store_true")
    a = ap.parse_args(argv)
    sha, out = PL.holdout_step(a.family, config_path=a.config, config_sha256=a.config_sha256,
                               precount_path=a.precount, precount_sha256=a.precount_sha256,
                               dev_result_path=a.dev_result, dev_result_sha256=a.dev_result_sha256,
                               addendum_commit=a.addendum_commit, approved=a.i_have_user_approval,
                               out_dir=a.out_dir, gex_dev_result=a.gex_dev_result,
                               gex_dev_result_sha256=a.gex_dev_result_sha256,
                               gex_addendum_commit=a.gex_addendum_commit, allow_dirty=a.allow_dirty,
                               main_ref=a.main_ref)
    print(f"{out} sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
