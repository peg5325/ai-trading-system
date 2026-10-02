from __future__ import annotations

import copy
import json

import pytest

from src.freeze_paper.binding import binding_from_contract
from src.freeze_paper.engine import (
    COMPLETE,
    HALTED,
    NO_TRADE,
    UNRESOLVED_EXIT,
    Bar,
    client_order_id,
    finish_session,
    new_account_state,
    run_session,
    start_session,
    state_bytes,
    step_bar,
)
from src.freeze_paper.gates import NO_TRADE as GATE_NO_TRADE
from src.freeze_paper.gates import GateResult
from src.freeze_paper.reconcile import reconcile_state
from tests.freeze_paper_test_helpers import BINDING, minute, session_bars, trade_gate

DAY = "2026-10-01"
DAY2 = "2026-10-02"
EARLY_CLOSE = "2026-11-27"


def _run(gate, bars, binding=BINDING, state=None):
    return run_session(state or new_account_state(binding), gate, bars, binding)


def _fills(state, side=None):
    return [f for f in state["fills"] if side is None or f["side"] == side]


def _sell(state, symbol):
    (fill,) = [f for f in state["fills"] if f["symbol"] == symbol and f["side"] == "SELL"]
    return fill


def _events(state, kind):
    return [e for e in state["session"]["events"] if e["type"] == kind]


def _variant(**changes):
    contract = copy.deepcopy(BINDING.contract)
    for path, value in changes.items():
        section, key = path.split("__")
        contract[section][key] = value
    return binding_from_contract(contract, BINDING.sha256)


def test_flat_day_enters_at_0935_open_and_time_exits_after_60_minutes():
    state = _run(trade_gate(DAY, ["AAPL"]), session_bars(DAY, {"AAPL": 100.0}))
    buy = _fills(state, "BUY")[0]
    sell = _sell(state, "AAPL")
    assert buy["timestamp"] == minute(DAY, 9, 35)
    assert buy["quantity"] == 14.977526                # floor6(1500 / (100 * 1.0005 * 1.001))
    assert buy["price"] == pytest.approx(100.05)
    assert buy["client_order_id"] == "pfl1-20261001-AAPL-BUY"
    assert (sell["kind"], sell["timestamp"], sell["ref_price"]) == (
        "TIME_EXIT", minute(DAY, 10, 35), 100.0)
    assert sell["price"] == pytest.approx(99.95)
    assert sell["client_order_id"] == client_order_id(DAY, "AAPL", "SELL", 1)
    assert sell["client_order_id"] == "pfl1-20261001-AAPL-SELL-1"
    assert state["session"]["status"] == COMPLETE and state["positions"] == {}
    assert state["session"]["sizing_equity"] == 2000.0


def test_same_bar_stop_and_target_takes_stop_first():
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 40): {"AAPL": Bar(100.0, 103.0, 99.0, 100.0)}})
    sell = _sell(_run(trade_gate(DAY, ["AAPL"]), bars), "AAPL")
    assert sell["kind"] == "STOP_LOSS"
    assert sell["ref_price"] == pytest.approx(100.05 * (1 - 0.0075))
    assert sell["timestamp"] == minute(DAY, 9, 40)


def test_stop_is_scanned_on_the_entry_bar():
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 35): {"AAPL": Bar(100.0, 100.2, 99.0, 99.5)}})
    state = _run(trade_gate(DAY, ["AAPL"]), bars)
    sell = _sell(state, "AAPL")
    assert (sell["kind"], sell["timestamp"]) == ("STOP_LOSS", minute(DAY, 9, 35))
    assert sell["ref_price"] == pytest.approx(100.05 * 0.9925)


def test_gap_down_stop_fills_at_the_bar_open():
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 40): {"AAPL": Bar(98.0, 98.5, 97.5, 98.2)}})
    sell = _sell(_run(trade_gate(DAY, ["AAPL"]), bars), "AAPL")
    assert (sell["kind"], sell["ref_price"]) == ("STOP_LOSS", 98.0)
    assert sell["price"] == pytest.approx(98.0 * 0.9995)


