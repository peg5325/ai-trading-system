from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src.premarket.contract import PremarketConfig, build_snapshot
from src.premarket.planner import build_daily_brief
from src.premarket.report import (
    json_bytes,
    render_markdown,
    write_immutable,
    write_immutable_pair,
)
from src.premarket.schedule import build_premarket_schedule

ROOT = Path(__file__).resolve().parents[1]


def _config(
    *,
    approved: tuple[str, ...] = (),
    approved_short: tuple[str, ...] = (),
    min_catalyst_score: float = 0.0,
) -> PremarketConfig:
    return PremarketConfig.from_mapping({
        "schema": "premarket-config/v1",
        "required_source_kinds": ["MARKET", "NEWS"],
        "source_freshness_minutes": 180,
        "primary_offset_minutes": 90,
        "verification_offset_minutes": 30,
        "freeze_offset_minutes": 20,
        "max_candidates": 3,
        "min_liquidity_score": 0.5,
        "min_catalyst_score": min_catalyst_score,
        "approved_strategy_templates": list(approved),
        "approved_short_strategy_templates": list(approved_short),
        "allowed_symbols": ["AAPL", "MSFT", "AAA", "BBB", "CCC", "DDD"],
        "excluded_universe_classes": ["inverse_etf"],
        "weights": {
            "catalyst_score": 0.30,
            "trend_score": 0.25,
            "relative_strength_score": 0.20,
            "volume_score": 0.15,
            "liquidity_score": 0.10,
        },
    })


def _input() -> dict:
    return {
        "schema": "premarket-input/v1",
        "market": "US",
        "trading_day": "2026-03-09",  # EDT; open 13:30Z, freeze 13:10Z
        "as_of": "2026-03-09T12:00:00Z",
        "sources": [
            {
                "source_id": "market-primary",
                "kind": "MARKET",
                "status": "OK",
                "observed_at": "2026-03-09T11:58:00Z",
                "available_at": "2026-03-09T11:58:01Z",
                "ingested_at": "2026-03-09T11:58:02Z",
                "source_url": "https://evidence.invalid/market/1",
                "content_sha256": "a" * 64,
            },
            {
                "source_id": "news-primary",
                "kind": "NEWS",
                "status": "OK",
                "observed_at": "2026-03-09T11:55:00Z",
                "available_at": "2026-03-09T11:55:01Z",
                "ingested_at": "2026-03-09T11:55:02Z",
                "source_url": "https://evidence.invalid/news/receipt",
                "content_sha256": "b" * 64,
            },
        ],
        "events": [
            {
                "event_id": "event-aapl",
                "source_id": "news-primary",
                "published_at": "2026-03-09T11:40:00Z",
                "available_at": "2026-03-09T11:40:01Z",
                "ingested_at": "2026-03-09T11:40:02Z",
                "source_url": "https://evidence.invalid/news/aapl",
                "content_sha256": "c" * 64,
                "symbols": ["AAPL"],
                "sentiment": 0.7,
            },
            {
                "event_id": "event-msft",
                "source_id": "news-primary",
                "published_at": "2026-03-09T11:42:00Z",
                "available_at": "2026-03-09T11:42:01Z",
                "ingested_at": "2026-03-09T11:42:02Z",
                "source_url": "https://evidence.invalid/news/msft",
                "content_sha256": "d" * 64,
                "symbols": ["MSFT"],
                "sentiment": 0.5,
            },
        ],
        "candidates": [
            {
                "symbol": "AAPL",
                "strategy_template_id": "orb-v1",
                "available_at": "2026-03-09T11:50:00Z",
                "ingested_at": "2026-03-09T11:50:01Z",
                "evidence_event_ids": ["event-aapl"],
                "source_ids": ["market-primary", "news-primary"],
                "catalyst_score": 0.9,
                "trend_score": 0.8,
                "relative_strength_score": 0.7,
                "volume_score": 0.8,
                "liquidity_score": 0.9,
            },
            {
                "symbol": "MSFT",
                "strategy_template_id": None,
                "available_at": "2026-03-09T11:51:00Z",
                "ingested_at": "2026-03-09T11:51:01Z",
                "evidence_event_ids": ["event-msft"],
                "source_ids": ["market-primary", "news-primary"],
                "catalyst_score": 0.6,
                "trend_score": 0.7,
                "relative_strength_score": 0.8,
                "volume_score": 0.6,
                "liquidity_score": 0.95,
            },
        ],
        "market_regime": {
            "label": "RISK_ON",
            "available_at": "2026-03-09T11:59:00Z",
            "ingested_at": "2026-03-09T11:59:01Z",
            "source_ids": ["market-primary"],
        },
    }


