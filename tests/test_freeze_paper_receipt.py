from __future__ import annotations

import json

import pytest

from src.freeze_paper.receipt import (
    CONTRACT_MD_RELATIVE,
    RECEIPT_RELATIVE,
    ReceiptError,
    load_receipt,
    validate_receipt,
)
from src.premarket.contract import sha256_payload
from tests.freeze_paper_runner_helpers import (
    make_root,
    receipt_payload,
    write_bars,
    write_receipt,
)


def test_valid_receipt_round_trips(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    assert load_receipt(root) == receipt
    assert receipt["scheduled_sessions"][0] == "2026-10-01"
    assert receipt["scheduled_sessions"][-1] == "2026-10-14"
    assert receipt["window_start_close_date"] == "2026-09-30"


def test_missing_receipt_is_rejected(tmp_path):
    with pytest.raises(ReceiptError, match="MISSING_FIRST_SESSION_RECEIPT"):
        load_receipt(make_root(tmp_path))


def test_unreadable_receipt_is_rejected(tmp_path):
    root = make_root(tmp_path)
    (root / RECEIPT_RELATIVE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ReceiptError, match="UNREADABLE"):
        load_receipt(root)


@pytest.mark.parametrize(("overrides", "code"), [
    ({"schema": "other/v1"}, "RECEIPT_SCHEMA_MISMATCH"),
    ({"contract_json_sha256": "0" * 64}, "CONTRACT_JSON_SHA256"),
    ({"contract_md_sha256": "0" * 64}, "CONTRACT_MD_SHA256"),
    ({"contract_version": "1.1"}, "CONTRACT_VERSION"),
    ({"code_commit": "HEAD"}, "CODE_COMMIT"),
    ({"activation_config_sha256": "0" * 64}, "ACTIVATION_CONFIG_SHA256"),
    ({"first_session": "2026-10-03"}, "FIRST_SESSION_NOT_TRADING_DAY"),
    ({"scheduled_sessions": ["2026-10-01"]}, "SCHEDULED_SESSIONS"),
    ({"window_start_close_date": "2026-09-29"}, "WINDOW_START_CLOSE_DATE"),
    ({"spy_price_source": ""}, "SPY_PRICE_SOURCE"),
    ({"spy_ex_dividend_dates_in_window": ["2026-09-01"]}, "SPY_EX_DIVIDEND_DATES"),
    ({"spy_ex_dividend_dates_in_window": None}, "SPY_EX_DIVIDEND_DATES"),
    ({"spy_ex_dividend_dates_in_window": ["2026-10-09", "2026-10-09"]}, "SPY_EX_DIVIDEND_DATES"),
    ({"spy_ex_dividend_source": ""}, "SPY_EX_DIVIDEND_SOURCE"),
    ({"operating_costs": {"items": [{"name": "api", "usd": 5.0}], "total_usd": 0.0,
                          "allocation": "x", "reason": "y"}}, "OPERATING_COSTS_TOTAL"),
    ({"operating_costs": {"items": [], "total_usd": 0.0, "allocation": "x", "reason": ""}},
     "OPERATING_COSTS_METHOD"),
    ({"account_event_fee": {"note": "x"}}, "ACCOUNT_EVENT_FEE"),
    ({"allowed_symbols_check": {"index_3x_etfs_listed": ["TQQQ"],
                                "single_stock_leveraged_etfs": [], "passed": True}},
     "ALLOWED_SYMBOLS_CHECK"),
    ({"verified_paths": {"restart": ["tests/test_fake_verified_paths.py::test_restart"]}},
     "VERIFIED_PATHS"),
    ({"verified_paths": {k: ["tests/test_fake_verified_paths.py::test_missing"]
                         for k in ("restart", "eod", "reconciliation", "duplicate")}},
     "VERIFIED_PATHS_RESTART"),
    ({"test_run": {"command": "pytest", "passed": False}}, "TEST_RUN"),
])
def test_inconsistent_receipts_are_rejected(tmp_path, overrides, code):
    root = make_root(tmp_path)
    with pytest.raises(ReceiptError, match=code):
        validate_receipt(receipt_payload(root, **overrides), root=root)


def test_missing_field_and_tampered_signature_are_rejected(tmp_path):
    root = make_root(tmp_path)
    receipt = receipt_payload(root)
    missing = {k: v for k, v in receipt.items() if k != "spy_dividend_source"}
    with pytest.raises(ReceiptError, match="RECEIPT_MISSING_FIELDS:spy_dividend_source"):
        validate_receipt(missing, root=root)
    no_list = {k: v for k, v in receipt.items() if k != "spy_ex_dividend_dates_in_window"}
    with pytest.raises(ReceiptError, match="spy_ex_dividend_dates_in_window"):
        validate_receipt(no_list, root=root)
    tampered = dict(receipt, first_session="2026-10-02")
    with pytest.raises(ReceiptError, match="RECEIPT_SHA256_MISMATCH"):
        validate_receipt(tampered, root=root)


def test_contract_markdown_change_after_receipt_invalidates_it(tmp_path):
    root = make_root(tmp_path)
    write_receipt(root)
    path = root / CONTRACT_MD_RELATIVE
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ReceiptError, match="CONTRACT_MD_SHA256"):
        load_receipt(root)


