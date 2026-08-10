from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime

import pytest

from src.history.oos_guard import OOSLockError, TRACK_INTRADAY, research_load
from src.models import ET, Candle
from src.research.gross_edge.contract import (
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    compute_json_sha256,
    compute_md_sha256,
    load_contract,
)
from src.research.gross_edge.engine import (
    DataDefectError,
    GrossTrade,
    candidate_specs,
    extract_symbol_day,
)
from src.research.gross_edge.evaluation import (
    evaluate_candidate,
    exact_positive_sign_p,
    overall_verdict,
)
from src.research.gross_edge.report import render_markdown
from src.research.gross_edge.run import canonical_json_bytes
from src.research.orb.windows import Fold


def candle(day: date, minute: int, *, o: float, close: float, symbol: str = "SPY") -> Candle:
    moment = datetime(
        day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=ET
    )
    return Candle.from_moment(
        symbol, "1m", moment, o, max(o, close), min(o, close), close, 1000, "USD", True
    )


def normal_inputs(day: date = date(2024, 1, 3)) -> tuple[list[Candle], list[Candle]]:
    previous_day = date(2024, 1, 2)
    current = [
        candle(day, 570, o=100, close=100.2),
        candle(day, 575, o=101, close=101.1),
        candle(day, 599, o=101.8, close=102),
        candle(day, 600, o=102, close=102.1),
        candle(day, 629, o=102.8, close=103),
        candle(day, 630, o=103, close=103.1),
        candle(day, 719, o=103.8, close=104),
        candle(day, 720, o=104, close=104.1),
        candle(day, 955, o=105, close=105.1),
    ]
    previous = [candle(previous_day, 959, o=98.8, close=99)]
    return current, previous


def test_prereg_hashes_are_frozen_and_registry_has_eight_candidates() -> None:
    assert compute_md_sha256() == PREREG_MD_SHA256
    assert compute_json_sha256() == PREREG_JSON_SHA256
    contract = load_contract()
    assert len(candidate_specs(contract)) == 8
    assert contract["windows"]["blind_oos_access"] == "forbidden"


def test_contract_is_deeply_immutable() -> None:
    contract = load_contract()
    with pytest.raises(TypeError):
        contract["windows"]["blind_oos_access"] = "allowed"


def test_exact_one_sided_sign_test_fixed_value() -> None:
    assert exact_positive_sign_p(6, 6) == pytest.approx(1 / 64)
    assert exact_positive_sign_p(0, 0) == 1.0
    with pytest.raises(ValueError):
        exact_positive_sign_p(2, 1)


def test_all_candidates_use_next_known_entry_and_are_direction_symmetric() -> None:
    contract = load_contract()
    current, previous = normal_inputs()
    trades = extract_symbol_day(
        symbol="SPY",
        day=date(2024, 1, 3),
        current=current,
        previous=previous,
        specs=candidate_specs(contract),
    )
    assert len(trades) == 8
    values = {trade.candidate_id: trade.gross_return_pct_points for trade in trades}
    for family in ("gap", "opening_30m", "first_hour", "morning"):
        assert values[f"{family}_continuation"] == pytest.approx(
            -values[f"{family}_reversal"]
        )
    assert values["opening_30m_continuation"] == pytest.approx((105 / 102 - 1) * 100)


def test_extraction_is_input_order_invariant() -> None:
    contract = load_contract()
    current, previous = normal_inputs()
    forward = extract_symbol_day(
        symbol="SPY", day=date(2024, 1, 3), current=current, previous=previous,
        specs=candidate_specs(contract),
    )
    reverse = extract_symbol_day(
        symbol="SPY", day=date(2024, 1, 3), current=list(reversed(current)),
        previous=list(reversed(previous)), specs=candidate_specs(contract),
    )
    assert forward == reverse


def test_duplicate_minute_fails_closed() -> None:
    contract = load_contract()
    current, previous = normal_inputs()
    current.append(candle(date(2024, 1, 3), 570, o=999, close=999))
    with pytest.raises(DataDefectError, match="duplicate regular minute"):
        extract_symbol_day(
            symbol="SPY", day=date(2024, 1, 3), current=current, previous=previous,
            specs=candidate_specs(contract),
        )


