"""FREEZE decision gates of the Paper contract (``decision_input.gates``).

Fail closed: any file, receipt, hash or JSON-path defect yields NO_TRADE with
explicit reason codes.  The brief is read only; nothing here writes files.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.freeze_paper.binding import ContractBinding
from src.output_layout import premarket_day_relpath
from src.premarket.contract import sha256_payload
from src.premarket.operations import OPERATION_SCHEMA, stage_paths
from src.premarket.planner import BRIEF_SCHEMA
from src.universe.screener import leverage_for

TRADE = "TRADE"
NO_TRADE = "NO_TRADE"
LEG_LONG = "LONG"
LEG_INVERSE = "INVERSE"
_LEG_BY_REGIME = {"RISK_ON": LEG_LONG, "RISK_OFF": LEG_INVERSE}


@dataclass(frozen=True)
class GateCandidate:
    rank: int                 # FREEZE rank (index in brief.watchlist)
    signal_symbol: str        # watchlist symbol
    bias: str
    leg: str                  # LONG | INVERSE
    traded_symbol: str        # symbol actually bought (mapped inverse ETF for INVERSE)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GateResult:
    trading_day: str
    decision: str
    reason_codes: tuple[str, ...]
    leg: str | None = None
    candidates: tuple[GateCandidate, ...] = ()
    observation_only: tuple[dict, ...] = ()
    brief_file_sha256: str | None = None
    artifact_sha256: str | None = None
    schedule: dict | None = None
    contract_sha256: str | None = None
    regime: str | None = None

    def to_dict(self) -> dict:
        return {
            "trading_day": self.trading_day, "decision": self.decision,
            "reason_codes": list(self.reason_codes), "leg": self.leg,
            "candidates": [c.to_dict() for c in self.candidates],
            "observation_only": [dict(item) for item in self.observation_only],
            "brief_file_sha256": self.brief_file_sha256,
            "artifact_sha256": self.artifact_sha256,
            "schedule": self.schedule, "contract_sha256": self.contract_sha256,
            "regime": self.regime,
        }


def freeze_paths(trading_day: date, reports_root: Path) -> tuple[Path, Path]:
    """(brief, operation receipt) paths; names come from src.premarket.operations, the day
    folder under reports_root from src.output_layout."""
    names = stage_paths(Path("."), trading_day, "FREEZE")
    day_dir = reports_root / premarket_day_relpath(trading_day)
    return day_dir / names["brief"].name, day_dir / names["operation"].name


def evaluate_gates(trading_day: date | str, reports_root: Path,
                   contract: ContractBinding) -> GateResult:
    day = date.fromisoformat(trading_day) if isinstance(trading_day, str) else trading_day
    day_iso = day.isoformat()
    brief_path, receipt_path = freeze_paths(day, Path(reports_root))

    def no_trade(*codes: str, **kwargs: Any) -> GateResult:
        return GateResult(day_iso, NO_TRADE, tuple(sorted(set(codes))),
                          contract_sha256=contract.sha256, **kwargs)

    if not brief_path.is_file():
        return no_trade("MISSING_FREEZE_BRIEF")
    if not receipt_path.is_file():
        return no_trade("MISSING_FREEZE_RECEIPT")
    brief_bytes = brief_path.read_bytes()
    file_sha = hashlib.sha256(brief_bytes).hexdigest()
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        artifact = json.loads(brief_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return no_trade("UNREADABLE_FREEZE_ARTIFACT", brief_file_sha256=file_sha)
    if not isinstance(receipt, dict) or not isinstance(artifact, dict):
        return no_trade("UNREADABLE_FREEZE_ARTIFACT", brief_file_sha256=file_sha)

    codes = _receipt_defects(receipt, day_iso, file_sha)
    stored_artifact_sha = artifact.get("artifact_sha256")
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if stored_artifact_sha != sha256_payload(unsigned):
        codes.append("ARTIFACT_SHA256_MISMATCH")
    codes += _artifact_defects(artifact, day_iso)
    common = {"brief_file_sha256": file_sha, "artifact_sha256": stored_artifact_sha}
    if codes:
        return no_trade(*codes, **common)

    snapshot_payload = artifact["snapshot"]["payload"]
    schedule = snapshot_payload.get("schedule")
    if not isinstance(schedule, Mapping) or schedule.get("trading_day") != day_iso:
        return no_trade("SCHEDULE_MISMATCH", **common)
    common["schedule"] = dict(schedule)
    regime = snapshot_payload["input"]["market_regime"]["label"]
    common["regime"] = regime
    leg = _LEG_BY_REGIME.get(regime)
    if leg is None:
        return no_trade("REGIME_NOT_TRADABLE", **common)
    policy = snapshot_payload.get("policy_config")
    allowed = policy.get("allowed_symbols") if isinstance(policy, Mapping) else None
    if not isinstance(allowed, (list, tuple)):
        return no_trade("ARTIFACT_STRUCTURE_INVALID", **common)
    policy_symbols = frozenset(str(v).upper() for v in allowed)

    watchlist = artifact["brief"]["watchlist"]
    if len(watchlist) > contract.max_candidates:
        return no_trade("WATCHLIST_EXCEEDS_MAX_CANDIDATES", leg=leg, **common)
    candidates: list[GateCandidate] = []
    observation: list[dict] = []
    for rank, item in enumerate(watchlist):
        symbol = str(item.get("symbol", "")).upper()
        reason = _item_defect(item, symbol, leg, contract, policy_symbols)
        if reason is not None:
            observation.append({"rank": rank, "symbol": symbol,
                                "bias": item.get("bias"), "reason": reason})
            continue
        traded = contract.inverse_mapping[symbol] if leg == LEG_INVERSE else symbol
        candidates.append(GateCandidate(rank, symbol, item["bias"], leg, traded))
    if not candidates:
        return no_trade("NO_ELIGIBLE_CANDIDATE", leg=leg,
                        observation_only=tuple(observation), **common)
    return GateResult(day_iso, TRADE, (), leg=leg, candidates=tuple(candidates),
                      observation_only=tuple(observation), contract_sha256=contract.sha256,
                      **common)


def _receipt_defects(receipt: Mapping, day_iso: str, file_sha: str) -> list[str]:
    codes: list[str] = []
    if receipt.get("schema") != OPERATION_SCHEMA:
        codes.append("RECEIPT_SCHEMA_MISMATCH")
    if receipt.get("operation") != "PREMARKET_STAGE" or receipt.get("stage") != "FREEZE":
        codes.append("RECEIPT_NOT_FREEZE_STAGE")
    if receipt.get("trading_day") != day_iso:
        codes.append("RECEIPT_TRADING_DAY_MISMATCH")
    if receipt.get("status") != "COMPLETE":
        codes.append("RECEIPT_NOT_COMPLETE")
    unsigned = {k: v for k, v in receipt.items() if k != "artifact_sha256"}
    if receipt.get("artifact_sha256") != sha256_payload(unsigned):
        codes.append("RECEIPT_SHA256_MISMATCH")
    hashes = receipt.get("artifact_file_sha256")
    if not isinstance(hashes, Mapping) or hashes.get("brief") != file_sha:
        codes.append("BRIEF_FILE_SHA256_MISMATCH")
    return codes


def _artifact_defects(artifact: Mapping, day_iso: str) -> list[str]:
    try:
        brief = artifact["brief"]
        payload = artifact["snapshot"]["payload"]
        normalized = payload["input"]
        sources = normalized["sources"]
        regime = normalized["market_regime"]["label"]
        watchlist = brief["watchlist"]
    except (KeyError, TypeError):
        return ["ARTIFACT_STRUCTURE_INVALID"]
    codes: list[str] = []
    if artifact.get("schema") != BRIEF_SCHEMA or brief.get("schema") != BRIEF_SCHEMA:
        codes.append("BRIEF_SCHEMA_MISMATCH")
    if brief.get("trading_day") != day_iso:
        codes.append("BRIEF_TRADING_DAY_MISMATCH")
    if normalized.get("stage") != "FREEZE":
        codes.append("INPUT_STAGE_NOT_FREEZE")
    provenance = normalized.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("run_mode") != "LIVE_WINDOW":
        codes.append("RUN_MODE_NOT_LIVE_WINDOW")
    if not isinstance(sources, list) or not sources or any(
            not isinstance(s, Mapping) or s.get("status") != "OK" for s in sources):
        codes.append("SOURCE_NOT_OK")
    if payload.get("quality_defects") != []:
        codes.append("QUALITY_DEFECTS_PRESENT")
    if brief.get("status") != "SHADOW_READY":
        codes.append("BRIEF_NOT_SHADOW_READY")
    if (brief.get("executable") is not False or brief.get("orders") != []
            or brief.get("promotion_allowed") is not False):
        codes.append("BRIEF_INVARIANT_VIOLATED")
    if not isinstance(regime, str):
        codes.append("REGIME_INVALID")
    if not isinstance(watchlist, list) or any(not isinstance(i, Mapping) for i in watchlist):
        codes.append("WATCHLIST_INVALID")
    return codes


def _item_defect(item: Mapping, symbol: str, leg: str, contract: ContractBinding,
                 policy_symbols: frozenset[str]) -> str | None:
    """Allowlist, not a leverage blacklist: the signal symbol must be in the FREEZE snapshot's
    policy_config.allowed_symbols; the LONG leg additionally needs leverage 1 or an index 3x
    ETF from the contract; the INVERSE leg buys only the contract mapping targets."""
    bias = item.get("bias")
    if item.get("strategy_template_id") != contract.strategy_template_id:
        return "TEMPLATE_ID_MISMATCH"
    if item.get("strategy_template_approved") is not True:
        return "TEMPLATE_NOT_APPROVED"
    if symbol not in policy_symbols:
        return "SYMBOL_NOT_IN_POLICY_UNIVERSE"
    if leg == LEG_LONG:
        if bias != "LONG_WATCH":
            return "BIAS_NOT_LONG_WATCH"
        if leverage_for(symbol) != 1 and symbol not in contract.index_leveraged_etfs:
            return "LEVERAGED_ETF_NOT_ALLOWED"
        return None
    if bias != "SHORT_WATCH":
        return "BIAS_NOT_SHORT_WATCH"
    if symbol not in contract.inverse_mapping:
        return "SHORT_WATCH_OBSERVATION_ONLY"
    return None
