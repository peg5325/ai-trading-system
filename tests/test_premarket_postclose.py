from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.history.exchange_calendar import expected_regular_minutes, regular_close_minute
from src.history.store import SCHEMA
from src.models import ET, Candle
from src.premarket.contract import sha256_payload
from src.premarket.postclose import (
    build_cumulative_report,
    build_postclose_evaluation,
    evaluate_symbol_session,
    load_or_create_ledger,
    upsert_ledger_entry,
)

ROOT = Path(__file__).resolve().parents[1]


def _freeze_brief(trading_day: str = "2026-09-21") -> dict:
    result = {
        "schema": "premarket-daily-brief/v2",
        "brief": {
            "schema": "premarket-daily-brief/v2",
            "trading_day": trading_day,
            "as_of": f"{trading_day}T13:10:00Z",
            "expires_at": f"{trading_day}T13:10:00Z",
            "run_id": f"premarket-{trading_day}-fixture",
            "snapshot_sha256": "b" * 64,
            "status": "NO_TRADE",
            "executable": False,
            "reason_codes": ["NO_APPROVED_STRATEGY_TEMPLATE"],
            "watchlist": [
                {
                    "symbol": "AAA",
                    "bias": "LONG_WATCH",
                    "score": 0.9,
                    "strategy_template_id": None,
                    "strategy_template_approved": False,
                    "evidence": [{"event_id": "event-aaa"}],
                },
                {
                    "symbol": "BBB",
                    "bias": "SHORT_WATCH",
                    "score": 0.8,
                    "strategy_template_id": None,
                    "strategy_template_approved": False,
                    "evidence": [{"event_id": "event-bbb"}],
                },
            ],
        },
        "snapshot": {
            "payload": {
                "input": {"stage": "FREEZE"},
                "schedule": {
                    "freeze_utc": f"{trading_day}T13:10:00Z",
                },
            },
        },
    }
    result["artifact_sha256"] = sha256_payload(result)
    return result


def _refresh_freeze_artifact(brief: dict) -> None:
    brief.pop("artifact_sha256", None)
    brief["artifact_sha256"] = sha256_payload(brief)


def _make_db(tmp_path: Path, rows: dict[str, list[Candle]]) -> Path:
    db = tmp_path / "market.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for symbol_rows in rows.values():
        for bar in symbol_rows:
            con.execute(
                "INSERT INTO candle_history "
                "(symbol,market,interval,ts_utc,ts_exchange,session,open,high,low,close,"
                "volume,currency,adjusted,source,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bar.symbol,
                    bar.market,
                    bar.interval,
                    bar.ts_utc.isoformat(),
                    bar.ts_exchange.isoformat(),
                    bar.session,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.currency,
                    int(bar.adjusted),
                    "test",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
    con.commit()
    con.close()
    return db


def _regular_bars(
    symbol: str,
    day: date,
    *,
    first_open: float = 100.0,
    last_close: float = 101.0,
    minutes: int | None = None,
) -> list[Candle]:
    expected = expected_regular_minutes(day)
    count = minutes if minutes is not None else expected
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
    bars = []
    for index in range(count):
        ts = start + timedelta(minutes=index)
        fraction = index / max(count - 1, 1)
        close = first_open + (last_close - first_open) * fraction
        open_ = first_open if index == 0 else close
        bars.append(
            Candle.from_moment(
                symbol,
                "1m",
                ts,
                open_,
                max(open_, close) + 0.1,
                min(open_, close) - 0.1,
                close,
                1000.0 + index,
                "USD",
                True,
                market="US",
            )
        )
    return bars


def test_complete_postclose_evaluation_tracks_costs_and_spy_diagnostic(tmp_path):
    day = date(2026, 9, 21)  # EDT; outside the fixed blind-OOS window
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day, first_open=100.0, last_close=102.0),
        "BBB": _regular_bars("BBB", day, first_open=50.0, last_close=49.0),
        "SPY": _regular_bars("SPY", day, first_open=400.0, last_close=404.0),
    })

    result = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )

    assert result["status"] == "COMPLETE"
    assert result["benchmark"]["official_sp500_total_return_status"] == "INCONCLUSIVE_BENCHMARK"
    assert [row["status"] for row in result["candidates"]] == ["COMPLETE", "COMPLETE"]
    assert result["candidates"][0]["gross_return"] == pytest.approx(0.02)
    assert result["candidates"][1]["gross_return"] == pytest.approx(0.02)
    assert result["portfolio_proxy"]["gross_return"] == pytest.approx(0.02)
    assert result["portfolio_proxy"]["net_return_official_cost"] == pytest.approx(0.0166)
    assert result["portfolio_proxy"]["net_return_stress_cost"] == pytest.approx(0.0132)
    assert result["portfolio_proxy"]["official_cost_excess_vs_spy_diagnostic"] == pytest.approx(
        0.0066
    )