def test_previous_close_must_be_from_exact_previous_trading_day() -> None:
    contract = load_contract()
    current, _ = normal_inputs()
    stale = [candle(date(2023, 12, 29), 959, o=98, close=99)]
    with pytest.raises(DataDefectError, match="cross-day"):
        extract_symbol_day(
            symbol="SPY", day=date(2024, 1, 3), current=current, previous=stale,
            specs=candidate_specs(contract),
        )


def test_current_early_close_day_is_excluded() -> None:
    contract = load_contract()
    current, previous = normal_inputs(date(2023, 7, 3))
    assert extract_symbol_day(
        symbol="SPY", day=date(2023, 7, 3), current=current, previous=previous,
        specs=candidate_specs(contract),
    ) == ()


def test_daily_equal_weighting_uses_days_as_sign_test_units() -> None:
    contract = load_contract()
    trades = (
        GrossTrade("gap_continuation", "SPY", date(2024, 1, 3), 1.0),
        GrossTrade("gap_continuation", "QQQ", date(2024, 1, 3), -1.0),
        GrossTrade("gap_continuation", "SPY", date(2024, 1, 4), 2.0),
        GrossTrade("gap_continuation", "QQQ", date(2024, 1, 4), 2.0),
    )
    folds = (Fold("F01", date(2024, 1, 3), date(2024, 1, 4), 2),)
    result = evaluate_candidate(
        candidate_id="gap_continuation", trades=trades, folds=folds, contract=contract
    )
    assert result.trade_count == 4
    assert result.trading_days == 2
    assert result.mean_daily_gross_pct_points == 1.0
    assert result.nonzero_days == 1
    assert result.positive_days == 1
    assert result.sign_test_raw_p == 0.5
    assert result.sign_test_bonferroni_p == 1.0


def test_overall_verdict_distinguishes_gross_only_and_shortlist() -> None:
    contract = load_contract()
    folds = (Fold("F01", date(2024, 1, 3), date(2024, 1, 3), 1),)
    base = evaluate_candidate(
        candidate_id="gap_continuation", trades=(), folds=folds, contract=contract
    )
    assert overall_verdict((base,)) == "NO_GROSS_EDGE"
    gross_only = replace(base, gross_edge_pass=True, economic_pass=False)
    assert overall_verdict((gross_only,)) == "GROSS_EDGE_NOT_ECONOMIC"
    shortlist = replace(base, gross_edge_pass=True, economic_pass=True)
    assert overall_verdict((shortlist,)) == "SHORTLIST"


def test_result_serialization_and_markdown_are_deterministic_and_mapped() -> None:
    result = {
        "verdict": "NO_GROSS_EDGE",
        "development_window": {"start": "a", "end": "b", "trading_days": 1},
        "universe_count": 30,
        "candidate_count": 1,
        "blind_oos_accessed": False,
        "payload_sha256": "abc",
        "candidates": [{
            "candidate_id": "x", "trade_count": 1, "trading_days": 1,
            "mean_daily_gross_pct_points": 0.1, "positive_folds": 1,
            "sign_test_bonferroni_p": 0.5,
            "net_after_cost_pct_points": {
                "published_floor": -0.104, "conservative": -0.24, "stress": -0.58,
            },
            "gross_edge_pass": False, "economic_pass": False,
        }],
        "shortlisted_candidates": [], "gross_edge_only_candidates": [],
        "data_identity": {
            name: {"status": "match"}
            for name in ("snapshot_file", "snapshot_content", "manifest_data", "raw_content")
        },
    }
    assert canonical_json_bytes(result) == canonical_json_bytes(json.loads(canonical_json_bytes(result)))
    report = render_markdown(result)
    assert "x | 1 | 1 | 0.100000" in report
    assert "snapshot_file: `match`" in report


def test_blind_oos_remains_fail_closed_before_database_access() -> None:
    with pytest.raises(OOSLockError):
        research_load(
            "/does/not/exist.sqlite", "SPY", "1m",
            start=datetime(2026, 1, 21, tzinfo=ET),
            end=datetime(2026, 1, 22, tzinfo=ET),
            purpose="performance", track=TRACK_INTRADAY, market="US",
        )