def test_config_without_activation_templates_is_rejected(tmp_path):
    root = make_root(tmp_path, activate_config=False)
    with pytest.raises(ReceiptError, match="ACTIVATION_TEMPLATES"):
        validate_receipt(receipt_payload(root), root=root)


def test_config_listing_a_single_stock_leveraged_etf_is_rejected(tmp_path):
    root = make_root(tmp_path)
    universe = root / "config" / "universe_p0_us30.yaml"
    universe.write_text(universe.read_text(encoding="utf-8").replace(
        '  - {symbol: TQQQ,', '  - {symbol: NVDL,  class: leveraged_etf, note: "single"}\n'
        '  - {symbol: TQQQ,'), encoding="utf-8")
    with pytest.raises(ReceiptError, match="ALLOWED_SYMBOLS_SINGLE_STOCK_LEVERAGED"):
        validate_receipt(receipt_payload(root), root=root)


@pytest.mark.parametrize("first", ["2026-12-24", "2021-01-04"])
def test_sessions_or_window_outside_the_tracked_calendar_are_rejected(tmp_path, first):
    # 2026-12-24 needs sessions in 2027; 2021-01-04 needs a window start in 2020 (calendar starts 2021-01-01).
    root = make_root(tmp_path)
    receipt = receipt_payload(root, scheduled_sessions=[], window_start_close_date="2000-01-01")
    receipt["first_session"] = first
    receipt.pop("artifact_sha256")
    receipt["artifact_sha256"] = sha256_payload(receipt)
    with pytest.raises(ReceiptError, match="CALENDAR_RANGE"):
        validate_receipt(receipt, root=root)


def test_receipt_json_is_signed_with_the_premarket_payload_hash(tmp_path):
    root = make_root(tmp_path)
    receipt = write_receipt(root)
    stored = json.loads((root / RECEIPT_RELATIVE).read_text(encoding="utf-8"))
    unsigned = {k: v for k, v in stored.items() if k != "artifact_sha256"}
    assert stored["artifact_sha256"] == sha256_payload(unsigned) == receipt["artifact_sha256"]


def test_window_start_spy_close_evidence_must_match_the_store(tmp_path):
    root = make_root(tmp_path)
    receipt = receipt_payload(root)
    db = root / "data" / "market_history.sqlite"
    before = db.read_bytes()
    validate_receipt(receipt, root=root)
    assert db.read_bytes() == before                                   # read-only
    wrong = dict(receipt["window_start_spy_close_evidence"], close=499.0)
    with pytest.raises(ReceiptError, match="WINDOW_START_SPY_CLOSE"):
        validate_receipt(receipt_payload(root, window_start_spy_close_evidence=wrong), root=root)


def test_window_start_spy_close_captured_late_is_rejected(tmp_path):
    root = make_root(tmp_path)
    write_bars(root, "2026-09-30", {"SPY": 500.0}, created_at="2026-10-01T14:00:00+00:00")
    with pytest.raises(ReceiptError, match="WINDOW_START_SPY_CLOSE"):
        validate_receipt(receipt_payload(root), root=root)


def test_window_start_spy_close_missing_db_is_rejected(tmp_path):
    root = make_root(tmp_path)
    receipt = receipt_payload(root)
    (root / "data" / "market_history.sqlite").unlink()
    with pytest.raises(ReceiptError, match="WINDOW_START_SPY_CLOSE"):
        validate_receipt(receipt, root=root)


def test_unreadable_market_db_is_a_receipt_error(tmp_path):
    root = make_root(tmp_path)
    receipt = receipt_payload(root)
    (root / "data" / "market_history.sqlite").write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ReceiptError, match="WINDOW_START_SPY_CLOSE_UNREADABLE"):
        validate_receipt(receipt, root=root)