def _schedule():
    return build_premarket_schedule(date(2026, 3, 9))


def _short_input() -> dict:
    data = _input()
    data["market_regime"]["label"] = "RISK_OFF"
    data["candidates"][0].update(
        catalyst_score=-0.9,
        trend_score=0.2,
        relative_strength_score=0.3,
    )
    data["candidates"][1].update(
        catalyst_score=-0.6,
        trend_score=0.3,
        relative_strength_score=0.2,
    )
    return data


def test_schedule_is_dst_safe_and_exchange_relative():
    winter = build_premarket_schedule(date(2026, 3, 6))
    summer = build_premarket_schedule(date(2026, 3, 9))
    assert winter.market_open_utc == "2026-03-06T14:30:00Z"
    assert winter.primary_run_utc == "2026-03-06T13:00:00Z"
    assert summer.market_open_utc == "2026-03-09T13:30:00Z"
    assert summer.primary_run_utc == "2026-03-09T12:00:00Z"
    assert summer.freeze_utc == "2026-03-09T13:10:00Z"


def test_schedule_rejects_holiday_and_handles_early_close():
    with pytest.raises(ValueError, match="not an NYSE trading day"):
        build_premarket_schedule(date(2026, 9, 7))
    early = build_premarket_schedule(date(2026, 11, 27))
    assert early.market_close_utc == "2026-11-27T18:00:00Z"


def test_default_config_shows_watchlist_but_fails_closed_without_strategy():
    result = build_daily_brief(_input(), schedule=_schedule(), config=_config())
    assert result["brief"]["status"] == "NO_TRADE"
    assert result["brief"]["reason_codes"] == ["NO_APPROVED_STRATEGY_TEMPLATE"]
    assert [item["symbol"] for item in result["brief"]["watchlist"]] == ["AAPL", "MSFT"]
    assert result["brief"]["orders"] == []
    assert result["brief"]["executable"] is False
    assert result["brief"]["promotion_allowed"] is False
    assert {item["bias"] for item in result["brief"]["watchlist"]} == {"LONG_WATCH"}