def test_gap_up_target_gets_no_gap_credit():
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 40): {"AAPL": Bar(103.0, 103.5, 102.5, 103.2)}})
    sell = _sell(_run(trade_gate(DAY, ["AAPL"]), bars), "AAPL")
    assert sell["kind"] == "TAKE_PROFIT"
    assert sell["ref_price"] == pytest.approx(100.05 * 1.015)


def test_time_exit_with_missing_bar_uses_next_available_open():
    bars = session_bars(DAY, {"AAPL": 100.0}, {
        (10, 35): {"AAPL": None}, (10, 36): {"AAPL": Bar(100.4, 100.5, 100.3, 100.4)}})
    sell = _sell(_run(trade_gate(DAY, ["AAPL"]), bars), "AAPL")
    assert (sell["kind"], sell["timestamp"], sell["ref_price"]) == (
        "TIME_EXIT", minute(DAY, 10, 36), 100.4)


def test_eod_flatten_at_1550_on_a_normal_day():
    binding = _variant(exits__time_exit_minutes=1000)
    state = _run(trade_gate(DAY, ["AAPL"]), session_bars(DAY, {"AAPL": 100.0}), binding)
    sell = _sell(state, "AAPL")
    assert (sell["kind"], sell["timestamp"]) == ("EOD_FLATTEN", minute(DAY, 15, 50))
    assert state["session"]["eod_utc"] == minute(DAY, 15, 50)


def test_early_close_flattens_at_1250_and_ignores_bars_after_close():
    binding = _variant(exits__time_exit_minutes=1000)
    bars = session_bars(EARLY_CLOSE, {"AAPL": 100.0}, end=(16, 0))
    bars[minute(EARLY_CLOSE, 12, 50)].pop("AAPL")          # EOD bar missing -> next bar
    bars[minute(EARLY_CLOSE, 13, 5)] = {"AAPL": Bar(1.0, 1.0, 1.0, 1.0)}   # after close
    state = _run(trade_gate(EARLY_CLOSE, ["AAPL"]), bars, binding)
    sell = _sell(state, "AAPL")
    assert (sell["kind"], sell["timestamp"]) == ("EOD_FLATTEN", minute(EARLY_CLOSE, 12, 51))
    assert state["session"]["eod_utc"] == minute(EARLY_CLOSE, 12, 50)
    assert state["session"]["last_minute_utc"] == minute(EARLY_CLOSE, 12, 59)
    assert state["session"]["status"] == COMPLETE


def test_daily_loss_halt_liquidates_remaining_positions_at_next_bar_open():
    bars = session_bars(DAY, {"AAPL": 100.0, "MSFT": 200.0},
                        {(9, 40): {"AAPL": Bar(90.0, 90.0, 89.0, 89.5)}})
    state = _run(trade_gate(DAY, ["AAPL", "MSFT"]), bars)
    assert _sell(state, "AAPL")["kind"] == "STOP_LOSS"
    (halt,) = _events(state, "RISK_HALT")
    assert (halt["reason"], halt["minute_utc"]) == ("DAILY_LOSS_LIMIT", minute(DAY, 9, 40))
    msft = _sell(state, "MSFT")
    assert (msft["kind"], msft["timestamp"], msft["ref_price"]) == ("EXIT", minute(DAY, 9, 41), 200.0)
    assert state["session"]["status"] == HALTED
    assert state["halted"]["reason"] == "DAILY_LOSS_LIMIT"
    nxt = _run(trade_gate(DAY2, ["AAPL"]), session_bars(DAY2, {"AAPL": 100.0}), state=state)
    assert nxt["session"]["status"] == HALTED
    assert [f for f in nxt["fills"] if f["trading_day"] == DAY2] == []
    assert [(e["symbol"], e["reason"]) for e in _events(nxt, "NO_TRADE_SYMBOL")] == [
        ("AAPL", "HALTED")]


def test_drawdown_halt_uses_the_restored_peak():
    bars = session_bars(DAY, {"AAPL": 100.0}, {(9, 36): {"AAPL": Bar(100.0, 100.0, 99.5, 99.5)}})
    restored = new_account_state(BINDING)
    restored["peak_equity"] = 2215.0
    state = _run(trade_gate(DAY, ["AAPL"]), bars, state=restored)
    (halt,) = _events(state, "RISK_HALT")
    assert (halt["reason"], halt["minute_utc"]) == ("MAX_DRAWDOWN", minute(DAY, 9, 36))
    sell = _sell(state, "AAPL")
    assert (sell["kind"], sell["timestamp"]) == ("EXIT", minute(DAY, 9, 37))
    assert state["peak_equity"] == 2215.0
    fresh = _run(trade_gate(DAY, ["AAPL"]), bars)
    assert _events(fresh, "RISK_HALT") == []


