"""Official one-pass development runner for the frozen screen v2 (§9).

Order (fail closed): contract hashes + registry -> window counts -> data identity (file SHA
+ tracked receipts, no semantic recompute, per the v1 erratum) -> clean git tree -> guarded
load through 2026-01-20 -> data-quality gate -> trades -> statistics -> determinism re-check
-> snapshot file SHA re-check.

Setup failures (contract, registry, windows, identity, git tree) raise ``SetupError`` and
must not produce a result file, so an operator error cannot consume the single official
run. Only after identity passes may a defect (data-quality breach, duplicate/stray bars,
determinism mismatch) be recorded as INCONCLUSIVE_DATA without arm statistics.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from src.history.research_snapshot import file_sha256
from src.research.development_identity import verify_intraday_identity
from src.research.gross_edge.contract import repo_root
from src.research.gross_edge_v2.arms import (
    UNIVERSE,
    extract_trades,
    valid_bars_for,
    verify_registry,
)
from src.research.gross_edge_v2.contract import (
    FREEZE_COMMIT,
    PREREG_JSON_PATH,
    PREREG_JSON_SHA256,
    PREREG_MD_PATH,
    PREREG_MD_SHA256,
    load_contract,
)
from src.research.gross_edge_v2.data import (
    DATA_END,
    DataDefectError,
    DayBars,
    Windows,
    build_windows,
    load_symbol_bars,
    window_defects,
)
from src.research.gross_edge_v2.evaluation import (
    BaselineCache,
    evaluate_arms,
    verify_statistics,
)

SCHEMA = "p0_us30_gross_edge_screen_result_v2"
TRACKED_SNAPSHOT_RECEIPT = "docs/research/P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json"
MANIFEST_PATH = "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"
RESULT_JSON_PATH = "docs/research/RESULT_P0_US30_GROSS_EDGE_SCREEN_V2.json"
RESULT_MD_PATH = "docs/research/RESULT_P0_US30_GROSS_EDGE_SCREEN_V2.md"
MAX_INVALID_SHARE = 0.05


class SetupError(RuntimeError):
    """Contract/registry/window/identity/git failure before any result may be written."""


def canonical_json_bytes(result: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _base_payload(contract: Mapping[str, Any], windows: Windows) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "verdict": None,
        "strategy_id": contract["strategy_id"],
        "prereg": {
            "md_path": PREREG_MD_PATH,
            "json_path": PREREG_JSON_PATH,
            "md_file_sha256": PREREG_MD_SHA256,
            "json_file_sha256": PREREG_JSON_SHA256,
            "freeze_commit": FREEZE_COMMIT,
        },
        "windows": {
            "trade_eval_start": windows.trade_days[0].isoformat(),
            "trade_eval_end": windows.trade_days[-1].isoformat(),
            "trade_eval_trading_days": len(windows.trade_days),
            "excluded_block_trading_days": len(windows.excluded_block_days),
            "evaluated_trading_days": len(windows.evaluated_days),
            "data_end": DATA_END.isoformat(),
        },
        "blind_oos_accessed": False,
        "holdout_h2_accessed": False,
        "automatic_oos_h2_or_paper_approval": False,
        "data_identity": None,
        "implementation": None,
        "data_quality": None,
        "determinism": None,
        "arms": [],
        "shortlisted_arms": [],
        "inverse_only_arms": [],
        "sample_arms": [],
        "inconclusive_reason": None,
    }


def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("payload_sha256", None)
    payload["payload_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def inconclusive(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    payload["verdict"] = "INCONCLUSIVE_DATA"
    payload["inconclusive_reason"] = reason
    payload["arms"] = []
    payload["shortlisted_arms"] = payload["inverse_only_arms"] = payload["sample_arms"] = []
    return _finalize(payload)


def data_quality(
    bars_by_symbol: Mapping[str, Mapping[date, DayBars]], evaluated_days: Sequence[date]
) -> dict[str, Any]:
    invalid = {
        symbol: sum(
            1 for day in evaluated_days
            if valid_bars_for(bars_by_symbol.get(symbol, {}), day) is None
        )
        for symbol in UNIVERSE
    }
    evaluated = len(UNIVERSE) * len(evaluated_days)
    total = sum(invalid.values())
    share = total / evaluated if evaluated else 1.0
    max_day = max(
        (day for days in bars_by_symbol.values() for day in days), default=None
    )
    return {
        "evaluated_symbol_days": evaluated,
        "invalid_symbol_days": total,
        "invalid_share": share,
        "max_invalid_share": MAX_INVALID_SHARE,
        "breach": share > MAX_INVALID_SHARE,
        "invalid_by_symbol": invalid,
        "max_loaded_day": None if max_day is None else max_day.isoformat(),
    }


def _evaluate(
    bars_by_symbol: Mapping[str, Mapping[date, DayBars]],
    windows: Windows,
    *,
    reverse_input: bool = False,
) -> tuple[list[dict[str, Any]], str, Mapping[str, Mapping[str, int]]]:
    extraction = extract_trades(bars_by_symbol, windows.evaluated_days)
    trades = list(extraction.trades)
    if reverse_input:
        trades.reverse()
    results, verdict = evaluate_arms(
        trades, BaselineCache(bars_by_symbol, windows.evaluated_days)
    )
    return results, verdict, extraction.skip_counts


def build_payload(
    contract: Mapping[str, Any],
    windows: Windows,
    identity: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Mapping[date, DayBars]],
    implementation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Quality gate, trades, statistics and determinism check on already-loaded bars."""
    payload = _base_payload(contract, windows)
    payload["data_identity"] = dict(identity)
    payload["implementation"] = None if implementation is None else dict(implementation)
    max_day = max((day for days in bars_by_symbol.values() for day in days), default=None)
    if max_day is not None and max_day > DATA_END:
        raise AssertionError(f"data after {DATA_END} reached the screen: {max_day}")
    quality = data_quality(bars_by_symbol, windows.evaluated_days)
    payload["data_quality"] = quality
    if quality["breach"]:
        return inconclusive(payload, "data_quality: invalid symbol-days exceed 5%")

    results, verdict, skips = _evaluate(bars_by_symbol, windows)
    rerun, rerun_verdict, _ = _evaluate(bars_by_symbol, windows, reverse_input=True)
    identical = canonical_json_bytes({"a": results, "v": verdict}) == canonical_json_bytes(
        {"a": rerun, "v": rerun_verdict}
    )
    payload["determinism"] = {"evaluation_rerun_reversed_input_identical": identical}
    quality["skip_counts_by_arm"] = skips
    if not identical:
        return inconclusive(payload, "determinism: evaluation re-run differs")
    payload["verdict"] = verdict
    payload["arms"] = results
    payload["shortlisted_arms"] = [r["arm_id"] for r in results if r["status"] == "LONG_CANDIDATE"]
    payload["inverse_only_arms"] = [r["arm_id"] for r in results if r["status"] == "INVERSE_ONLY"]
    payload["sample_arms"] = [r["arm_id"] for r in results if r["status"] == "SAMPLE"]
    return _finalize(payload)


