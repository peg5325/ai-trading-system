"""Exit retries, partial fills, invalid bars and session guards of the Paper engine."""
from __future__ import annotations

import dataclasses
import json
import math

import pytest

import src.freeze_paper.engine as engine_module
from src.freeze_paper.engine import (
    INVALID_BAR,
    Bar,
    finish_session,
    new_account_state,
    run_session,
    start_session,
    state_bytes,
    step_bar,
)
from src.freeze_paper.gates import GateCandidate, GateResult
from src.freeze_paper.reconcile import reconcile_state
from src.order.paper_broker import PaperBrokerProvider
from tests.freeze_paper_test_helpers import (
    BINDING,
    minute,
    schedule,
    session_bars,
    trade_gate,
)

DAY = "2026-10-01"
DAY2 = "2026-10-02"


@pytest.fixture
def scripted_broker(monkeypatch):
    """Paper broker that rejects or partially fills specific client_order_ids."""
    script = {"reject": set(), "partial": {}}

    class ScriptedBroker(PaperBrokerProvider):
        def place_order(self, request):
            if request.client_order_id in script["reject"]:
                return self._reject(request, "SCRIPTED_REJECT")
            result = super().place_order(request)
            qty = script["partial"].get(request.client_order_id)
            if qty is None:
                return result
            return dataclasses.replace(
                result, filled_quantity=qty,
                fee=self.fee_model(request.side, qty * result.avg_fill_price))

    monkeypatch.setattr(engine_module, "PaperBrokerProvider", ScriptedBroker)
    return script


def _sells(state, symbol="AAPL"):
    return [f for f in state["fills"] if f["symbol"] == symbol and f["side"] == "SELL"]


def _events(state, kind):
    return [e for e in state["session"]["events"] if e["type"] == kind]


def _order(state, coid):
    (order,) = [o for o in state["orders"] if o["client_order_id"] == coid]
    return order


def test_rejected_exit_is_retried_next_bar_with_a_new_id(scripted_broker):
    scripted_broker["reject"].add("pfl1-20261001-AAPL-SELL-1")
    state = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]),
                        session_bars(DAY, {"AAPL": 100.0}), BINDING)
    assert _order(state, "pfl1-20261001-AAPL-SELL-1")["status"] == "REJECTED"
    (sell,) = _sells(state)
    assert (sell["client_order_id"], sell["kind"], sell["timestamp"]) == (
        "pfl1-20261001-AAPL-SELL-2", "TIME_EXIT", minute(DAY, 10, 36))
    assert state["positions"] == {} and state["session"]["status"] == "COMPLETE"
    assert _events(state, "DUPLICATE_CLIENT_ORDER_ID") == []
    assert reconcile_state(state).ok


def test_restart_after_rejected_exit_is_idempotent(scripted_broker):
    scripted_broker["reject"].add("pfl1-20261001-AAPL-SELL-1")
    gate = trade_gate(DAY, ["AAPL"])
    bars = session_bars(DAY, {"AAPL": 100.0})
    full = run_session(new_account_state(BINDING), gate, bars, BINDING)
    rejected_at = minute(DAY, 10, 35)
    s = start_session(new_account_state(BINDING), gate, BINDING)
    for m in sorted(bars):
        s = step_bar(s, m, bars[m], BINDING)
        if m == rejected_at:
            break
    checkpoint = json.loads(state_bytes(s))
    assert step_bar(checkpoint, rejected_at, bars[rejected_at], BINDING) == checkpoint
    for m in sorted(bars):
        if m > rejected_at:
            checkpoint = step_bar(checkpoint, m, bars[m], BINDING)
    assert state_bytes(finish_session(checkpoint, BINDING)) == state_bytes(full)


