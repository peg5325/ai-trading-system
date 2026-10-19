"""Budget unit = one run_account call (closeout 2026-10-19, §2.6). Synthetic stubs/counters and tmp ledgers only."""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest
from test_daytrade_paper import EXT_OK, NO_EVENTS, PREV, PRIOR, QC, s1_day

from src.daytrade_v13.policy import COST_MODELS, V10_PLUS, V13
from src.daytrade_v13.runner import run_account, run_arm
from src.evalpath import ledger

ROOT = Path(__file__).resolve().parents[1]
AUTH = "AUTH-TEST"


def plan(n_strategies, n_costs, segments=("A", "B")):
    runs = [{"run_key": f"s{i}-c{j}", "policy": f"p{i}", "cost_model_id": f"c{j}"}
            for i in range(n_strategies) for j in range(n_costs)]
    return ledger.expand_plan(runs, list(segments))


class Counter:
    def __init__(self):
        self.n = 0

    def __call__(self, *a, **k):
        self.n += 1
        return {"ok": True}


def execute(lp, units, budget, counter, extra_keys=()):
    """What a driver does: preflight the whole plan, then call through the guard."""
    ledger.preflight(lp, AUTH, budget, units)
    g = ledger.AccountCallGuard(lp, AUTH, budget, [u["unit_key"] for u in units])
    for u in units:
        g.call(u["unit_key"], counter)
    for k in extra_keys:
        g.call(k, counter)


def test_twelve_accounts_against_a_limit_of_eight_are_refused_before_any_call(tmp_path):
    units = plan(2, 3)
    assert len(units) == 12
    c = Counter()
    with pytest.raises(ledger.BudgetExceeded, match="before any computation"):
        execute(tmp_path / "l.jsonl", units, 8, c)
    assert c.n == 0 and ledger.read(tmp_path / "l.jsonl") == []


def test_exactly_eight_accounts_run_and_are_all_recorded(tmp_path):
    units = plan(2, 2)
    assert len(units) == 8
    c = Counter()
    execute(tmp_path / "l.jsonl", units, 8, c)
    recs = ledger.read(tmp_path / "l.jsonl")
    assert c.n == 8
    assert [r["event"] for r in recs].count("reserve") == 8 and [r["event"] for r in recs].count("complete") == 8
    assert ledger.used_units(recs, AUTH) == 8


def test_a_hidden_extra_call_is_refused_before_it_runs(tmp_path):
    units = plan(1, 1)
    c = Counter()
    with pytest.raises(ledger.HiddenCall):
        execute(tmp_path / "l.jsonl", units, 8, c, extra_keys=["s0-c0/A-stress"])
    assert c.n == 2  # only the two planned accounts ran
    g = ledger.AccountCallGuard(tmp_path / "l2.jsonl", AUTH, 8, ["x/A"])
    g.call("x/A", c)
    with pytest.raises(ledger.HiddenCall):
        g.call("x/A", c)  # a planned unit cannot run twice
    assert c.n == 3


def test_failure_and_restart_keep_the_budget_spent(tmp_path):
    lp = tmp_path / "l.jsonl"
    units = plan(1, 2)  # 4 units

    def boom(*a, **k):
        raise RuntimeError("crash")
    ledger.preflight(lp, AUTH, 4, units)
    g = ledger.AccountCallGuard(lp, AUTH, 4, [u["unit_key"] for u in units])
    with pytest.raises(RuntimeError):
        g.call(units[0]["unit_key"], boom)
    # simulate a crash between reservation and finish
    ledger.reserve(lp, AUTH, 4, units[1]["unit_key"], 1, {"note": "crashed before finishing"})
    recs = ledger.read(lp)
    assert ledger.used_units(recs, AUTH) == 2
    assert [r["event"] for r in recs] == ["reserve", "fail", "reserve"]
    # a restart cannot reuse those keys, and renaming the batch still draws on the same authorization budget
    with pytest.raises(ledger.BudgetExceeded, match="reuses"):
        ledger.preflight(lp, AUTH, 4, units)
    renamed = ledger.expand_plan([{"run_key": "renamed-batch"}, {"run_key": "renamed-batch-2"}], ["A", "B"])
    with pytest.raises(ledger.BudgetExceeded, match="already used"):
        ledger.preflight(lp, AUTH, 4, renamed)
    ledger.preflight(lp, AUTH, 4, renamed[:2])  # exactly what is left


