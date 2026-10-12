"""Reversal study v1 blind OOS (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §8, §8.1).

  prepare  Targets = (a) the designated entry-study k = 2 cell, always, plus (b) the dev result's selected cell, if
           any. Applies the OOS data gate (G1, G3) and builds the OOS mask and the snapshot row-hash list. Every OOS
           read is research_load(purpose="coverage"); no return is computed. Writes oos_mask.json,
           oos_snapshot.json, oos_prepare.json (write-once) and prints their SHA-256 for the addendum PR.
  run      Opens the OOS once: per target a verified unlock artifact (--unlock-artifact a=PATH / b=PATH) is loaded and
           bound to this code (OOSExecutionGrant); without it research_load raises OOSLockError. Refuses without
           --i-have-user-approval, without the OOS event calendar, if the rows or the OOS mask differ from the
           prepared ones, or if G4 fails.
Both subcommands run the same §5.1 fixed-input verification as `dev` first.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.reversal_study_v1 import cli as C
from src.reversal_study_v1 import oos as O


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "run"):
        sp = sub.add_parser(name)
        C.add_fixed_args(sp, approval=(name == "run"))
        sp.add_argument("--dev-result", type=Path, required=True)
        sp.add_argument("--dev-result-sha256", required=True)
        sp.add_argument("--oos-events", type=Path, default=ROOT / "config" / "daytrade_events_oos_v1.yaml")
    runp = sub.choices["run"]
    runp.add_argument("--prepare", type=Path, required=True, help="oos_prepare.json")
    runp.add_argument("--prepare-sha256", required=True)
    runp.add_argument("--oos-mask", type=Path, required=True)
    runp.add_argument("--oos-mask-sha256", required=True)
    runp.add_argument("--oos-snapshot", type=Path, required=True)
    runp.add_argument("--oos-snapshot-sha256", required=True)
    runp.add_argument("--unlock-artifact", action="append", default=[], metavar="LABEL=PATH")
    runp.add_argument("--snapshot-manifest-path", required=True)
    runp.add_argument("--snapshot-hash-field", required=True)
    runp.add_argument("--repo-root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    return O.prepare(args) if args.cmd == "prepare" else O.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
