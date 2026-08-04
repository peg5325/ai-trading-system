"""D9-only audit over the frozen snapshot plus eligible repair candidates."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.history.raw_snapshot import RawSnapshotStore
from src.models import Candle
from src.research.gap_rvol.contract import require
from src.research.gap_rvol.development import FeaturePass
from src.research.gap_rvol.loading import load_symbol_day
from src.research.gap_rvol.provider_repair import (
    SCHEMA as CANDIDATE_SCHEMA,
)
from src.research.gap_rvol.provider_repair import (
    data_content_sha256_path,
)

SCHEMA = "gap-rvol-d9-overlay-audit/v1"


@dataclass(frozen=True)
class CandidateIdentity:
    manifest_file_sha256: str
    candidate_data_sha256: str
    raw_content_sha256: str
    candidate_rows: int
    eligible_targets: tuple[tuple[str, str], ...]
    ineligible_targets: tuple[tuple[str, str], ...]


def verify_candidate_identity(
    *, manifest_path: str, candidate_db: str, candidate_raw_root: str
) -> tuple[dict[str, Any], CandidateIdentity]:
    """Fail closed unless DB/raw/target identities match the tracked manifest."""
    with open(manifest_path, "rb") as handle:
        manifest_bytes = handle.read()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError("unexpected repair candidate manifest schema")
    actual_data = data_content_sha256_path(candidate_db)
    if actual_data != manifest["repair_db_data_content_sha256"]:
        raise ValueError("repair candidate DB content hash mismatch")
    if not os.path.isdir(candidate_raw_root):
        raise ValueError("repair candidate raw root is missing")
    raw_manifest = RawSnapshotStore(candidate_raw_root).manifest()
    actual_raw = str(raw_manifest["raw_content_sha256"])
    if actual_raw != manifest["raw_content_sha256"]:
        raise ValueError("repair candidate raw content hash mismatch")
    if int(raw_manifest["content_file_count"]) != int(manifest["raw_content_file_count"]):
        raise ValueError("repair candidate raw file count mismatch")

    all_targets = {
        (str(item["symbol"]), str(item["day"])) for item in manifest["targets"]
    }
    eligible = {
        (str(item["symbol"]), str(item["day"]))
        for item in manifest["targets"]
        if item["full_regular_session"] is True
        and item["required_feature_minutes_complete"] is True
    }
    con = sqlite3.connect(f"file:{candidate_db}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"repair candidate DB integrity failed: {integrity}")
        rows = int(con.execute("SELECT COUNT(*) FROM candle_history").fetchone()[0])
        db_targets = {
            (str(symbol), str(day))
            for symbol, day in con.execute(
                "SELECT DISTINCT symbol,substr(ts_exchange,1,10) "
                "FROM candle_history WHERE interval='1m'"
            )
        }
        conflicts = int(con.execute("SELECT COUNT(*) FROM import_conflict").fetchone()[0])
    finally:
        con.close()
    if rows != int(manifest["repair_db_rows"]):
        raise ValueError("repair candidate DB row count mismatch")
    if db_targets != all_targets:
        raise ValueError("repair candidate DB target set mismatch")
    if conflicts:
        raise ValueError("repair candidate DB has import conflicts")
    if len(eligible) != int(manifest["full_regular_session_count"]):
        raise ValueError("eligible repair target count mismatch")
    return manifest, CandidateIdentity(
        manifest_file_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        candidate_data_sha256=actual_data,
        raw_content_sha256=actual_raw,
        candidate_rows=rows,
        eligible_targets=tuple(sorted(eligible)),
        ineligible_targets=tuple(sorted(all_targets - eligible)),
    )


def overlay_loader(
    *, candidate_db: str, eligible_targets: tuple[tuple[str, str], ...]
) -> Callable[[str, str, date], tuple[Candle, ...]]:
    """Replace only eligible symbol-days; all other loads stay on the frozen DB."""
    eligible = set(eligible_targets)

    def load(snapshot_db: str, symbol: str, day: date) -> tuple[Candle, ...]:
        if (symbol, day.isoformat()) in eligible:
            candles = load_symbol_day(candidate_db, symbol, day)
            if not candles:
                raise ValueError(f"eligible repair target is empty: {symbol} {day}")
            return candles
        return load_symbol_day(snapshot_db, symbol, day)

    return load


def _event_map(feature_pass: FeaturePass) -> dict[tuple[str, str], tuple[str, ...]]:
    primary = feature_pass.features_by_spec["primary"]
    return {
        (symbol, day.isoformat()): primary[day][symbol].excluded_reasons
        for day in feature_pass.eval_days
        for symbol in feature_pass.symbols
        if primary[day][symbol].excluded_reasons
    }


def build_overlay_audit(
    *,
    feature_pass: FeaturePass,
    baseline_audit: Mapping[str, Any],
    candidate_identity: CandidateIdentity,
    contract: Mapping[str, object],
    frozen_identity: Mapping[str, str],
    producer_commit: str,
    review_evidence_url: str,
    generated_at_utc: str,
) -> dict[str, Any]:
    baseline_events = {
        (str(item["symbol"]), str(item["day"])): tuple(map(str, item["reasons"]))
        for item in baseline_audit["exclusion_audit"]["events"]
    }
    overlay_events = _event_map(feature_pass)
    added = sorted(set(overlay_events) - set(baseline_events))
    if added:
        raise ValueError(f"repair overlay introduced new exclusion events: {added[:5]}")
    resolved = sorted(set(baseline_events) - set(overlay_events))
    changed = [
        {
            "symbol": symbol,
            "day": day,
            "before": list(baseline_events[(symbol, day)]),
            "after": list(overlay_events[(symbol, day)]),
        }
        for symbol, day in sorted(set(baseline_events) & set(overlay_events))
        if baseline_events[(symbol, day)] != overlay_events[(symbol, day)]
    ]
    denominator = len(feature_pass.eval_days) * len(feature_pass.symbols)
    numerator = len(overlay_events)
    ratio = numerator / denominator
    threshold = float(require(contract, "defect_policy", "data_exclusion_ratio_lte"))
    major = int(baseline_audit["corrected_d9"]["major_defect_count"])
    passes = major == 0 and ratio <= threshold
    reasons = Counter(
        reason for event_reasons in overlay_events.values() for reason in event_reasons
    )
    return {
        "schema": SCHEMA,
        "generated_at_utc": generated_at_utc,
        "producer_commit": producer_commit,
        "review_evidence_url": review_evidence_url,
        "scope": {
            "development_only": True,
            "d9_only": True,
            "blind_oos_opened": False,
            "performance_computed": False,
            "forward_returns_computed": False,
            "strategy_contract_changed": False,
            "frozen_snapshot_written": False,
        },
        "identity": {
            "frozen": dict(sorted(frozen_identity.items())),
            "candidate_manifest_file_sha256": candidate_identity.manifest_file_sha256,
            "candidate_data_sha256": candidate_identity.candidate_data_sha256,
            "candidate_raw_sha256": candidate_identity.raw_content_sha256,
            "candidate_rows": candidate_identity.candidate_rows,
        },
        "overlay": {
            "eligible_targets": len(candidate_identity.eligible_targets),
            "ineligible_targets_not_applied": len(candidate_identity.ineligible_targets),
            "resolved_event_count": len(resolved),
            "changed_event_count": len(changed),
            "added_event_count": 0,
            "resolved_events": [
                {"symbol": symbol, "day": day} for symbol, day in resolved
            ],
            "changed_events": changed,
        },
        "baseline": {
            "numerator": int(baseline_audit["exclusion_audit"]["numerator"]),
            "denominator": int(baseline_audit["exclusion_audit"]["denominator"]),
            "ratio": float(baseline_audit["exclusion_audit"]["ratio"]),
        },
        "corrected_d9": {
            "major_defect_count": major,
            "data_exclusion_numerator": numerator,
            "data_exclusion_denominator": denominator,
            "data_exclusion_ratio": ratio,
            "threshold_lte": threshold,
            "reason_counts": {key: reasons[key] for key in sorted(reasons)},
            "passes": passes,
            "verdict": (
                "REQUIRES_FULL_GATE_REEVALUATION" if passes else "INCONCLUSIVE_DATA"
            ),
        },
    }


def audit_bytes(audit: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def render_overlay_audit(audit: Mapping[str, Any]) -> str:
    d9 = audit["corrected_d9"]
    overlay = audit["overlay"]
    identity = audit["identity"]
    lines = [
        "# P0-US30 Gap+RVOL D9 repair overlay audit",
        "",
        f"> generated: `{audit['generated_at_utc']}`",
        f"> producer: `{audit['producer_commit']}`",
        "",
        "## 판정",
        "",
        f"**{d9['verdict']}**",
        "",
        f"- excluded: `{d9['data_exclusion_numerator']} / {d9['data_exclusion_denominator']}`",
        f"- ratio: `{d9['data_exclusion_ratio']:.8f}` (threshold `<= {d9['threshold_lte']:.2f}`)",
        f"- major defects: `{d9['major_defect_count']}`",
        "",
        "## Overlay effect",
        "",
        f"- eligible targets applied: `{overlay['eligible_targets']}`",
        f"- ineligible targets not applied: `{overlay['ineligible_targets_not_applied']}`",
        f"- resolved events: `{overlay['resolved_event_count']}`",
        f"- changed events: `{overlay['changed_event_count']}`",
        f"- added events: `{overlay['added_event_count']}`",
        "",
        "## Identity",
        "",
        f"- candidate manifest: `{identity['candidate_manifest_file_sha256']}`",
        f"- candidate data: `{identity['candidate_data_sha256']}`",
        f"- candidate raw: `{identity['candidate_raw_sha256']}`",
        "",
        "## Scope",
        "",
        "- development-only / D9-only: `True`",
        "- blind OOS opened: `False`",
        "- performance / forward returns computed: `False`",
        "- frozen snapshot written: `False`",
        "",
    ]
    return "\n".join(lines)
