from __future__ import annotations

import json
import shutil
from datetime import date

import pytest

from scripts import evaluate_freeze_paper_window
from src.freeze_paper.binding import load_binding
from src.freeze_paper.evaluator import (
    ELIGIBLE_FOR_LIVE_REVIEW,
    EXTEND_OBSERVATION,
    HALT_AND_REVIEW,
    INCONCLUSIVE_BENCHMARK,
    NO_PROMOTION,
    DividendFileError,
    evaluate_window,
    evaluation_paths,
    load_spy_dividends,
    spy_total_return,
    write_evaluation,
)
from src.freeze_paper.receipt import CONTRACT_JSON_RELATIVE, RECEIPT_RELATIVE, ROOT
from src.freeze_paper.runner import paper_paths, run_paper_session
from src.freeze_paper.spy import capture_defect, spy_close_evidence
from src.premarket.contract import sha256_payload
from src.premarket.report import json_bytes
from tests.freeze_paper_runner_helpers import (
    FakeBackfill,
    after_close,
    fake_git,
    make_root,
    write_bars,
    write_day_freeze,
    write_receipt,
)
from tests.freeze_paper_test_helpers import watch_item

TARGET_BARS = {(9, 40): {"AAPL": (100.0, 103.0, 100.0, 102.0),
                         "MSFT": (200.0, 206.0, 200.0, 204.0)}}


def _sessions(root):
    return json.loads((root / RECEIPT_RELATIVE).read_text())["scheduled_sessions"]


def _run_day(root, day):
    result = run_paper_session(trading_day=date.fromisoformat(day), root=root,
                               now=after_close(day), runner=FakeBackfill(),
                               git_runner=fake_git())
    assert result["status"] == "COMPLETE", result


def _build(root, days, overrides=None, freeze_kwargs=None):
    for day in days:
        write_day_freeze(root, day, **(freeze_kwargs or {}).get(day, {}))
        write_bars(root, day, {"AAPL": 100.0, "MSFT": 200.0, "SQQQ": 20.0},
                   (overrides or {}).get(day, TARGET_BARS))
        _run_day(root, day)


@pytest.fixture(scope="module")
def good_window(tmp_path_factory):
    root = make_root(tmp_path_factory.mktemp("good"))
    receipt = write_receipt(root)
    _build(root, receipt["scheduled_sessions"])
    return root


def _copy(root, tmp_path):
    target = tmp_path / "copy"
    shutil.copytree(root, target)
    return target


def _cap(closes, captured=None, updated=None):
    """Close evidence as the store records it: first written that evening (21:00Z)."""
    out = {}
    for day, value in closes.items():
        stamp = captured or f"{day}T21:00:00+00:00"
        out[day] = {"close": value, "captured_at": stamp, "updated_at": updated or stamp}
    return out


def _evaluate(root, closes=None, dividends=(), through=None, receipt=None):
    receipt = receipt or json.loads((root / RECEIPT_RELATIVE).read_text())
    binding = load_binding(root / CONTRACT_JSON_RELATIVE)
    if closes is None:
        closes = {d: 500.0 for d in [receipt["window_start_close_date"],
                                     *receipt["scheduled_sessions"]]}
    return evaluate_window(root=root, receipt=receipt, binding=binding, spy_closes=_cap(closes),
                           dividends=list(dividends),
                           through=date.fromisoformat(through) if through else None)


def test_profitable_complete_window_is_eligible_for_live_review_only(good_window):
    result = _evaluate(good_window)
    assert result["label"] == "FINAL"
    assert result["outcome"] == ELIGIBLE_FOR_LIVE_REVIEW
    assert result["session_counts"]["traded"] == 10 and result["missing_evidence"] == []
    assert "Not a live-trading approval; no automatic promotion" in result["notice"]
    assert result["benchmark"]["official_sp500_total_return"]["status"] == INCONCLUSIVE_BENCHMARK
    assert result["legs"]["INVERSE"]["trades"] == 0 and result["legs"]["LONG"]["trades"] == 20