def test_spy_diagnostic_remains_long_when_spy_is_a_short_watch(tmp_path):
    day = date(2026, 9, 21)
    brief = _freeze_brief(day.isoformat())
    brief["brief"]["watchlist"] = [{
        "symbol": "SPY",
        "bias": "SHORT_WATCH",
        "score": 0.9,
        "strategy_template_id": None,
        "strategy_template_approved": False,
        "evidence": [{"event_id": "event-spy"}],
    }]
    _refresh_freeze_artifact(brief)
    db = _make_db(tmp_path, {
        "SPY": _regular_bars("SPY", day, first_open=400.0, last_close=404.0),
    })

    result = build_postclose_evaluation(
        brief,
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )

    assert result["candidates"][0]["direction"] == "SHORT_WATCH"
    assert result["candidates"][0]["gross_return"] == pytest.approx(-0.01)
    assert result["spy_diagnostic"]["direction"] == "LONG_WATCH"
    assert result["spy_diagnostic"]["gross_return"] == pytest.approx(0.01)


def test_pre_close_evaluation_is_blocked_even_if_bars_exist(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {"AAA": _regular_bars("AAA", day), "BBB": _regular_bars("BBB", day)})

    result = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-09-21T19:59:59Z",
    )

    assert result["status"] == "INCOMPLETE_PRE_CLOSE"
    assert result["reason_codes"] == ["PRE_CLOSE_EVALUATION_BLOCKED"]
    assert result["candidates"] == []


def test_missing_regular_bars_are_incomplete_not_zero_filled(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day, minutes=389),
        "BBB": _regular_bars("BBB", day),
    })

    result = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )

    assert result["status"] == "INCOMPLETE_MARKET_DATA"
    assert "INCOMPLETE_REGULAR_SESSION" in result["reason_codes"]
    assert result["portfolio_proxy"]["gross_return"] is None
    assert result["candidates"][0]["observed_regular_minutes"] == 389


def test_early_close_uses_exchange_calendar_expected_minutes(tmp_path):
    day = date(2026, 11, 27)
    assert regular_close_minute(day) == 13 * 60
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })

    result = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-11-27T18:00:00Z",
    )

    assert result["status"] == "COMPLETE"
    assert result["candidates"][0]["expected_regular_minutes"] == 210
    assert result["candidates"][0]["observed_regular_minutes"] == 210


def test_duplicate_or_unsupported_direction_fails_closed():
    day = date(2026, 9, 21)
    bars = _regular_bars("AAA", day)
    duplicated = [bars[0], *bars]

    duplicate_result = evaluate_symbol_session(
        symbol="AAA",
        direction="LONG_WATCH",
        bars=duplicated,
        trading_day=day,
        expected_minutes=len(duplicated),
    )
    unsupported = evaluate_symbol_session(
        symbol="AAA",
        direction="TRADE_NOW",
        bars=bars,
        trading_day=day,
        expected_minutes=len(bars),
    )

    assert duplicate_result.status == "INCOMPLETE"
    assert "DUPLICATE_REGULAR_MINUTE" in duplicate_result.reason_codes
    assert unsupported.status == "INCOMPLETE"
    assert "UNSUPPORTED_DIRECTION" in unsupported.reason_codes


def test_short_excursions_use_entry_notional_return_convention():
    day = date(2026, 9, 21)
    bars = _regular_bars("AAA", day, first_open=100.0, last_close=95.0)
    result = evaluate_symbol_session(
        symbol="AAA",
        direction="SHORT_WATCH",
        bars=bars,
        trading_day=day,
        expected_minutes=len(bars),
    )

    assert result.status == "COMPLETE"
    assert result.gross_return == pytest.approx(0.05)
    assert result.mfe_return == pytest.approx((100.0 - 94.9) / 100.0)
    assert result.mae_return == pytest.approx((100.0 - 100.1) / 100.0)


