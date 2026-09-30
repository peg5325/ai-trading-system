from __future__ import annotations

import copy

from src.freeze_paper.engine import Bar, new_account_state, run_session
from src.freeze_paper.reconcile import reconcile_state
from tests.freeze_paper_test_helpers import BINDING, session_bars, trade_gate

DAY = "2026-10-01"
DAY2 = "2026-10-02"


def _session(day=DAY, state=None, overrides=None):
    gate = trade_gate(day, ["AAPL", "MSFT"])
    bars = session_bars(day, {"AAPL": 100.0, "MSFT": 200.0}, overrides)
    return gate, bars, run_session(state or new_account_state(BINDING), gate, bars, BINDING)


def _checks(result):
    return {m["check"] for m in result.mismatches}


def test_clean_session_reconciles_including_restart_rebuild():
    gate, bars, state = _session(overrides={(9, 50): {"AAPL": Bar(100.0, 102.0, 99.9, 101.0)}})
    rebuilt = run_session(new_account_state(BINDING), gate, bars, BINDING)
    result = reconcile_state(state, rebuilt_state=rebuilt)
    assert result.ok, result.to_dict()
    assert len(state["fills"]) == 4


def test_two_sessions_reconcile_across_days():
    _, _, day1 = _session()
    _, _, day2 = _session(DAY2, state=day1)
    assert reconcile_state(day2).ok
    assert len(day2["fills"]) == 8


def test_injected_position_mismatch_is_flagged():
    _, _, state = _session()
    state["order_positions"]["AAPL"] = {"quantity": 3.0, "avg_price": 100.05}
    result = reconcile_state(state)
    assert not result.ok
    assert "POSITIONS_ORDER_MANAGER_VS_PORTFOLIO_ACCOUNT" in _checks(result)


def test_engine_position_record_without_fill_is_flagged():
    _, _, state = _session()
    state["positions"]["MSFT"] = {"quantity": 1.0, "entry_fill_price": 200.1}
    checks = _checks(reconcile_state(state))
    assert "POSITIONS_ENGINE_VS_PORTFOLIO_ACCOUNT" in checks
    assert "NOT_FLAT_AT_SESSION_END" in checks


def test_injected_cash_mismatch_is_flagged():
    _, _, state = _session()
    state["cash"] += 0.01
    result = reconcile_state(state)
    assert [m["source"] for m in result.mismatches if m["check"] == "CASH_IDENTITY"] == ["state_cash"]


def test_order_fill_field_and_count_mismatches_are_flagged():
    _, _, state = _session()
    tampered = copy.deepcopy(state)
    tampered["orders"][0]["fee"] += 0.5
    assert "ORDER_FILL_FIELD" in _checks(reconcile_state(tampered))
    missing_fill = copy.deepcopy(state)
    dropped = missing_fill["fills"].pop()
    checks = _checks(reconcile_state(missing_fill))
    assert "ORDER_FILL_COUNT" in checks
    orphan = copy.deepcopy(state)
    orphan["fills"].append(dict(dropped, client_order_id="pfl1-20261001-ZZZ-BUY"))
    assert "FILL_WITHOUT_ORDER" in _checks(reconcile_state(orphan))


def test_duplicate_client_order_ids_are_flagged():
    _, _, state = _session()
    state["orders"].append(copy.deepcopy(state["orders"][0]))
    assert "DUPLICATE_CLIENT_ORDER_ID" in _checks(reconcile_state(state))


def test_restart_rebuild_difference_is_flagged():
    gate, _, state = _session()
    other =run_session(new_account_state(BINDING), gate,
                        session_bars(DAY, {"AAPL": 100.0, "MSFT": 201.0}), BINDING)
    assert "RESTART_REBUILD_MISMATCH" in _checks(reconcile_state(state, rebuilt_state=other))


def test_unresolved_exit_session_is_not_flagged_for_being_open():
    missing = {(h, m): {"AAPL": None} for h in range(10, 16) for m in range(60) if (h, m) >= (10, 35)}
    _, _, state = _session(overrides=missing)
    assert state["session"]["status"] == "UNRESOLVED_EXIT"
    assert reconcile_state(state).ok


def test_fill_fee_must_equal_commission_plus_sec_fee():
    _, _, state = _session()
    state["fills"][-1]["sec_fee"] += 0.01
    assert "FILL_FEE_PARTS" in _checks(reconcile_state(state))