def test_approved_template_only_changes_shadow_status_not_execution_authority():
    result = build_daily_brief(_input(), schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "SHADOW_READY"
    assert result["brief"]["orders"] == []
    assert result["brief"]["executable"] is False
    assert result["brief"]["promotion_allowed"] is False


def test_risk_off_publishes_ranked_short_watchlist_but_defaults_to_cash():
    result = build_daily_brief(_short_input(), schedule=_schedule(), config=_config())

    assert result["brief"]["status"] == "NO_TRADE"
    assert result["brief"]["direction_policy"] == "LONG_SHORT_OR_CASH"
    assert result["brief"]["reason_codes"] == ["NO_APPROVED_STRATEGY_TEMPLATE"]
    assert [item["symbol"] for item in result["brief"]["watchlist"]] == ["AAPL", "MSFT"]
    assert {item["bias"] for item in result["brief"]["watchlist"]} == {"SHORT_WATCH"}
    assert result["brief"]["watchlist"][0]["score"] == pytest.approx(0.82)
    assert result["brief"]["orders"] == []


def test_long_template_approval_never_authorizes_short_shadow_readiness():
    result = build_daily_brief(
        _short_input(),
        schedule=_schedule(),
        config=_config(approved=("orb-v1",)),
    )

    assert result["brief"]["status"] == "NO_TRADE"
    assert all(
        item["strategy_template_approved"] is False
        for item in result["brief"]["watchlist"]
    )


def test_short_template_approval_only_changes_shadow_status_not_order_authority():
    result = build_daily_brief(
        _short_input(),
        schedule=_schedule(),
        config=_config(approved_short=("orb-v1",)),
    )

    assert result["brief"]["status"] == "SHADOW_READY"
    assert result["brief"]["orders"] == []
    assert result["brief"]["executable"] is False
    assert result["brief"]["promotion_allowed"] is False


def test_risk_off_never_turns_positive_catalyst_into_a_short_candidate():
    data = _input()
    data["market_regime"]["label"] = "RISK_OFF"
    result = build_daily_brief(data, schedule=_schedule(), config=_config())

    assert result["brief"]["status"] == "NO_TRADE"
    assert result["brief"]["watchlist"] == []
    assert result["brief"]["reason_codes"] == ["NO_ELIGIBLE_CANDIDATE"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda data: data["sources"].pop(), "MISSING_OR_UNHEALTHY_NEWS_SOURCE"),
        (lambda data: data["sources"][1].update(observed_at="2026-03-09T08:00:00Z"),
         "STALE_NEWS_SOURCE"),
        (lambda data: data["events"][0].update(available_at="2026-03-09T12:01:00Z"),
         "FUTURE_EVENT_EVIDENCE"),
        (lambda data: data["candidates"][0].update(evidence_event_ids=["missing"]),
         "UNKNOWN_CANDIDATE_EVENT"),
    ],
)
def test_quality_and_regime_fail_closed(mutate, reason):
    data = _input()
    mutate(data)
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert reason in result["brief"]["reason_codes"]
    assert result["brief"]["orders"] == []
    if reason.startswith("MARKET_REGIME_"):
        assert result["brief"]["watchlist"] == []


def test_uncertain_regime_does_not_publish_long_watchlist():
    data = _input()
    data["market_regime"]["label"] = "UNCERTAIN"
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert result["brief"]["watchlist"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["sources"][0].pop("source_id"),
        lambda data: data["events"][0].pop("event_id"),
        lambda data: data["candidates"][0].pop("symbol"),
        lambda data: data["candidates"][0].update(source_ids=[None]),
        lambda data: data["candidates"][0].update(evidence_event_ids=[None]),
        lambda data: data["market_regime"].update(source_ids=[None]),
    ],
)
def test_missing_or_non_string_identity_never_becomes_literal_none(mutate):
    data = _input()
    mutate(data)
    with pytest.raises(TypeError, match="must be a string"):
        build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))


def test_conflicting_revision_fails_closed():
    data = _input()
    revision = copy.deepcopy(data["events"][0])
    revision["content_sha256"] = "e" * 64
    data["events"].append(revision)
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert "CONFLICTING_EVENT_REVISION" in result["brief"]["reason_codes"]


def test_after_freeze_fails_closed():
    data = _input()
    data["as_of"] = "2026-03-09T13:11:00Z"
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert "AFTER_FREEZE_CUTOFF" in result["brief"]["reason_codes"]


def test_before_primary_run_fails_closed():
    data = _input()
    data["as_of"] = "2026-03-09T11:59:59Z"
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert "BEFORE_PRIMARY_RUN" in result["brief"]["reason_codes"]


def test_universe_source_and_event_symbol_links_fail_closed():
    data = _input()
    data["candidates"][0]["symbol"] = "SQQQ"
    data["candidates"][0]["source_ids"] = ["missing"]
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    reasons = result["brief"]["reason_codes"]
    assert "SYMBOL_OUTSIDE_ALLOWED_UNIVERSE" in reasons
    assert "UNKNOWN_CANDIDATE_SOURCE" in reasons
    assert "CANDIDATE_EVENT_SYMBOL_MISMATCH" in reasons