@pytest.mark.parametrize("round_", range(20))
def test_concurrent_reservations_never_exceed_the_limit(round_, tmp_path):
    """Repeated rounds: an unlock-before-flush ledger exceeded the limit only occasionally (9 of 20 once)."""
    lp = tmp_path / "l.jsonl"
    ok, refused = [], []

    def worker(i):
        try:
            ledger.reserve(lp, AUTH, 8, f"u{i}", 1, {})
            ok.append(i)
        except ledger.BudgetExceeded:
            refused.append(i)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 8 and len(refused) == 32
    assert ledger.used_units(ledger.read(lp), AUTH) == 8


def test_single_run_lock_refuses_a_second_driver(tmp_path):
    lp = tmp_path / "l.jsonl"
    with ledger.single_run(lp), pytest.raises(ledger.RunLocked), ledger.single_run(lp):
        pass
    with ledger.single_run(lp):  # released after the first run
        pass


def test_synthetic_calls_are_a_separate_resource(tmp_path):
    lp = tmp_path / "l.jsonl"
    for i in range(5):
        ledger.reserve(lp, AUTH, 5, f"syn{i}", 1, {}, resource=ledger.SYNTHETIC_UNIT)
    recs = ledger.read(lp)
    assert ledger.used_units(recs, AUTH, ledger.SYNTHETIC_UNIT) == 5
    assert ledger.used_units(recs, AUTH, ledger.MARKET_UNIT) == 0


def test_legacy_records_without_a_resource_are_not_counted_as_market_units():
    legacy = [{"event": "reserve", "authorization_id": AUTH, "run_key": "old", "units": 1}]
    assert ledger.used_units(legacy, AUTH) == 0
    assert ledger.used_units(legacy, AUTH, ledger.LEGACY_UNIT) == 1


# ---------------------------------------------------------------- the compute step's guarded path


def _compute_module():
    spec = importlib.util.spec_from_file_location("eval_repro_v13_compute", ROOT / "scripts" / "eval_repro_v13_compute.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seg_days():
    def di(build):
        reg, pre = build()
        return {"day": "2026-10-05", "regular": reg, "qqq_pre": pre, "prior": PRIOR, "external": EXT_OK,
                "events": NO_EVENTS, "prev_times": QC["prev_times"], "expected_prev": QC["expected_prev"],
                "prev_early_close": False}
    return {"A": [di(s1_day), di(lambda: s1_day(qqq_after=lambda t: PREV * 1.03))], "B": [di(s1_day)]}


@pytest.mark.parametrize("pol", [V10_PLUS, V13], ids=lambda p: p.name)
def test_guarded_compute_equals_run_arm_and_counts_one_unit_per_account(pol, tmp_path):
    comp = _compute_module()
    led = comp.load_ledger_module()
    costs = COST_MODELS["CM030_v1"]
    seg_days = _seg_days()
    calls = []

    def counted(*a, **k):
        calls.append(1)
        return run_account(*a, **k)
    g = led.AccountCallGuard(tmp_path / "l.jsonl", AUTH, 2, ["r/A", "r/B"], led.SYNTHETIC_UNIT)
    out = comp.run_guarded(seg_days, pol, costs, 0, counted, g, "r")
    ref = run_arm(seg_days, pol, costs, 0)
    assert out["G"] == ref["G"] and out["D"] == ref["D"] and len(calls) == out["run_account_calls"] == 2
    for name in ("A", "B"):
        assert out["segments"][name]["equity_end"] == ref["segments"][name]["equity_end"]
    # a third segment that was not planned is refused before its run_account call
    g2 = led.AccountCallGuard(tmp_path / "l2.jsonl", AUTH, 2, ["r/A"], led.SYNTHETIC_UNIT)
    calls.clear()
    with pytest.raises(led.HiddenCall):
        comp.run_guarded(seg_days, pol, costs, 0, counted, g2, "r")
    assert len(calls) == 1
