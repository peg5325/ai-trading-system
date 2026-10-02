"""Stage-3 swing study entry point (PREREG_STAGE3_SWING_V1).

  --fetch    download raw files only (data/external/stage3_swing/<YYYYMMDD>/); no computation.
  (default)  compute from saved raw files and write RESULT_STAGE3_SWING_V1.json/.md.

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
RESULT_BASE = ROOT / "docs" / "research" / "RESULT_STAGE3_SWING_V1"


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
    ap.add_argument("--workers", type=int, default=1, help="bootstrap worker processes")
    args = ap.parse_args()

    if args.fetch:
        out = args.raw_dir or RAW_ROOT / datetime.now(timezone.utc).strftime("%Y%m%d")
        manifest = fetch_all(out)
        print(f"fetched {len(manifest['files'])} files to {out}")
        return 0

    raw_dir = args.raw_dir or max(p for p in RAW_ROOT.iterdir() if p.is_dir())
    prov = provenance(raw_dir)
    result = run_study(load_raw_dir(raw_dir), prov, reps=args.reps, workers=args.workers)
    write_json(result, RESULT_BASE.with_suffix(".json"))
    RESULT_BASE.with_suffix(".md").write_text(render_markdown(result))
    print(f"status: {result['status']}")
    return 0 if result["status"] != "CALIBRATION_FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