def test_ledger_is_deterministic_and_conflicting_same_day_is_rejected(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    result = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )
    ledger = upsert_ledger_entry(load_or_create_ledger(tmp_path / "missing.json"), result)
    same = upsert_ledger_entry(ledger, result)
    assert same == ledger

    changed = json.loads(json.dumps(result))
    changed["artifact_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="conflicting ledger entry"):
        upsert_ledger_entry(ledger, changed)

    report = build_cumulative_report(ledger)
    assert report["ledger_entry_count"] == 1
    assert report["complete_entry_count"] == 1


def test_cumulative_period_is_incomplete_when_any_day_lacks_proxy_data(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    evaluation = build_postclose_evaluation(
        _freeze_brief(day.isoformat()),
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )
    ledger = upsert_ledger_entry(load_or_create_ledger(tmp_path / "missing.json"), evaluation)
    incomplete = dict(ledger["entries"][0])
    incomplete.update({
        "trading_day": "2026-09-22",
        "status": "INCOMPLETE_MARKET_DATA",
        "gross_return": None,
        "net_return_official_cost": None,
        "net_return_stress_cost": None,
        "official_cost_excess_vs_spy_diagnostic": None,
    })
    report = build_cumulative_report({"schema": "premarket-postclose-ledger/v1", "entries": [
        ledger["entries"][0], incomplete,
    ]})

    assert report["status"] == "INCOMPLETE_PROXY_COVERAGE"
    assert report["cumulative_net_return_official_cost"] is None
    assert report["complete_only_cumulative_net_return_official_cost"] is not None


def test_duplicate_trading_days_in_existing_ledger_fail_closed(tmp_path):
    duplicate = {
        "schema": "premarket-postclose-ledger/v1",
        "entries": [
            {"trading_day": "2026-09-21"},
            {"trading_day": "2026-09-21"},
        ],
    }
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(duplicate), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate trading_day"):
        load_or_create_ledger(path)
    with pytest.raises(ValueError, match="duplicate trading_day"):
        build_cumulative_report(duplicate)


def test_cli_writes_immutable_evaluation_and_mutable_ledger(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    brief = tmp_path / "freeze.json"
    brief.write_text(json.dumps(_freeze_brief(day.isoformat())), encoding="utf-8")
    out_json = tmp_path / "eval.json"
    out_md = tmp_path / "eval.md"
    ledger = tmp_path / "ledger.json"
    cumulative = tmp_path / "cumulative.json"
    cumulative_md = tmp_path / "cumulative.md"

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "evaluate_premarket_postclose.py"),
        "--freeze-brief",
        str(brief),
        "--market-db",
        str(db),
        "--evaluation-as-of",
        "2026-09-21T20:00:00Z",
        "--out-json",
        str(out_json),
        "--out-markdown",
        str(out_md),
        "--ledger-json",
        str(ledger),
        "--cumulative-json",
        str(cumulative),
        "--cumulative-markdown",
        str(cumulative_md),
    ]
    completed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=True)
    summary = json.loads(completed.stdout)
    assert summary["status"] == "COMPLETE"
    assert json.loads(out_json.read_text())["freeze"]["input_file_sha256"]
    assert json.loads(ledger.read_text())["entries"][0]["trading_day"] == "2026-09-21"

    out_json.write_text('{"different":true}\n', encoding="utf-8")
    failed = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
    assert failed.returncode != 0
    assert "immutable artifact differs" in failed.stderr