def test_carried_position_retries_after_rejected_liquidation(scripted_broker):
    missing = {(h, m): {"AAPL": None} for h in range(10, 16) for m in range(60)
               if (h, m) >= (10, 35)}
    day1 = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]),
                       session_bars(DAY, {"AAPL": 100.0}, missing), BINDING)
    assert day1["session"]["status"] == "UNRESOLVED_EXIT"
    scripted_broker["reject"].add("pfl1-20261001-AAPL-SELL-1")
    day2 = run_session(day1, trade_gate(DAY2, ["MSFT"]),
                       session_bars(DAY2, {"AAPL": 101.0, "MSFT": 50.0}), BINDING)
    (sell,) = _sells(day2)
    assert (sell["client_order_id"], sell["kind"], sell["timestamp"]) == (
        "pfl1-20261001-AAPL-SELL-2", "EXIT", minute(DAY2, 9, 31))
    assert day2["positions"] == {}
    assert reconcile_state(day2).ok


def test_partial_exit_fill_keeps_the_remainder_open_and_records_it(scripted_broker):
    scripted_broker["partial"]["pfl1-20261001-AAPL-SELL-1"] = 10.0
    state = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]),
                        session_bars(DAY, {"AAPL": 100.0}), BINDING)
    (mismatch,) = _events(state, "EXIT_FILL_QUANTITY_MISMATCH")
    assert (mismatch["held"], mismatch["filled"]) == (14.977526, 10.0)
    sells = _sells(state)
    assert [(f["client_order_id"], f["timestamp"]) for f in sells] == [
        ("pfl1-20261001-AAPL-SELL-1", minute(DAY, 10, 35)),
        ("pfl1-20261001-AAPL-SELL-2", minute(DAY, 10, 36)),
    ]
    assert sells[0]["quantity"] == 10.0
    assert sells[1]["quantity"] == 4.977526        # exact 6-dp remainder, no float drift
    assert state["positions"] == {}
    assert reconcile_state(state).ok


BAD_BARS = [
    Bar(math.nan, 100.0, 100.0, 100.0),
    Bar(100.0, 100.0, 100.0, math.nan),
    Bar(0.0, 100.0, 0.0, 100.0),
    Bar(-1.0, 100.0, -1.0, 100.0),
    Bar(100.0, 99.0, 101.0, 100.0),          # inverted high/low
    Bar(100.0, 100.5, 99.8, 101.0),          # close above high
    Bar(math.inf, math.inf, 100.0, 100.0),
]


@pytest.mark.parametrize("bad", BAD_BARS)
def test_invalid_entry_bar_is_a_missing_entry_bar(bad):
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 35): {"AAPL": bad}})
    state = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]), bars, BINDING)
    assert state["fills"] == [] and state["orders"] == []
    assert [(e["symbol"], e["reason"]) for e in _events(state, "NO_TRADE_SYMBOL")] == [
        ("AAPL", "MISSING_ENTRY_BAR")]
    assert [(e["symbol"], e["reason"]) for e in _events(state, INVALID_BAR)] == [
        ("AAPL", INVALID_BAR)]


@pytest.mark.parametrize("bad", BAD_BARS)
def test_invalid_exit_bar_defers_the_exit_to_the_next_valid_bar(bad):
    bars = session_bars(DAY, {"AAPL": 100.0}, {(10, 35): {"AAPL": bad}})
    state = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]), bars, BINDING)
    (sell,) = _sells(state)
    assert (sell["kind"], sell["timestamp"], sell["ref_price"]) == (
        "TIME_EXIT", minute(DAY, 10, 36), 100.0)


@pytest.mark.parametrize("bad", [Bar(100.0, 100.0, 50.0, math.nan),
                                 Bar(100.0, 100.0, 120.0, 90.0),
                                 Bar(100.0, 100.0, 50.0, -5.0)])