def test_used_unhealthy_source_fails_closed_even_if_kind_has_another_healthy_source():
    data = _input()
    failed = copy.deepcopy(data["sources"][1])
    failed.update(source_id="news-failed", status="ERROR", content_sha256="f" * 64)
    data["sources"].append(failed)
    data["events"][0]["source_id"] = "news-failed"
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert "UNHEALTHY_EVENT_SOURCE" in result["brief"]["reason_codes"]
    assert result["brief"]["orders"] == []
    assert result["brief"]["executable"] is False
    assert result["brief"]["promotion_allowed"] is False


@pytest.mark.parametrize(
    ("consumer", "reason"),
    [
        ("event", "STALE_EVENT_SOURCE"),
        ("candidate", "STALE_CANDIDATE_SOURCE"),
        ("regime", "STALE_REGIME_SOURCE"),
    ],
)
def test_each_used_source_must_be_fresh_even_if_required_kinds_are_healthy(consumer, reason):
    data = _input()
    stale = {
        "source_id": "optional-stale",
        "kind": "FILING",
        "status": "OK",
        "observed_at": "2026-03-09T08:00:00Z",
        "available_at": "2026-03-09T08:00:01Z",
        "ingested_at": "2026-03-09T08:00:02Z",
        "source_url": "https://evidence.invalid/filing/stale",
        "content_sha256": "f" * 64,
    }
    data["sources"].append(stale)
    if consumer == "event":
        data["events"][0]["source_id"] = "optional-stale"
    elif consumer == "candidate":
        data["candidates"][0]["source_ids"].append("optional-stale")
    else:
        data["market_regime"]["source_ids"] = ["optional-stale"]
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert reason in result["brief"]["reason_codes"]
    assert result["brief"]["orders"] == []
    assert result["brief"]["executable"] is False
    assert result["brief"]["promotion_allowed"] is False


def test_identical_duplicate_candidate_is_deduplicated():
    data = _input()
    data["candidates"].append(copy.deepcopy(data["candidates"][0]))
    result = build_daily_brief(data, schedule=_schedule(), config=_config())
    assert [item["symbol"] for item in result["brief"]["watchlist"]] == ["AAPL", "MSFT"]


def test_one_candidate_missing_event_join_fails_entire_run_closed():
    data = _input()
    data["candidates"][1].pop("evidence_event_ids")
    result = build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))
    assert result["brief"]["status"] == "NO_TRADE"
    assert result["brief"]["watchlist"] == []
    assert "MISSING_CANDIDATE_EVENT" in result["brief"]["reason_codes"]
    assert result["brief"]["orders"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["events"][0].update(symbols="AAPL"),
        lambda data: data["candidates"][0].update(evidence_event_ids="event-aapl"),
        lambda data: data["candidates"][0].update(source_ids="market-primary"),
        lambda data: data["market_regime"].update(source_ids="market-primary"),
    ],
)
def test_identity_collections_must_be_lists(mutate):
    data = _input()
    mutate(data)
    with pytest.raises(TypeError, match="must be a list"):
        build_daily_brief(data, schedule=_schedule(), config=_config(approved=("orb-v1",)))


def test_snapshot_and_rank_are_input_order_invariant():
    first = _input()
    second = copy.deepcopy(first)
    second["sources"].reverse()
    second["events"].reverse()
    second["candidates"].reverse()
    second["market_regime"]["source_ids"].reverse()
    a = build_snapshot(first, _schedule(), _config())
    b = build_snapshot(second, _schedule(), _config())
    assert a == b
    assert build_daily_brief(first, schedule=_schedule(), config=_config()) == build_daily_brief(
        second, schedule=_schedule(), config=_config())


