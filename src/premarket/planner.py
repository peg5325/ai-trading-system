"""Deterministic LONG/SHORT/CASH shadow watchlist planner."""
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

BRIEF_SCHEMA = "premarket-daily-brief/v2"


def _directional_score(
    candidate: Mapping[str, Any],
    config: PremarketConfig,
    bias: str,
) -> float:
    components = {field: float(candidate[field]) for field in SCORE_FIELDS}
    if bias == "SHORT_WATCH":
        components["catalyst_score"] = -components["catalyst_score"]
        components["trend_score"] = 1.0 - components["trend_score"]
        components["relative_strength_score"] = (
            1.0 - components["relative_strength_score"]
        )
    return sum(components[field] * config.weights[field] for field in SCORE_FIELDS)


def _bias_for_regime(regime: str) -> str | None:
    return {
        "RISK_ON": "LONG_WATCH",
        "RISK_OFF": "SHORT_WATCH",
    }.get(regime)


def _approved_templates(config: PremarketConfig, bias: str) -> tuple[str, ...]:
    if bias == "SHORT_WATCH":
        return config.approved_short_strategy_templates
    return config.approved_strategy_templates


def build_daily_brief(data: Mapping[str, Any], *, schedule: PremarketSchedule,
                      config: PremarketConfig) -> dict:
    """Create an auditable shadow watchlist; never create or authorize orders."""
    snapshot = build_snapshot(data, schedule, config)
    normalized = snapshot["payload"]["input"]
    defects = list(snapshot["payload"]["quality_defects"])
    regime = normalized["market_regime"]["label"]
    bias = _bias_for_regime(regime)
    reasons = list(defects)
    if bias is None:
        reasons.append(f"MARKET_REGIME_{regime}")

    event_by_id = {item["event_id"]: item for item in normalized["events"]}
    ranked: list[dict] = []
    if not defects and bias is not None:
        approved_templates = _approved_templates(config, bias)
        for candidate in normalized["candidates"]:
            if candidate["liquidity_score"] < config.min_liquidity_score:
                continue
            catalyst = float(candidate["catalyst_score"])
            if bias == "LONG_WATCH" and catalyst <= config.min_catalyst_score:
                continue
            if bias == "SHORT_WATCH" and catalyst >= -config.min_catalyst_score:
                continue
            evidence = [event_by_id[eid] for eid in candidate["evidence_event_ids"]]
            if not evidence:
                continue
            ranked.append({
                "symbol": candidate["symbol"],
                "bias": bias,
                "score": round(_directional_score(candidate, config, bias), 12),
                "strategy_template_id": candidate["strategy_template_id"],
                "strategy_template_approved": (
                    candidate["strategy_template_id"] in approved_templates
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

    if not defects and bias is not None and approved:
        status = "SHADOW_READY"
    else:
        status = "NO_TRADE"
        if not defects and bias is not None and watchlist and not approved:
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
        "direction_policy": "LONG_SHORT_OR_CASH",
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
