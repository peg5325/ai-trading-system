"""Deterministic LONG/CASH-only shadow watchlist planner."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.premarket.contract import (
    SCORE_FIELDS,
    PremarketConfig,
    build_snapshot,
    sha256_payload,
)
from src.premarket.schedule import PremarketSchedule

BRIEF_SCHEMA = "premarket-daily-brief/v1"


def _score(candidate: Mapping[str, Any], config: PremarketConfig) -> float:
    return sum(float(candidate[field]) * config.weights[field] for field in SCORE_FIELDS)


def build_daily_brief(data: Mapping[str, Any], *, schedule: PremarketSchedule,
                      config: PremarketConfig) -> dict:
    """Create an auditable shadow watchlist; never create or authorize orders."""
    snapshot = build_snapshot(data, schedule, config)
    normalized = snapshot["payload"]["input"]
    defects = list(snapshot["payload"]["quality_defects"])
    regime = normalized["market_regime"]["label"]
    reasons = list(defects)
    if regime != "RISK_ON":
        reasons.append(f"MARKET_REGIME_{regime}")

    event_by_id = {item["event_id"]: item for item in normalized["events"]}
    ranked: list[dict] = []
    if not defects and regime == "RISK_ON":
        for candidate in normalized["candidates"]:
            if candidate["liquidity_score"] < config.min_liquidity_score:
                continue
            if candidate["catalyst_score"] <= config.min_catalyst_score:
                continue
            evidence = [event_by_id[eid] for eid in candidate["evidence_event_ids"]]
            if not evidence:
                continue
            ranked.append({
                "symbol": candidate["symbol"],
                "bias": "LONG_WATCH",
                "score": round(_score(candidate, config), 12),
                "strategy_template_id": candidate["strategy_template_id"],
                "strategy_template_approved": (
                    candidate["strategy_template_id"] in config.approved_strategy_templates
                ),
                "evidence": [{
                    "event_id": item["event_id"],
                    "source_id": item["source_id"],
                    "source_url": item["source_url"],
                    "content_sha256": item["content_sha256"],
                } for item in evidence],
            })
    ranked.sort(key=lambda item: (-item["score"], item["symbol"]))
    watchlist = ranked[:config.max_candidates]
    approved = [item for item in watchlist if item["strategy_template_approved"]]

    if not defects and regime == "RISK_ON" and approved:
        status = "SHADOW_READY"
    else:
        status = "NO_TRADE"
        if not defects and regime == "RISK_ON" and not approved:
            reasons.append("NO_APPROVED_STRATEGY_TEMPLATE")
        if not watchlist and not defects:
            reasons.append("NO_ELIGIBLE_CANDIDATE")

    payload = {
        "schema": BRIEF_SCHEMA,
        "run_id": snapshot["run_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "trading_day": schedule.trading_day,
        "as_of": normalized["as_of"],
        "expires_at": schedule.freeze_utc,
        "status": status,
        "direction_policy": "LONG_OR_CASH",
        "execution_mode": "READ_ONLY_SHADOW",
        "executable": False,
        "promotion_allowed": False,
        "orders": [],
        "reason_codes": sorted(set(reasons)),
        "watchlist": watchlist,
    }
    result = {
        "schema": BRIEF_SCHEMA,
        "snapshot": snapshot,
        "brief": payload,
    }
    result["artifact_sha256"] = sha256_payload(result)
    return result