def test_unresolved_exit_is_carried_and_liquidated_next_session_then_halted():
    missing = {(h, m): {"AAPL": None} for h in range(10, 16) for m in range(60) if (h, m) >= (10, 35)}
    bars = session_bars(DAY, {"AAPL": 100.0}, missing)
    day1 = _run(trade_gate(DAY, ["AAPL"]), bars)
    assert day1["session"]["status"] == UNRESOLVED_EXIT
    assert day1["halted"]["reason"] == UNRESOLVED_EXIT
    assert set(day1["positions"]) == {"AAPL"}
    assert day1["prior_close_equity"] == day1["session"]["end_equity"]

    day2 = _run(trade_gate(DAY2, ["MSFT"]), session_bars(DAY2, {"AAPL": 101.0, "MSFT": 50.0}),
                state=day1)
    sell = _sell(day2, "AAPL")
    assert (sell["kind"], sell["timestamp"], sell["ref_price"]) == ("EXIT", minute(DAY2, 9, 30), 101.0)
    assert sell["client_order_id"] == "pfl1-20261001-AAPL-SELL-1"
    assert day2["session"]["status"] == HALTED
    assert day2["session"]["day_start_equity"] == day1["session"]["end_equity"]
    assert not any(f["symbol"] == "MSFT" for f in day2["fills"])
    assert day2["positions"] == {}


def test_missing_entry_bar_and_min_order_notional_are_no_trade_symbols():
    # Equity 2.0: cap 1.5; META's budget 0.75 is under USD 1 and is skipped, so MU (last
    # remaining) gets the whole room.
    binding = _variant(account__initial_equity=2.0)
    bars = session_bars(DAY, {"META": 725.93, "MU": 1075.0, "NVDA": 100.0},
                        {(9, 35): {"NVDA": None}})
    state = _run(trade_gate(DAY, ["NVDA", "META", "MU"]), bars, binding=binding)
    reasons = {(e["symbol"], e["reason"]) for e in _events(state, "NO_TRADE_SYMBOL")}
    assert ("NVDA", "MISSING_ENTRY_BAR") in reasons
    assert ("META", "BELOW_MIN_ORDER_NOTIONAL") in reasons
    assert [(f["symbol"], f["quantity"]) for f in _fills(state, "BUY")] == [("MU", 0.001393)]


def test_observed_2026_10_01_opens_size_all_three_fractional_in_the_engine():
    # v1.2 bought MSFT 1, AMZN 1 and skipped SPY (EXPOSURE_ROOM): about 38% deployed.
    bars = session_bars(DAY, {"MSFT": 517.56, "AMZN": 250.895, "SPY": 764.0})
    state = _run(trade_gate(DAY, ["MSFT", "AMZN", "SPY"]), bars)
    buys = _fills(state, "BUY")
    assert [(f["symbol"], f["quantity"]) for f in buys] == [
        ("MSFT", 0.964624), ("AMZN", 1.989879), ("SPY", 0.65347)]
    used = sum(f["quantity"] * f["price"] + f["fee"] for f in buys)
    assert 1499.99 < used <= 0.75 * 2000.0 + 1e-9
    sells = _fills(state, "SELL")
    assert {f["symbol"]: f["quantity"] for f in sells} == {f["symbol"]: f["quantity"] for f in buys}
    # every fractional exit is the 10:35 ET time exit, well before close - 60 minutes
    assert {(f["kind"], f["timestamp"]) for f in sells} == {("TIME_EXIT", minute(DAY, 10, 35))}
    assert state["positions"] == {} and state["session"]["status"] == COMPLETE
    assert reconcile_state(state).ok