def check_identity(
    contract: Mapping[str, Any],
    *,
    snapshot_path: str,
    raw_root: str,
    manifest_path: str,
    snapshot_receipt_path: str,
) -> dict[str, Any]:
    """File SHA + tracked receipts (v1 erratum method). Raises on any mismatch."""
    expected = contract["data_identity"]
    if os.path.basename(snapshot_path) != expected["snapshot_basename"]:
        # Also keeps the operating market_history.sqlite (or any other DB) out.
        raise RuntimeError(f"snapshot basename is not the frozen snapshot: {snapshot_path}")
    checks = verify_intraday_identity(
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        intraday_manifest_path=manifest_path,
        snapshot_receipt_path=snapshot_receipt_path,
        expected_snapshot_file=str(expected["snapshot_file_sha256"]),
        expected_snapshot_content=str(expected["snapshot_content_sha256"]),
        expected_intraday_manifest=str(expected["manifest_data_sha256"]),
        expected_raw_content=str(expected["raw_content_sha256"]),
    )
    return {
        str(check["name"]): {
            "expected": check["expected"], "actual": check["actual"], "status": check["status"]
        }
        for check in checks
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root(), check=True, capture_output=True, text=True
    ).stdout


def implementation_provenance(*, require_clean: bool) -> dict[str, Any]:
    """HEAD commit and worktree cleanliness (``git status --porcelain``, nothing filtered)."""
    try:
        commit = _git("rev-parse", "HEAD").strip()
        status = _git("status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as error:
        raise SetupError(f"git provenance unavailable: {error}") from error
    clean = status.strip() == ""
    if require_clean and not clean:
        raise SetupError("worktree is not clean; commit the implementation first:\n" + status)
    return {"git_commit": commit, "worktree_clean": clean}


def prepare(
    *,
    snapshot_path: str,
    raw_root: str,
    manifest_path: str,
    snapshot_receipt_path: str | None = None,
    require_clean_tree: bool = True,
) -> tuple[Mapping[str, Any], Windows, dict[str, Any], dict[str, Any]]:
    """Every check that must pass before prices are loaded. Writes nothing.

    Returns (contract, windows, identity, implementation); raises SetupError otherwise.
    """
    try:
        contract = load_contract()
        verify_registry(contract)
        verify_statistics(contract)
        windows = build_windows(contract)
        defects = window_defects(contract, windows)
        if defects:
            raise SetupError("window: " + "; ".join(defects))
        identity = check_identity(
            contract,
            snapshot_path=snapshot_path,
            raw_root=raw_root,
            manifest_path=manifest_path,
            snapshot_receipt_path=snapshot_receipt_path
            or os.path.join(repo_root(), TRACKED_SNAPSHOT_RECEIPT),
        )
    except SetupError:
        raise
    except Exception as error:
        raise SetupError(f"{type(error).__name__}: {error}") from error
    implementation = implementation_provenance(require_clean=require_clean_tree)
    return contract, windows, identity, implementation


def load_all(snapshot_path: str) -> dict[str, dict[date, DayBars]]:
    return {symbol: load_symbol_bars(snapshot_path, symbol) for symbol in UNIVERSE}


def run_screen(
    *,
    snapshot_path: str,
    raw_root: str,
    manifest_path: str,
    snapshot_receipt_path: str | None = None,
) -> dict[str, Any]:
    """Official run. SetupError before identity passes (nothing may be written); afterwards
    data defects become INCONCLUSIVE_DATA. A snapshot file changed during the run raises."""
    contract, windows, identity, implementation = prepare(
        snapshot_path=snapshot_path,
        raw_root=raw_root,
        manifest_path=manifest_path,
        snapshot_receipt_path=snapshot_receipt_path,
    )
    try:
        bars_by_symbol = load_all(snapshot_path)
    except DataDefectError as error:
        payload = _base_payload(contract, windows)
        payload["data_identity"] = identity
        payload["implementation"] = implementation
        result = inconclusive(payload, f"data_defect: {error}")
    else:
        result = build_payload(contract, windows, identity, bars_by_symbol, implementation)
    after = file_sha256(snapshot_path)
    if after != identity["snapshot_file"]["actual"]:
        raise SetupError(f"snapshot file changed during the run: {after}")
    return result


def default_manifest_path() -> str:
    return os.path.join(repo_root(), MANIFEST_PATH)