def test_invalid_mark_bar_is_ignored_and_risk_check_still_runs(bad):
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 40): {"AAPL": bad}})
    s = start_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]), BINDING)
    for m in sorted(bars):
        s = step_bar(s, m, bars[m], BINDING)
        if m == minute(DAY, 9, 40):
            break
    assert s["marks"]["AAPL"] == 100.0
    assert math.isfinite(s["session"]["risk"]["equity"])
    assert s["session"]["last_minute_utc"] == minute(DAY, 9, 40)
    assert _events(s, INVALID_BAR)
    assert "AAPL" in s["positions"]                  # the low of the bad bar was not a stop


def test_start_session_rejects_a_gate_from_another_contract():
    gate = GateResult(DAY, "TRADE", (), leg="LONG",
                      candidates=(GateCandidate(0, "AAPL", "LONG_WATCH", "LONG", "AAPL"),),
                      schedule=schedule(DAY), contract_sha256="0" * 64)
    with pytest.raises(ValueError, match="different contract"):
        start_session(new_account_state(BINDING), gate, BINDING)


def test_start_session_rejects_same_or_earlier_trading_day():
    state = run_session(new_account_state(BINDING), trade_gate(DAY2, ["AAPL"]),
                        session_bars(DAY2, {"AAPL": 100.0}), BINDING)
    for day in (DAY2, DAY):
        with pytest.raises(ValueError, match="not after the last recorded session"):
            start_session(state, trade_gate(day, ["AAPL"]), BINDING)


def test_rejected_stop_retries_at_next_bar_open_even_after_price_recovers(scripted_broker):
    scripted_broker["reject"].add("pfl1-20261001-AAPL-SELL-1")
    bars = session_bars(DAY, {"AAPL": 100.0}, {
        (9, 40): {"AAPL": Bar(100.0, 100.0, 99.0, 99.2)},      # touches the stop
        (9, 41): {"AAPL": Bar(100.5, 100.6, 100.4, 100.5)},    # recovered above the stop
    })
    state = run_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]), bars, BINDING)
    (sell,) = _sells(state)
    assert (sell["client_order_id"], sell["kind"], sell["timestamp"], sell["ref_price"]) == (
        "pfl1-20261001-AAPL-SELL-2", "STOP_LOSS", minute(DAY, 9, 41), 100.5)
    assert reconcile_state(state).ok


def test_filled_attempt_id_with_open_position_is_ledger_inconsistent():
    gate = trade_gate(DAY, ["AAPL"])
    bars = session_bars(DAY, {"AAPL": 100.0})
    s = start_session(new_account_state(BINDING), gate, BINDING)
    for m in sorted(bars):
        s = step_bar(s, m, bars[m], BINDING)
        if m == minute(DAY, 9, 35):
            break
    ghost = dict(s["orders"][0], client_order_id="pfl1-20261001-AAPL-SELL-1", side="SELL")
    s["orders"].append(ghost)
    for m in sorted(bars):
        if m > minute(DAY, 9, 35):
            s = step_bar(s, m, bars[m], BINDING)
    s = finish_session(s, BINDING)
    (event,) = _events(s, "LEDGER_INCONSISTENT")      # recorded once, not every bar
    assert event["client_order_id"] == "pfl1-20261001-AAPL-SELL-1"
    assert _sells(s) == []
    assert s["session"]["status"] == "UNRESOLVED_EXIT"
    assert not reconcile_state(s).ok


def test_colliding_bar_keys_are_both_invalid():
    s = start_session(new_account_state(BINDING), trade_gate(DAY, ["AAPL"]), BINDING)
    s = step_bar(s, minute(DAY, 9, 35), {"AAPL": Bar(100.0, 100.0, 100.0, 100.0),
                                         "aapl": Bar(101.0, 101.0, 101.0, 101.0)}, BINDING)
    assert [e["reason"] for e in _events(s, INVALID_BAR)] == ["BAR_KEY_COLLISION"] * 2
    assert s["fills"] == []
    assert [e["reason"] for e in _events(s, "NO_TRADE_SYMBOL")] == ["MISSING_ENTRY_BAR"]
