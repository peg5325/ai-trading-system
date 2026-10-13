"""Daily same-day screen v1 precount (PREREG_DAILY_SAMEDAY_SCREEN_V1 §6). Counts, thresholds, sd of R*, power.

No mean return or P&L is computed. `--family gex` also needs the main DEV result (§3 run order).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daily_screen_v1 import pipeline as PL
from src.daily_screen_v1.candidates import CONFIG_PATH


def add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--family", choices=PL.FAMILIES, required=True)
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--config-sha256", required=True)
    ap.add_argument("--out-dir", type=Path, default=PL.OUT_DIR)
    ap.add_argument("--allow-dirty", action="store_true")


def add_main_order(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--main-dev-result", type=Path, help="gex only: main dev_result.json")
    ap.add_argument("--main-dev-result-sha256", help="gex only")
    ap.add_argument("--main-addendum-commit", help="gex only: merged main addendum commit (unless main NO_PASS)")
    ap.add_argument("--main-ref", default=PL.MAIN_REF, help="ref the addendum must be merged into (fetched)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_common(ap)
    add_main_order(ap)
    a = ap.parse_args(argv)
    sha, out = PL.precount_step(a.family, config_path=a.config, config_sha256=a.config_sha256, out_dir=a.out_dir,
                                main_dev_result=a.main_dev_result, main_dev_result_sha256=a.main_dev_result_sha256,
                                main_addendum_commit=a.main_addendum_commit, allow_dirty=a.allow_dirty,
                                main_ref=a.main_ref)
    print(f"{out} sha256 {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
