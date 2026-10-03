"""Stage-3 regime study entry point (PREREG_STAGE3_REGIME_V1).

  --explore  read rows up to 2019-12-31 only, evaluate 625 combos per index and write
             docs/research/SELECTION_STAGE3_REGIME_V1.json (refuses if it exists or the tree is dirty).
  --confirm  only with a committed selection file: compute 2020-01-02..2025-07-18 once and write
             RESULT_STAGE3_REGIME_V1.json/.md (refuses if they exist).

--not-prereg writes *.NOT_PREREG.* files instead and skips the one-shot/clean-tree guards.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.stage3_regime import contract as C
from src.research.stage3_regime.report import render_markdown
from src.research.stage3_regime.runner import run_confirm, run_explore
from src.research.stage3_swing.fetch import raw_sha256
from src.research.stage3_swing.loading import load_raw_dir
from src.research.stage3_swing.report import write_json

DEFAULT_RAW = ROOT / "data" / "external" / "stage3_swing" / "20261002"
SELECTION = ROOT / C.SELECTION_PATH
SELECTION_NP = SELECTION.with_name(SELECTION.stem + ".NOT_PREREG.json")
RESULT_BASE = ROOT / "docs" / "research" / C.RESULT_NAME


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def _committed(rel: str) -> bool:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel], cwd=ROOT, capture_output=True, check=False
    ).returncode == 0
    return tracked and not _git("status", "--porcelain", "--", rel)


def provenance(raw_dir: Path) -> dict:
    blob = _git("hash-object", C.PREREG_PATH)
    committed = _git("rev-parse", f"HEAD:{C.PREREG_PATH}")
    if blob != committed:
        raise SystemExit("prereg file differs from the committed blob; the prereg is locked")
    return {
        "prereg": {"path": C.PREREG_PATH, "git_blob_sha": blob},
        "code_commit": _git("rev-parse", "HEAD"),
        "code_dirty": bool(_git("status", "--porcelain", "--", *C.CODE_PATHS)),
        "tree_dirty": bool(_git("status", "--porcelain")),
        "raw_dir": str(raw_dir.relative_to(ROOT)) if raw_dir.is_relative_to(ROOT) else str(raw_dir),
        "raw_sha256": raw_sha256(raw_dir),
    }


V2_RESULT = ROOT / "docs" / "research" / "RESULT_STAGE3_SWING_V2.json"


def _raw_matches_v2(prov: dict) -> bool:
    """Prereg section 1: the raw files must be the ones the swing v2 result recorded."""
    return json.loads(V2_RESULT.read_text())["provenance"]["raw_sha256"] == prov["raw_sha256"]


def _code_unchanged_since(commit: str) -> bool:
    return subprocess.run(
        ["git", "diff", "--quiet", commit, "HEAD", "--", *C.CODE_PATHS], cwd=ROOT, check=False
    ).returncode == 0


def _refuse(problems: list[str]) -> int:
    print("refusing to run:\n  " + "\n  ".join(problems)
          + "\nuse --not-prereg for an explicitly non-preregistered run", file=sys.stderr)
    return 1


def explore(raw_dir: Path, not_prereg: bool) -> int:
    if not not_prereg and SELECTION.exists():
        return _refuse([f"{SELECTION.name} already exists (explore runs once)"])
    prov = provenance(raw_dir)
    if not not_prereg and prov["tree_dirty"]:
        return _refuse(["working tree has uncommitted changes (commit before explore)"])
    if not not_prereg and not _raw_matches_v2(prov):
        return _refuse(["raw SHA-256 differs from RESULT_STAGE3_SWING_V2 (prereg section 1)"])
    prov["preregistered_run"] = not not_prereg
    prov["explore_window"] = {"start": str(C.EXPLORE_START), "end": str(C.EXPLORE_END)}
    sel = run_explore(load_raw_dir(raw_dir), prov)
    sel["preregistered_run"] = not not_prereg
    out = SELECTION_NP if not_prereg else SELECTION
    write_json(sel, out)
    for index, row in sel["indices"].items():
        print(f"{index}: {row['status']} {row.get('combo', '')}")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


def confirm(raw_dir: Path, not_prereg: bool) -> int:
    problems = []
    if not_prereg and not RESULT_BASE.with_suffix(".json").exists():
        return _refuse(["a non-preregistered confirm may not open the confirm window before the official run"])
    sel_path = SELECTION if (SELECTION.exists() or not not_prereg) else SELECTION_NP
    if not sel_path.exists():
        problems.append(f"{sel_path.name} missing; run --explore and commit it first")
    elif not not_prereg and not _committed(C.SELECTION_PATH):
        problems.append(f"{SELECTION.name} is not committed (git ls-files + clean)")
    if not not_prereg and RESULT_BASE.with_suffix(".json").exists():
        problems.append(f"{RESULT_BASE.name}.json already exists (confirm runs once)")
    if not not_prereg and RESULT_BASE.with_suffix(".md").exists():
        problems.append(f"{RESULT_BASE.name}.md already exists (confirm runs once)")
    if problems:
        return _refuse(problems)
    prov = provenance(raw_dir)
    selection = json.loads(sel_path.read_text())
    if not not_prereg:
        if prov["code_dirty"]:
            problems.append("code has uncommitted changes (commit before the one-shot confirm)")
        if selection.get("prereg", {}).get("git_blob_sha") != prov["prereg"]["git_blob_sha"]:
            problems.append("selection was made under a different prereg blob")
        if not selection.get("preregistered_run"):
            problems.append("selection file is not from a preregistered explore run")
        sel_prov = selection.get("provenance", {})
        if sel_prov.get("raw_sha256") != prov["raw_sha256"]:
            problems.append("raw SHA-256 differs from the one recorded at explore")
        if not sel_prov.get("code_commit") or not _code_unchanged_since(sel_prov["code_commit"]):
            problems.append("study code changed since the explore run")
        if problems:
            return _refuse(problems)
    prov["preregistered_run"] = not not_prereg
    prov["selection_path"] = str(sel_path.relative_to(ROOT))
    prov["selection_git_blob_sha"] = _git("hash-object", str(sel_path.relative_to(ROOT)))
    prov["confirm_window"] = {"start": str(C.CONFIRM_START), "end": str(C.CONFIRM_END)}
    result = run_confirm(load_raw_dir(raw_dir), selection, prov)
    result["preregistered_run"] = not not_prereg
    base = RESULT_BASE.with_name(RESULT_BASE.name + ".NOT_PREREG") if not_prereg else RESULT_BASE
    write_json(result, base.with_name(base.name + ".json"))
    md = render_markdown(result)
    if not_prereg:
        title = f"# RESULT stage 3 regime {C.VERSION}"
        md = md.replace(title, title + " (NOT PREREGISTERED)", 1)
    base.with_name(base.name + ".md").write_text(md)
    print(f"verdicts: {result['verdicts']} stop_recommendation: {result['stop_recommendation']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    phase = ap.add_mutually_exclusive_group(required=True)
    phase.add_argument("--explore", action="store_true", help="explore window only -> selection file")
    phase.add_argument("--confirm", action="store_true", help="one-shot confirm with the committed selection")
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW, help=f"raw directory (default {DEFAULT_RAW})")
    ap.add_argument("--not-prereg", action="store_true", help="non-preregistered run; writes *.NOT_PREREG.* files")
    args = ap.parse_args()
    if not args.raw_dir.is_dir():
        return _refuse([f"raw directory {args.raw_dir} not found"])
    if args.explore:
        return explore(args.raw_dir, args.not_prereg)
    return confirm(args.raw_dir, args.not_prereg)


if __name__ == "__main__":
    raise SystemExit(main())