def test_max_three_and_symbol_tie_break():
    data = _input()
    data["events"][0]["symbols"] = ["AAA", "BBB", "CCC", "DDD"]
    base = copy.deepcopy(data["candidates"][0])
    base.update(strategy_template_id=None, evidence_event_ids=["event-aapl"])
    data["candidates"] = []
    for symbol in ["DDD", "CCC", "BBB", "AAA"]:
        item = copy.deepcopy(base)
        item["symbol"] = symbol
        data["candidates"].append(item)
    result = build_daily_brief(data, schedule=_schedule(), config=_config())
    assert [item["symbol"] for item in result["brief"]["watchlist"]] == ["AAA", "BBB", "CCC"]


def test_markdown_contains_hash_urls_and_read_only_boundary():
    result = build_daily_brief(_input(), schedule=_schedule(), config=_config())
    rendered = render_markdown(result)
    assert result["brief"]["snapshot_sha256"] in rendered
    assert "https://evidence.invalid/news/aapl" in rendered
    assert "주문 생성: **0건**" in rendered

    short_rendered = render_markdown(
        build_daily_brief(_short_input(), schedule=_schedule(), config=_config())
    )
    assert "SHORT_WATCH" in short_rendered
    assert "직접 공매도 주문" in short_rendered


def test_immutable_write_is_idempotent_and_refuses_changed_content(tmp_path):
    target = tmp_path / "result.json"
    write_immutable(target, b"same\n")
    write_immutable(target, b"same\n")
    with pytest.raises(FileExistsError, match="immutable artifact differs"):
        write_immutable(target, b"changed\n")
    assert not (tmp_path / "result.json.partial").exists()


def test_pair_preflight_does_not_create_first_when_second_conflicts(tmp_path):
    first = tmp_path / "brief.json"
    second = tmp_path / "brief.md"
    second.write_bytes(b"old")
    with pytest.raises(FileExistsError, match="immutable artifact differs"):
        write_immutable_pair((first, b"new-json"), (second, b"new-md"))
    assert not first.exists()


def test_cli_end_to_end_is_deterministic_and_default_no_trade(tmp_path):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_input()), encoding="utf-8")
    output_json = tmp_path / "brief.json"
    output_md = tmp_path / "brief.md"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "premarket_daily_brief.py"),
        "--input", str(input_path),
        "--config", str(ROOT / "config" / "premarket_daily_brief.yaml"),
        "--out-json", str(output_json),
        "--out-markdown", str(output_md),
    ]
    first = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)
    before = output_json.read_bytes()
    second = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)
    assert first.stdout == second.stdout
    assert output_json.read_bytes() == before
    result = json.loads(before)
    assert result["brief"]["status"] == "NO_TRADE"
    assert json_bytes(result) == before


def test_package_and_cli_have_no_network_order_or_live_imports():
    paths = list((ROOT / "src" / "premarket").glob("*.py")) + [
        ROOT / "scripts" / "premarket_daily_brief.py"
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "src.order", "src.live", "src.broker", "toss_client", "httpx", "requests",
        "urllib.request", "submit_order", "place_order",
    )
    assert {token for token in forbidden if token in source} == set()


def test_config_validation_rejects_weights_or_more_than_three_candidates():
    raw = {
        "schema": "premarket-config/v1",
        "required_source_kinds": ["MARKET"],
        "weights": {name: 0.1 for name in (
            "catalyst_score", "trend_score", "relative_strength_score",
            "volume_score", "liquidity_score",
        )},
    }
    with pytest.raises(ValueError, match="sum to 1"):
        PremarketConfig.from_mapping(raw)
    raw["weights"] = {name: 0.2 for name in raw["weights"]}
    raw["max_candidates"] = 4
    with pytest.raises(ValueError, match="between 1 and 3"):
        PremarketConfig.from_mapping(raw)


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_config_rejects_catalyst_threshold_outside_symmetric_range(threshold):
    with pytest.raises(ValueError, match="min_catalyst_score must be in"):
        _config(min_catalyst_score=threshold)