def test_official_stress_and_opex_views_match_hand_computation(good_window):
    receipt = json.loads((good_window / RECEIPT_RELATIVE).read_text())
    receipt["operating_costs"] = {"items": [{"name": "data api", "usd": 3.0}], "total_usd": 3.0,
                                  "allocation": "window sessions", "reason": "dedicated API"}
    m = _evaluate(good_window, receipt=receipt, through="2026-10-01")["metrics"]
    # first session: AAPL 7 @100.05 -> target 100.05*1.015 less 5bp; MSFT 3 @200.1 -> same
    notional = (7 * 100.05 + 7 * 100.05 * 1.015 * 0.9995
                + 3 * 200.1 + 3 * 200.1 * 1.015 * 0.9995)
    assert m["filled_notional"] == pytest.approx(notional)
    fees = (7 * 100.05 * 0.001 + 7 * 100.05 * 1.015 * 0.9995 * 0.001 + 0.02
            + 3 * 200.1 * 0.001 + 3 * 200.1 * 1.015 * 0.9995 * 0.001 + 0.02)   # SEC 0.02 each
    gross = 7 * (100.05 * 1.015 * 0.9995 - 100.05) + 3 * (200.1 * 1.015 * 0.9995 - 200.1)
    assert m["net_pnl"]["ending_equity"] == pytest.approx(2000 + gross - fees)
    assert m["official_drag"] == pytest.approx(notional * 0.0002)
    assert m["stress_drag"] == pytest.approx(notional * 0.00105)
    end = m["net_pnl"]["ending_equity"]
    assert m["official_cost_return"] == pytest.approx((end - notional * 0.0002) / 2000 - 1)
    assert m["official_opex_return"] == pytest.approx((end - notional * 0.0002 - 3.0) / 2000 - 1)
    assert m["stress_pnl"] == pytest.approx(end - notional * 0.00105 - 2000)
    assert m["net_account_return"] == pytest.approx(end / 2000 - 1)


def test_spy_underperformance_is_no_promotion(good_window):
    receipt = json.loads((good_window / RECEIPT_RELATIVE).read_text())
    closes = {d: 500.0 for d in [receipt["window_start_close_date"],
                                 *receipt["scheduled_sessions"]]}
    closes[receipt["scheduled_sessions"][-1]] = 600.0
    result = _evaluate(good_window, closes=closes)
    assert result["outcome"] == NO_PROMOTION
    assert result["economic_failures"] == ["EXCESS_VS_SPY_TR_NOT_POSITIVE"]


def test_spy_total_return_reinvests_a_verified_dividend_at_the_ex_close():
    dividends = [{"ex_date": "2026-10-09", "amount_usd": 1.5, "verified": True}]
    result = spy_total_return(start_day="2026-09-30", end_day="2026-10-14",
                              closes=_cap({"2026-09-30": 500.0, "2026-10-09": 505.0,
                                           "2026-10-14": 510.0}),
                              dividends=dividends, required_ex_dates=["2026-10-09"])
    assert result["status"] == "OK"
    assert result["total_return"] == pytest.approx((1 + 1.5 / 505.0) * 510.0 / 500.0 - 1)


def test_unverified_or_unlisted_dividend_makes_the_benchmark_inconclusive(good_window):
    receipt = json.loads((good_window / RECEIPT_RELATIVE).read_text())
    receipt["spy_ex_dividend_dates_in_window"] = ["2026-10-09"]
    result = _evaluate(good_window, receipt=receipt)
    spy = result["benchmark"]["spy_total_return"]
    assert spy["status"] == INCONCLUSIVE_BENCHMARK
    assert spy["reasons"] == ["UNVERIFIED_DIVIDEND:2026-10-09"]
    assert result["outcome"] == EXTEND_OBSERVATION
    assert INCONCLUSIVE_BENCHMARK in result["missing_evidence"]
    unverified = [{"ex_date": "2026-10-09", "amount_usd": None, "verified": False}]
    assert _evaluate(good_window, dividends=unverified)["benchmark"]["spy_total_return"][
        "status"] == INCONCLUSIVE_BENCHMARK