def test_preclose_cli_does_not_create_daily_artifacts_or_poison_ledger(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    brief = tmp_path / "freeze.json"
    brief.write_text(json.dumps(_freeze_brief(day.isoformat())), encoding="utf-8")
    out_json = tmp_path / "eval.json"
    out_md = tmp_path / "eval.md"
    ledger = tmp_path / "ledger.json"
    cumulative = tmp_path / "cumulative.json"
    cumulative_md = tmp_path / "cumulative.md"
    base = [
        sys.executable,
        str(ROOT / "scripts" / "evaluate_premarket_postclose.py"),
        "--freeze-brief", str(brief),
        "--market-db", str(db),
        "--out-json", str(out_json),
        "--out-markdown", str(out_md),
        "--ledger-json", str(ledger),
        "--cumulative-json", str(cumulative),
        "--cumulative-markdown", str(cumulative_md),
    ]

    early = subprocess.run(
        [*base, "--evaluation-as-of", "2026-09-21T19:59:59Z"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert early.returncode == 2
    assert json.loads(early.stdout)["status"] == "INCOMPLETE_PRE_CLOSE"
    assert not any(path.exists() for path in (out_json, out_md, ledger, cumulative, cumulative_md))

    completed = subprocess.run(
        [*base, "--evaluation-as-of", "2026-09-21T20:00:00Z"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout)["status"] == "COMPLETE"
    assert all(path.exists() for path in (out_json, out_md, ledger, cumulative, cumulative_md))


def test_nonfreeze_cli_does_not_create_daily_artifacts_or_poison_ledger(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    invalid_brief = _freeze_brief(day.isoformat())
    invalid_brief["snapshot"]["payload"]["input"]["stage"] = "PRIMARY"
    _refresh_freeze_artifact(invalid_brief)
    invalid_path = tmp_path / "primary.json"
    invalid_path.write_text(json.dumps(invalid_brief), encoding="utf-8")
    valid_path = tmp_path / "freeze.json"
    valid_path.write_text(json.dumps(_freeze_brief(day.isoformat())), encoding="utf-8")
    out_json = tmp_path / "eval.json"
    out_md = tmp_path / "eval.md"
    ledger = tmp_path / "ledger.json"
    cumulative = tmp_path / "cumulative.json"
    cumulative_md = tmp_path / "cumulative.md"
    base = [
        sys.executable,
        str(ROOT / "scripts" / "evaluate_premarket_postclose.py"),
        "--market-db", str(db),
        "--evaluation-as-of", "2026-09-21T20:00:00Z",
        "--out-json", str(out_json),
        "--out-markdown", str(out_md),
        "--ledger-json", str(ledger),
        "--cumulative-json", str(cumulative),
        "--cumulative-markdown", str(cumulative_md),
    ]

    invalid = subprocess.run(
        [*base, "--freeze-brief", str(invalid_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["status"] == "INCOMPLETE_INVALID_FREEZE"
    assert not any(path.exists() for path in (out_json, out_md, ledger, cumulative, cumulative_md))

    completed = subprocess.run(
        [*base, "--freeze-brief", str(valid_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout)["status"] == "COMPLETE"
    assert all(path.exists() for path in (out_json, out_md, ledger, cumulative, cumulative_md))


def test_freeze_artifact_hash_mismatch_is_rejected(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {"AAA": _regular_bars("AAA", day), "BBB": _regular_bars("BBB", day)})
    brief = _freeze_brief(day.isoformat())
    brief["artifact_sha256"] = "a" * 64

    with pytest.raises(ValueError, match="artifact_sha256 mismatch"):
        build_postclose_evaluation(
            brief,
            market_db=db,
            evaluation_as_of="2026-09-21T20:00:00Z",
        )


def test_freeze_artifact_hash_is_required(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {"AAA": _regular_bars("AAA", day), "BBB": _regular_bars("BBB", day)})
    brief = _freeze_brief(day.isoformat())
    del brief["artifact_sha256"]

    with pytest.raises(ValueError, match="requires artifact_sha256"):
        build_postclose_evaluation(
            brief,
            market_db=db,
            evaluation_as_of="2026-09-21T20:00:00Z",
        )


def test_postclose_rerun_after_close_is_byte_identical(tmp_path):
    day = date(2026, 9, 21)
    db = _make_db(tmp_path, {
        "AAA": _regular_bars("AAA", day),
        "BBB": _regular_bars("BBB", day),
        "SPY": _regular_bars("SPY", day),
    })
    brief = _freeze_brief(day.isoformat())

    first = build_postclose_evaluation(
        brief,
        market_db=db,
        evaluation_as_of="2026-09-21T20:00:00Z",
    )
    later = build_postclose_evaluation(
        brief,
        market_db=db,
        evaluation_as_of="2026-09-22T00:00:00Z",
    )

    assert first == later


def test_regular_session_grid_mismatch_is_incomplete():
    day = date(2026, 9, 21)
    bars = _regular_bars("AAA", day)
    shifted = [
        Candle.from_moment(
            bar.symbol,
            bar.interval,
            bar.ts_exchange + timedelta(minutes=1),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.currency,
            bar.adjusted,
            market=bar.market,
        )
        for bar in bars
    ]

    result = evaluate_symbol_session(
        symbol="AAA",
        direction="LONG_WATCH",
        bars=shifted,
        trading_day=day,
        expected_minutes=len(shifted),
    )

    assert result.status == "INCOMPLETE"
    assert "REGULAR_SESSION_FIRST_MINUTE_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    "watchlist",
    [
        [{"symbol": "AAA", "bias": "LONG_WATCH"}, "invalid"],
        [
            {"symbol": "AAA", "bias": "LONG_WATCH"},
            {"symbol": "BBB", "bias": "SHORT_WATCH"},
            {"symbol": "CCC", "bias": "LONG_WATCH"},
            {"symbol": "DDD", "bias": "SHORT_WATCH"},
        ],
    ],
)
def test_malformed_or_oversized_freeze_watchlist_is_rejected(tmp_path, watchlist):
    day = date(2026, 9, 21)
    brief = _freeze_brief(day.isoformat())
    brief["brief"]["watchlist"] = watchlist
    _refresh_freeze_artifact(brief)
    db = _make_db(tmp_path, {})

    with pytest.raises((TypeError, ValueError), match="watchlist"):
        build_postclose_evaluation(
            brief,
            market_db=db,
            evaluation_as_of="2026-09-21T20:00:00Z",
        )
