"""Stage-3 swing study entry point (PREREG_STAGE3_SWING_V2).

  --fetch    download raw files only (data/external/stage3_swing/<YYYYMMDD>/); no computation.
  (default)  compute from saved raw files and write RESULT_STAGE3_SWING_V2.json/.md.

Compute mode is a one-shot run: run it only after the independent review has passed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.stage3_swing import contract as C
from src.research.stage3_swing.fetch import fetch_all, raw_sha256
from src.research.stage3_swing.loading import load_raw_dir
from src.research.stage3_swing.report import render_markdown, write_json
from src.research.stage3_swing.runner import run_study

RAW_ROOT = ROOT / "data" / "external" / "stage3_swing"
RESULT_BASE = ROOT / "docs" / "research" / C.RESULT_NAME


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def provenance(raw_dir: Path) -> dict:
    blob = _git("hash-object", C.PREREG_PATH)
    committed = _git("rev-parse", f"HEAD:{C.PREREG_PATH}")
    if blob != committed:
        raise SystemExit("prereg file differs from the committed blob; the prereg is locked")
    dirty = _git("status", "--porcelain", "--", "src/research/stage3_swing", "scripts/run_stage3_swing.py")
    return {
        "prereg": {"path": C.PREREG_PATH, "git_blob_sha": blob},
        "code_commit": _git("rev-parse", "HEAD"),
        "code_dirty": bool(dirty),
        "raw_dir": str(raw_dir.relative_to(ROOT)) if raw_dir.is_relative_to(ROOT) else str(raw_dir),
        "raw_sha256": raw_sha256(raw_dir),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--fetch", action="store_true", help="download raw files only")
    ap.add_argument("--raw-dir", type=Path, help="raw directory (default: newest under data/external/stage3_swing)")
    ap.add_argument("--reps", type=int, default=C.BOOT_REPS, help="bootstrap replications (prereg: 1000)")
    ap.add_argument(
        "--not-prereg", action="store_true",
        help="allow a non-preregistered run (other reps, dirty tree, or existing result); "
        "writes RESULT_STAGE3_SWING_V2.NOT_PREREG.json/.md",
    )
    ap.add_argument("--workers", type=int, default=1, help="bootstrap worker processes")
    args = ap.parse_args()

    if args.fetch:
        out = args.raw_dir or RAW_ROOT / datetime.now(timezone.utc).strftime("%Y%m%d")
        manifest = fetch_all(out)
        print(f"fetched {len(manifest['files'])} files to {out}")
        return 0

    problems = []
    if not args.not_prereg:
        if args.reps != C.BOOT_REPS:
            problems.append(f"--reps {args.reps} != preregistered {C.BOOT_REPS}")
        if RESULT_BASE.with_suffix(".json").exists():
            problems.append(f"{RESULT_BASE.with_suffix('.json').name} already exists (one run only)")
    if args.raw_dir is None and not (RAW_ROOT.is_dir() and any(p.is_dir() for p in RAW_ROOT.iterdir())):
        problems.append(f"no raw data under {RAW_ROOT}; run --fetch first")
    if problems:
        print("refusing to run:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    raw_dir = args.raw_dir or max(p for p in RAW_ROOT.iterdir() if p.is_dir())
    prov = provenance(raw_dir)
    out_base = RESULT_BASE
    if args.not_prereg:
        out_base = RESULT_BASE.with_name(RESULT_BASE.name + ".NOT_PREREG")
    else:
        if prov["code_dirty"]:
            problems.append("code has uncommitted changes (commit before the one-shot run)")
        if problems:
            print("refusing to run:\n  " + "\n  ".join(problems)
                  + "\nuse --not-prereg for an explicitly non-preregistered run", file=sys.stderr)
            return 1
    prov["preregistered_run"] = not args.not_prereg
    result = run_study(load_raw_dir(raw_dir), prov, reps=args.reps, workers=args.workers)
    result["preregistered_run"] = not args.not_prereg
    write_json(result, out_base.with_name(out_base.name + ".json"))
    md = render_markdown(result)
    if args.not_prereg:
        title = f"# RESULT stage 3 swing {C.VERSION}"
        md = md.replace(title, title + " (NOT PREREGISTERED)", 1)
    out_base.with_name(out_base.name + ".md").write_text(md)
    print(f"status: {result['status']}")
    return 0 if result["status"] != "CALIBRATION_FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