def test_missing_spy_close_is_inconclusive(good_window):
    receipt = json.loads((good_window / RECEIPT_RELATIVE).read_text())
    closes = {d: 500.0 for d in receipt["scheduled_sessions"]}      # no window-start close
    spy = _evaluate(good_window, closes=closes)["benchmark"]["spy_total_return"]
    assert spy["reasons"] == ["MISSING_SPY_CLOSE:2026-09-30"]


def test_interim_report_is_never_eligible(good_window):
    result = _evaluate(good_window, through="2026-10-07")
    assert result["label"] == "INTERIM"
    assert result["outcome"] == EXTEND_OBSERVATION
    assert result["missing_evidence"] == ["WINDOW_NOT_COMPLETE"]
    assert result["session_counts"]["considered"] == 5


def test_missing_incomplete_and_invalid_sessions_extend_observation(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    sessions = _sessions(root)
    paper_paths(root, date.fromisoformat(sessions[9]))["session"].unlink()
    result = _evaluate(root)
    assert result["outcome"] == EXTEND_OBSERVATION
    assert "SESSION_MISSING:2026-10-14" in result["missing_evidence"]
    assert result["session_counts"]["missing"] == 1 and result["session_counts"]["recorded"] == 9

    op = paper_paths(root, date.fromisoformat(sessions[9]))["operation"]
    op.write_text(json.dumps({"status": "INCOMPLETE"}))
    assert "SESSION_INCOMPLETE:2026-10-14" in _evaluate(root)["missing_evidence"]

    path = paper_paths(root, date.fromisoformat(sessions[4]))["session"]
    artifact = json.loads(path.read_text())
    artifact["status"] = "NO_TRADE"                                   # tampered, unsigned
    path.write_text(json.dumps(artifact))
    tampered = _evaluate(root)
    assert tampered["evaluable"] is False and tampered["metrics"] is None
    assert tampered["sessions"][4] == {"trading_day": sessions[4], "status": "INVALID",
                                       "reason": "SESSION_SIGNATURE_INVALID"}
    assert tampered["sessions"][5]["reason"] == "SESSION_CHAIN_BROKEN"
    assert "NOT_EVALUABLE" in tampered["missing_evidence"]
    assert tampered["outcome"] == EXTEND_OBSERVATION


def test_zero_completed_trades_extend_observation(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    _build(root, receipt["scheduled_sessions"],
           freeze_kwargs={d: {"regime": "UNCERTAIN"} for d in receipt["scheduled_sessions"]})
    result = _evaluate(root)
    assert result["session_counts"]["no_trade"] == 10
    assert result["missing_evidence"] == ["NO_COMPLETED_TRADE"]
    assert result["outcome"] == EXTEND_OBSERVATION


def test_risk_halt_takes_precedence_over_everything(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    days = receipt["scheduled_sessions"]
    crash = {(9, 40): {"AAPL": (90.0, 90.0, 89.0, 89.5)}}
    _build(root, days[:3], overrides={days[0]: crash})
    receipt_doc = dict(receipt, spy_ex_dividend_dates_in_window=["2026-10-09"])
    result = _evaluate(root, receipt=receipt_doc, closes={})
    assert result["outcome"] == HALT_AND_REVIEW
    assert "RISK_HALT:DAILY_LOSS_LIMIT" in result["halt_reasons"]
    assert result["session_counts"]["halted"] == 3        # day 1 halts, days 2-3 stay HALTED


def test_legs_are_separated_including_an_inverse_trade(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    days = receipt["scheduled_sessions"][:2]
    inverse = {"regime": "RISK_OFF", "watchlist": [watch_item("TQQQ", "SHORT_WATCH")]}
    _build(root, days, overrides={days[1]: {(9, 40): {"SQQQ": (20.0, 20.5, 20.0, 20.4)}}},
           freeze_kwargs={days[1]: inverse})
    legs = _evaluate(root, through=days[1])["legs"]
    assert legs["LONG"]["trades"] == 2 and legs["INVERSE"]["trades"] == 1
    assert legs["INVERSE"]["realized_net_pnl"] > 0
    session = json.loads(paper_paths(root, date.fromisoformat(days[1]))["session"].read_text())
    sqqq = [f for f in session["fills"] if f["symbol"] == "SQQQ"]
    assert {f["leg"] for f in sqqq} == {"INVERSE"}
    expected = ((sqqq[1]["price"] - sqqq[0]["price"]) * sqqq[0]["quantity"]
                - sqqq[0]["fee"] - sqqq[1]["fee"])
    assert legs["INVERSE"]["realized_net_pnl"] == pytest.approx(expected)


def test_max_drawdown_includes_intraday_marks(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    day = receipt["scheduled_sessions"][0]
    _build(root, [day], overrides={day: {(9, 50): {"AAPL": (100.0, 100.0, 99.3, 99.3)}}})
    m = _evaluate(root, through=day)["metrics"]
    # 09:50 mark: 2000 - entry costs (7*100.05*0.001 + 7*0.05 + 3*200.1*0.001 + 3*0.1) - 7*0.7
    trough = 2000 - (7 * 100.05 * 0.001 + 7 * 0.05 + 3 * 200.1 * 0.001 + 3 * 0.1) - 7 * 0.7
    assert m["max_drawdown"] == pytest.approx((2000 - trough) / 2000)
    assert m["max_drawdown"] > -m["net_account_return"]


def test_rerun_is_byte_identical_and_final_is_write_once(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    first = _evaluate(root)
    assert json_bytes(first) == json_bytes(_evaluate(root))
    json_path, md_path = write_evaluation(root, first)
    assert json_path.name == "premarket-freeze-paper-evaluation.20261001_20261014.json"
    assert (json_path, md_path) == evaluation_paths(root, first)
    write_evaluation(root, first)                                   # identical: no-op
    receipt = json.loads((root / RECEIPT_RELATIVE).read_text())
    closes = {d: 500.0 for d in [receipt["window_start_close_date"], *receipt["scheduled_sessions"]]}
    closes["2026-10-14"] = 400.0
    with pytest.raises(FileExistsError):
        write_evaluation(root, _evaluate(root, closes=closes))
    assert "run_at" not in json_path.read_text()
    assert "Not a live-trading approval" in md_path.read_text()


def test_interim_files_are_regenerable(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    interim = _evaluate(root, through="2026-10-02")
    json_path, _ = write_evaluation(root, interim)
    assert json_path.name == "premarket-freeze-paper-evaluation.interim.20261001_20261002.json"
    receipt = json.loads((root / RECEIPT_RELATIVE).read_text())
    closes = {d: 450.0 for d in [receipt["window_start_close_date"], *receipt["scheduled_sessions"]]}
    write_evaluation(root, _evaluate(root, closes=closes, through="2026-10-02"))


def test_dividend_file_template_and_loader(tmp_path):
    assert load_spy_dividends(ROOT / "docs/research/SPY_DIVIDENDS.json") == []
    path = tmp_path / "div.json"
    path.write_text(json.dumps({"schema": "spy-dividends/v1", "dividends": [
        {"ex_date": "2026-10-09", "amount_usd": 1.5, "source_url": "https://example.test/spy",
         "verified_on": "2026-10-10"},
        {"ex_date": "2026-12-18", "amount_usd": None, "source_url": "", "verified_on": None}]}))
    loaded = load_spy_dividends(path)
    assert [(d["ex_date"], d["verified"]) for d in loaded] == [
        ("2026-10-09", True), ("2026-12-18", False)]
    path.write_text(json.dumps({"schema": "other", "dividends": []}))
    with pytest.raises(DividendFileError):
        load_spy_dividends(path)


def test_cli_reads_spy_closes_from_the_market_db(good_window, tmp_path, capsys):
    root = _copy(good_window, tmp_path)
    receipt = json.loads((root / RECEIPT_RELATIVE).read_text())
    for day in [receipt["window_start_close_date"], *receipt["scheduled_sessions"]]:
        write_bars(root, day, {"SPY": 500.0}, end=(16, 0),
                   created_at=f"{day}T20:30:00+00:00")
    (root / "docs/research/SPY_DIVIDENDS.json").write_text(json.dumps(
        {"schema": "spy-dividends/v1", "dividends": []}))
    assert evaluate_freeze_paper_window.main(["--root", str(root)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["label"], printed["outcome"]) == ("FINAL", ELIGIBLE_FOR_LIVE_REVIEW)
    assert (root / "reports" / printed["path"]).is_file()


def _resign(path, mutate):
    artifact = json.loads(path.read_text())
    mutate(artifact)
    artifact.pop("artifact_sha256")
    artifact["artifact_sha256"] = sha256_payload(artifact)
    path.write_text(json.dumps(artifact))


def test_final_requires_all_ten_verified_sessions(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    last = _sessions(root)[-1]
    session_file = paper_paths(root, date.fromisoformat(last))["session"]
    saved = session_file.read_bytes()
    session_file.unlink()
    partial = _evaluate(root, through=last)
    assert partial["label"] == "INTERIM"
    json_path, _ = write_evaluation(root, partial)
    assert json_path.name.startswith("premarket-freeze-paper-evaluation.interim.")
    assert not (root / "reports" / "premarket-freeze-paper-evaluation.20261001_20261014.json"
                ).exists()
    # account (9 sessions) is never compared with SPY through the 10th close
    assert partial["metrics"]["excess_vs_spy_tr_pct_points"] is None
    assert partial["metrics"]["benchmark_comparison"] == "WINDOW_INCOMPLETE"
    assert partial["benchmark"]["spy_total_return"]["status"] == "OK"
    session_file.write_bytes(saved)
    final = _evaluate(root, through=last)
    assert final["label"] == "FINAL" and final["metrics"]["benchmark_comparison"] == "OK"
    assert write_evaluation(root, final)[0].name == (
        "premarket-freeze-paper-evaluation.20261001_20261014.json")


def test_eligibility_carried_by_the_inverse_leg_is_flagged(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    days = receipt["scheduled_sessions"]
    stop = {(9, 40): {"AAPL": (100.0, 100.0, 99.0, 99.5), "MSFT": (200.0, 200.0, 198.0, 199.0)}}
    target = {(9, 40): {"SQQQ": (20.0, 20.5, 20.0, 20.4)}}
    inverse = {"regime": "RISK_OFF", "watchlist": [watch_item("TQQQ", "SHORT_WATCH")]}
    _build(root, days, overrides={d: (stop if i == 0 else target) for i, d in enumerate(days)},
           freeze_kwargs={d: inverse for d in days[1:]})
    result = _evaluate(root)
    assert result["outcome"] == ELIGIBLE_FOR_LIVE_REVIEW
    assert result["legs"]["LONG"]["official_net_pnl"] < 0 < result["legs"]["INVERSE"][
        "official_net_pnl"]
    assert result["flags"] == ["ELIGIBILITY_DEPENDS_ON_INVERSE_LEG"]


def test_old_schema_or_missing_equity_curve_is_not_evaluable(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    first = _sessions(root)[0]
    path = paper_paths(root, date.fromisoformat(first))["session"]
    original = path.read_text()
    _resign(path, lambda a: a["end_state"]["session"].pop("equity_curve"))
    result = _evaluate(root, through=first)
    assert result["sessions"][0]["reason"] == "SESSION_EQUITY_CURVE_MISSING"
    assert result["metrics"] is None and "NOT_EVALUABLE" in result["missing_evidence"]
    path.write_text(original)
    _resign(path, lambda a: a.update(schema="premarket-freeze-paper/session/v1"))
    assert _evaluate(root, through=first)["sessions"][0]["reason"] == "SESSION_SCHEMA_MISMATCH"


def test_leg_pnl_must_sum_to_the_account_fifo_pnl(good_window, tmp_path):
    root = _copy(good_window, tmp_path)
    first = _sessions(root)[0]
    path = paper_paths(root, date.fromisoformat(first))["session"]

    def relabel(artifact):
        for fill in artifact["end_state"]["fills"]:
            if fill["symbol"] == "MSFT":
                fill["leg"] = "UNKNOWN"

    _resign(path, relabel)
    result = _evaluate(root, through=first)
    assert result["outcome"] == HALT_AND_REVIEW
    assert result["halt_reasons"] == ["LEG_PNL_MISMATCH"]


def test_profit_factor_without_losing_trades_is_null(good_window):
    result = _evaluate(good_window)
    assert result["diagnostics"]["losses"] == 0
    assert result["diagnostics"]["profit_factor"] is None
    json_bytes(result)                                   # allow_nan=False: JSON-safe


def test_expected_quarterly_ex_date_must_be_listed():
    closes = _cap({"2026-09-10": 500.0, "2026-09-18": 501.0, "2026-09-24": 502.0})
    unlisted = spy_total_return(start_day="2026-09-10", end_day="2026-09-24", closes=closes,
                                dividends=[], required_ex_dates=[])
    assert unlisted["reasons"] == ["UNLISTED_EXPECTED_EX_DATE:2026-09-18"]
    listed = spy_total_return(
        start_day="2026-09-10", end_day="2026-09-24", closes=closes,
        dividends=[{"ex_date": "2026-09-18", "amount_usd": 1.8, "verified": True}],
        required_ex_dates=["2026-09-18"])
    assert listed["status"] == "OK"
    assert listed["total_return"] == pytest.approx((1 + 1.8 / 501.0) * 502.0 / 500.0 - 1)
    assert "not an official unadjusted daily close" in listed["close_source"]
    october = spy_total_return(start_day="2026-09-30", end_day="2026-10-14",
                               closes=_cap({"2026-09-30": 1.0, "2026-10-14": 1.0}),
                               dividends=[], required_ex_dates=[])
    assert october["status"] == "OK"                     # no quarterly window intersects


def test_prorated_operating_costs_are_diagnostics_only(good_window):
    receipt = json.loads((good_window / RECEIPT_RELATIVE).read_text())
    receipt["operating_costs"] = {"items": [{"name": "api", "usd": 3.0}], "total_usd": 3.0,
                                  "allocation": "window sessions", "reason": "dedicated API"}
    result = _evaluate(good_window, receipt=receipt, through="2026-10-07")
    m, d = result["metrics"], result["diagnostics"]
    assert m["operating_costs_usd"] == 3.0
    assert d["operating_costs_prorated_usd"] == pytest.approx(1.5)
    end, drag = m["net_pnl"]["ending_equity"], m["official_drag"]
    assert m["official_opex_return"] == pytest.approx((end - drag - 3.0) / 2000 - 1)
    assert d["official_opex_return_prorated"] == pytest.approx((end - drag - 1.5) / 2000 - 1)


def test_spy_close_capture_rule():
    same_evening = _cap({"2026-10-01": 500.0}, captured="2026-10-01T20:30:00+00:00")
    assert capture_defect("2026-10-01", same_evening["2026-10-01"]) is None
    before_midnight = _cap({"2026-10-01": 500.0}, captured="2026-10-02T03:59:00+00:00")
    assert capture_defect("2026-10-01", before_midnight["2026-10-01"]) is None   # 23:59 EDT
    later = _cap({"2026-10-01": 500.0}, captured="2026-10-02T04:01:00+00:00")   # 00:01 EDT
    assert capture_defect("2026-10-01", later["2026-10-01"]) == (
        "SPY_CLOSE_NOT_CAPTURED_SAME_DAY:2026-10-01")
    too_early = _cap({"2026-10-01": 500.0}, captured="2026-10-01T19:00:00+00:00")
    assert capture_defect("2026-10-01", too_early["2026-10-01"]) == (
        "SPY_CLOSE_NOT_CAPTURED_SAME_DAY:2026-10-01")
    overwritten = _cap({"2026-10-01": 500.0}, captured="2026-10-01T20:30:00+00:00",
                       updated="2026-10-20T20:30:00+00:00")
    assert capture_defect("2026-10-01", overwritten["2026-10-01"]) == (
        "SPY_CLOSE_OVERWRITTEN:2026-10-01")
    # early close (13:00 EST): window 18:00Z .. 05:00Z next day
    early = "2026-11-27"
    assert capture_defect(early, _cap({early: 1.0}, captured="2026-11-27T18:30:00+00:00")[
        early]) is None
    assert capture_defect(early, _cap({early: 1.0}, captured="2026-11-27T17:59:00+00:00")[
        early]) == f"SPY_CLOSE_NOT_CAPTURED_SAME_DAY:{early}"
    assert capture_defect(early, _cap({early: 1.0}, captured="2026-11-28T04:59:00+00:00")[
        early]) is None
    assert capture_defect(early, _cap({early: 1.0}, captured="2026-11-28T05:01:00+00:00")[
        early]) == f"SPY_CLOSE_NOT_CAPTURED_SAME_DAY:{early}"


def test_spy_price_discontinuity_is_inconclusive():
    closes = _cap({"2026-09-30": 500.0, "2026-10-01": 500.0, "2026-10-02": 250.0,
                   "2026-10-14": 250.0})
    spy = spy_total_return(start_day="2026-09-30", end_day="2026-10-14", closes=closes,
                           dividends=[], required_ex_dates=[])
    assert spy["status"] == INCONCLUSIVE_BENCHMARK
    assert spy["reasons"] == ["SPY_PRICE_DISCONTINUITY:2026-10-02"]
    fine = _cap({"2026-09-30": 500.0, "2026-10-01": 520.0, "2026-10-14": 530.0})
    assert spy_total_return(start_day="2026-09-30", end_day="2026-10-14", closes=fine,
                            dividends=[], required_ex_dates=[])["status"] == "OK"


def test_late_captured_spy_close_makes_the_benchmark_inconclusive(good_window, tmp_path, capsys):
    root = _copy(good_window, tmp_path)
    receipt = json.loads((root / RECEIPT_RELATIVE).read_text())
    for day in [receipt["window_start_close_date"], *receipt["scheduled_sessions"]]:
        captured = ("2026-10-20T20:30:00+00:00" if day == "2026-10-14"
                    else f"{day}T20:30:00+00:00")
        write_bars(root, day, {"SPY": 500.0}, end=(16, 0), created_at=captured)
    (root / "docs/research/SPY_DIVIDENDS.json").write_text(json.dumps(
        {"schema": "spy-dividends/v1", "dividends": []}))
    assert evaluate_freeze_paper_window.main(["--root", str(root), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == EXTEND_OBSERVATION
    closes = {d: spy_close_evidence(root / "data/market_history.sqlite", date.fromisoformat(d))
              for d in ["2026-09-30", "2026-10-14"]}
    spy = spy_total_return(start_day="2026-09-30", end_day="2026-10-14", closes=closes,
                           dividends=[], required_ex_dates=[])
    assert spy["reasons"] == ["SPY_CLOSE_NOT_CAPTURED_SAME_DAY:2026-10-14"]
    assert closes["2026-09-30"]["captured_at"] == "2026-09-30T20:30:00+00:00"


def test_cli_reports_receipt_errors_without_a_traceback(tmp_path, capsys):
    root = make_root(tmp_path)
    write_receipt(root)
    (root / "data" / "market_history.sqlite").write_bytes(b"not a sqlite database" * 100)
    assert evaluate_freeze_paper_window.main(["--root", str(root)]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "status": "NOT_ACTIVATED",
        "reason_codes": ["RECEIPT_INVALID:WINDOW_START_SPY_CLOSE_UNREADABLE"]}
    assert "Traceback" not in out.err