def test_risk_never_rejects_procedure_accepted_entries_including_3x_etfs():
    bars = session_bars(DAY, {"TQQQ": 52.3, "SOXL": 31.7, "AAPL": 231.4})
    state = _run(trade_gate(DAY, ["TQQQ", "SOXL", "AAPL"]), bars)
    assert _events(state, "REJECT_ENTRY") == []
    buys = _fills(state, "BUY")
    assert [f["symbol"] for f in buys] == ["TQQQ", "SOXL", "AAPL"]
    used = sum(f["quantity"] * f["price"] + f["fee"] for f in buys)
    assert used <= 0.75 * 2000.0 + 1e-9


def test_inverse_leg_buys_the_mapped_etf_with_its_own_bar():
    gate = trade_gate(DAY, ["SQQQ", "SOXS"], leg="INVERSE", signal_symbols=["TQQQ", "SOXL"])
    bars = session_bars(DAY, {"TQQQ": 52.0, "SOXL": 30.0, "SQQQ": 20.0}, end=(16, 0))
    state = _run(gate, bars)
    (buy,) = _fills(state, "BUY")
    assert (buy["symbol"], buy["leg"], buy["ref_price"]) == ("SQQQ", "INVERSE", 20.0)
    assert ("SOXS", "MISSING_ENTRY_BAR") in {
        (e["symbol"], e["reason"]) for e in _events(state, "NO_TRADE_SYMBOL")}
    assert all(f["symbol"] not in {"TQQQ", "SOXL"} for f in state["fills"])


def test_no_trade_gate_places_no_orders():
    gate = GateResult(DAY, GATE_NO_TRADE, ("REGIME_UNCERTAIN_NOT_TRADABLE",),
                      contract_sha256=BINDING.sha256)
    state = _run(gate, session_bars(DAY, {"AAPL": 100.0}))
    assert state["session"]["status"] == NO_TRADE and state["orders"] == []


def test_checkpoint_after_bar_k_then_resume_equals_full_run():
    gate = trade_gate(DAY, ["AAPL", "MSFT"])
    bars = session_bars(DAY, {"AAPL": 100.0, "MSFT": 200.0},
                        {(9, 50): {"AAPL": Bar(100.0, 101.8, 99.9, 101.0)}})
    full = _run(gate, bars)
    minutes = sorted(bars)
    for k in (0, 5, 6, 20, len(minutes) - 1):
        s = start_session(new_account_state(BINDING), gate, BINDING)
        for m in minutes[:k + 1]:
            s = step_bar(s, m, bars[m], BINDING)
        s = json.loads(state_bytes(s))              # restart from the persisted checkpoint
        for m in minutes[k + 1:]:
            s = step_bar(s, m, bars[m], BINDING)
        assert state_bytes(finish_session(s, BINDING)) == state_bytes(full)


def test_replayed_minute_is_a_no_op_and_duplicate_order_id_never_refills():
    gate = trade_gate(DAY, ["AAPL"])
    bars = session_bars(DAY, {"AAPL": 100.0})
    entry = minute(DAY, 9, 35)
    s = start_session(new_account_state(BINDING), gate, BINDING)
    for m in sorted(bars):
        s = step_bar(s, m, bars[m], BINDING)
        if m == entry:
            break
    assert step_bar(s, entry, bars[entry], BINDING) == s
    # Crash-after-order simulation: the BUY order is stored but the entry bar is re-run.
    crashed = copy.deepcopy(s)
    crashed["session"]["last_minute_utc"] = minute(DAY, 9, 34)
    crashed["session"]["entries_done"] = False
    crashed["positions"] = {}
    crashed["fills"] = []
    rerun = step_bar(crashed, entry, bars[entry], BINDING)
    assert rerun["fills"] == []
    assert [o["client_order_id"] for o in rerun["orders"]] == ["pfl1-20261001-AAPL-BUY"]
    assert _events(rerun, "DUPLICATE_CLIENT_ORDER_ID")


def test_engine_output_is_byte_identical_on_rerun():
    gate = trade_gate(DAY, ["TQQQ", "AAPL"])
    bars = session_bars(DAY, {"TQQQ": 52.3, "AAPL": 231.4},
                        {(9, 45): {"TQQQ": Bar(52.3, 53.2, 52.0, 53.0)}})
    assert state_bytes(_run(gate, bars)) == state_bytes(_run(gate, bars))


def test_state_from_another_contract_is_refused():
    state = new_account_state(BINDING)
    state["contract_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different contract"):
        start_session(state, trade_gate(DAY, ["AAPL"]), BINDING)
