"""Evaluation-path checks for the v1.3 route (MINIMAL_PATCH_PLAN 2026-10-16 §3). Synthetic bars and tmp DBs only.

Time conversion (store._bar once), order events, causality under later price/volume changes, deleted bars and early
data end, the quality mask, the cost identity, account/cash state, verdict axes and run records.
"""
from __future__ import annotations

from datetime import date

import pytest
from test_daytrade_paper import (
    EXT_OK,
    NO_EVENTS,
    PREV,
    PRIOR,
    QC,
    T,
    s1_day,
    tqqq_default,
)
from test_daytrade_shadow import gap_stop_day, s3_day
from test_daytrade_v11_precount import CAL, SEG, make_db

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar, data_quality, five_min_close
from src.daytrade_paper.situation import classify_open, opening_signals
from src.daytrade_paper.store import _bar
from src.daytrade_v11 import data as V
from src.daytrade_v13.day import (
    AccountState,
    InvalidRun,
    cost_breakdown,
    sell_net,
    simulate_day,
)
from src.daytrade_v13.policy import (
    BASE_COSTS,
    COST_MODELS,
    V10_PLUS,
    V13,
    Costs,
    cost_model_id,
)
from src.daytrade_v13.report import NOT_EVALUATED, PASS, verdict_axes
from src.daytrade_v13.runio import RUN_RECORD_FIELDS, config_hash, run_record
from src.daytrade_v13.runner import (
    ACCOUNT_SIM,
    FIXED_QTY_DIAGNOSTIC,
    fixed_qty_diagnostic,
    run_account,
)
from src.history.loader import load_candles

DAY = "2026-10-05"
CM030, CM020 = COST_MODELS["CM030_v1"], COST_MODELS["CM020_diag_v1"]
ENTRY_KEYS = ("kind", "symbol", "entry_t", "entry_raw", "entry_fill", "stop", "stop_frac", "notional", "shares",
              "buy_commission", "sizing_equity", "high_halved", "signal_t", "timing")


def sim(reg, pre, pol=V10_PLUS, costs=BASE_COSTS, delay=0, ev=NO_EVENTS, ext=EXT_OK, acct=None):
    return simulate_day(DAY, reg, pre, PRIOR, ext, ev, acct or AccountState(), pol, costs, delay, **QC)


def entry(trade):
    return {k: trade[k] for k in ENTRY_KEYS}


def first(out, kind):
    return next(t for t in out["trades"] if t["kind"] == kind)


def change_after(reg, t_after, keep_fill_open_at=None, price=0.8, vol=10.0):
    """Same bars before `t_after`; every later bar re-priced and re-volumed. At `keep_fill_open_at` only the open
    is kept (the high/low/close/volume of the fill bar are not known when the order is sized)."""
    out = {}
    for sym, bs in reg.items():
        new = []
        for b in bs:
            if b.t < t_after:
                new.append(b)
            elif b.t == keep_fill_open_at:
                new.append(Bar(b.t, b.open, b.open * 1.01, b.open * 0.999, b.open * 1.005, b.volume * vol))
            else:
                p = b.open * price
                new.append(Bar(b.t, p, p * 1.01, p * 0.99, p, b.volume * vol))
        out[sym] = new
    return out


def day_input(reg, pre, ev=NO_EVENTS, ext=EXT_OK):
    return {"day": DAY, "regular": reg, "qqq_pre": pre, "prior": PRIOR, "external": ext, "events": ev,
            "prev_times": QC["prev_times"], "expected_prev": QC["expected_prev"], "prev_early_close": False}


# ---------------------------------------------------------------- T-TIME: one conversion, in store._bar


def test_toss_label_is_the_end_minute_and_store_bar_shifts_it_once():
    row = lambda hhmm, sess="REGULAR": (f"2026-10-05T{hhmm}:00-04:00", sess, 1.0, 1.0, 1.0, 1.0, 1.0)
    assert _bar(row("09:30")) == ("2026-10-05", "PRE", Bar(T(9, 29), 1.0, 1.0, 1.0, 1.0, 1.0))
    assert _bar(row("09:31"))[1:] == ("REGULAR", Bar(T(9, 30), 1.0, 1.0, 1.0, 1.0, 1.0))
    assert _bar(row("16:00"))[1:] == ("REGULAR", Bar(T(15, 59), 1.0, 1.0, 1.0, 1.0, 1.0))
    assert _bar(row("16:01"))[1] == "POST"
    # T-TIME-6: the DB session column (start-minute convention) is ignored
    assert _bar(row("10:00", "POST"))[1] == "REGULAR"
    assert _bar(row("09:31", "PRE"))[1] == "REGULAR"


def test_loader_keeps_the_label_and_candle_bar_shifts_exactly_once(tmp_path):
    db = make_db(tmp_path / "h.sqlite", days=[date(2023, 7, 5)])
    candles = load_candles(str(db), "QQQ", "1m")
    reg = [c for c in candles if c.ts_exchange.hour == 9 and c.ts_exchange.minute == 31]
    assert len(reg) == 1
    assert (reg[0].ts_exchange.hour, reg[0].ts_exchange.minute) == (9, 31)  # history loader: no shift
    _day, session, bar = V.candle_bar(reg[0])
    assert (session, bar.t) == ("REGULAR", T(9, 30))  # one shift; T(9, 29) would be a double shift


def test_early_close_day_has_210_regular_bars_ending_1259():
    row = lambda hhmm: (f"2026-11-27T{hhmm}:00-05:00", "REGULAR", 1.0, 1.0, 1.0, 1.0, 1.0)
    assert _bar(row("13:00"))[1:] == ("REGULAR", Bar(T(12, 59), 1.0, 1.0, 1.0, 1.0, 1.0))
    assert _bar(row("13:01"))[1] == "POST"
    full = {s: [Bar(t, 1, 1, 1, 1, 1) for t in range(R.OPEN, T(13, 0))] for s in R.SYMBOLS}
    q = data_quality(full, PRIOR, True, R.DECISION, QC["prev_times"], QC["expected_prev"])
    assert q["ok"] and q["expected_bars"] == 210
    short = {s: bs[:-1] for s, bs in full.items()}
    assert not data_quality(short, PRIOR, True, R.DECISION, QC["prev_times"], QC["expected_prev"])["ok"]


def test_five_minute_close_exists_only_once_its_last_minute_exists():
    bt = {t: Bar(t, 1, 1, 1, float(t), 1) for t in range(T(9, 30), T(9, 34))}  # 09:30..09:33 only
    assert five_min_close(bt, T(9, 30)) is None  # the 09:30 bucket closes with the 09:34 bar (label 09:35)
    bt[T(9, 34)] = Bar(T(9, 34), 1, 1, 1, 99.0, 1)
    assert five_min_close(bt, T(9, 30)) == 99.0


# ---------------------------------------------------------------- order events (§3.1)


def test_order_timing_records_signal_sizing_submit_and_fill():
    reg, pre = s1_day()
    tr = first(sim(reg, pre), "S1")
    tm = tr["timing"]
    fill_bar = next(b for b in reg["TQQQ"] if b.t == R.DECISION)
    assert tm["signal_t"] == R.DECISION and tm["data_available_through_t"] == R.DECISION - 1
    assert tm["sizing_price"] == fill_bar.open and tm["sizing_price_kind"] == "bar_open"
    assert tm["sizing_price_known_at_t"] == tm["order_submit_t"] == tm["fill_t"] == R.DECISION
    assert tm["fill_price"] == fill_bar.open * (1 + BASE_COSTS.slippage)
    assert tm["order_qty"] == tm["filled_qty"] == tr["shares"] == tr["notional"] / tr["entry_fill"]
    assert "not verified" in tm["fill_assumption"]


def test_sizing_depends_on_the_fill_bar_open_by_contract():
    """The observed-open model: the bar-T open sets the stop distance and the notional (contract v1.0)."""
    reg, pre = s1_day()
    reg2 = {**reg, "TQQQ": [Bar(b.t, b.open + 0.8, b.high + 0.8, b.low, b.close, b.volume) if b.t == R.DECISION
                            else b for b in reg["TQQQ"]]}
    # v1.3 (1% risk, 70% cap): the wider stop distance moves the notional below the cap; v1.0+ would refuse it
    a, b = first(sim(reg, pre, V13), "S1"), first(sim(reg2, pre, V13), "S1")
    assert a["stop"] == b["stop"]
    assert a["sizing"]["binding"] == ["base"] and a["sizing"]["single_cap_binds"]
    assert not b["sizing"]["single_cap_binds"]
    assert b["stop_frac"] > a["stop_frac"] and b["notional"] < a["notional"]
    assert b["timing"]["sizing_price"] == pytest.approx(a["timing"]["sizing_price"] + 0.8)
    # with both inside the single cap the notional is the same: equal quantities do not prove independence
    c = first(sim({**reg, "TQQQ": [Bar(x.t, x.open + 0.05, x.high + 0.05, x.low, x.close, x.volume)
                                   if x.t == R.DECISION else x for x in reg["TQQQ"]]}, pre, V13), "S1")
    assert c["notional"] == a["notional"] and c["stop_frac"] != a["stop_frac"]


def test_delayed_fill_is_sized_at_the_fill_bar_open():
    reg, pre = s1_day()
    tm = first(sim(reg, pre, delay=1), "S1")["timing"]
    assert tm["signal_t"] == R.DECISION
    assert tm["sizing_price_known_at_t"] == tm["fill_t"] == R.DECISION + 1


# ---------------------------------------------------------------- causality (§3.2)

CASES = {
    # name: (builder, kind, fill bar t, policy, delay)
    "S1": (lambda: s1_day(), "S1", R.DECISION, V10_PLUS, 0),
    "S1_v13": (lambda: s1_day(), "S1", R.DECISION, V13, 0),
    "S1_delay": (lambda: s1_day(), "S1", R.DECISION + 1, V10_PLUS, 1),
    "S3": (s3_day, "S3", R.S3_DECISION, V10_PLUS, 0),
    "S5": (lambda: s1_day(qqq_after=lambda t: PREV * 1.03), "S5", R.S5_DECISION, V13, 0),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_entry_intent_and_quantity_unchanged_by_later_prices_and_volumes(name):
    build, kind, fill_t, pol, delay = CASES[name]
    reg, pre = build()
    base = sim(reg, pre, pol, delay=delay)
    changed = sim(change_after(reg, fill_t, keep_fill_open_at=fill_t), pre, pol, delay=delay)
    assert entry(first(base, kind)) == entry(first(changed, kind))


@pytest.mark.parametrize("cut", ["delete_tqqq_1400", "data_ends_1200"])
def test_missing_later_bars_invalidate_the_day_instead_of_making_a_no_trade_day(cut):
    reg, pre = s1_day()
    if cut == "delete_tqqq_1400":
        reg2 = {**reg, "TQQQ": [b for b in reg["TQQQ"] if b.t != T(14, 0)]}
    else:
        reg2 = {s: [b for b in bs if b.t < T(12, 0)] for s, bs in reg.items()}
    # the real-time decision at 09:45 is the same with or without the later bars
    inp = {"prev_close": PREV, "atr14_pct": 0.01, "gap": 0.005, "bull": True}
    assert classify_open(inp, opening_signals(inp, reg["QQQ"], PRIOR, R.DECISION))["label"] == "S1"
    assert opening_signals(inp, reg["QQQ"], PRIOR, R.DECISION) == opening_signals(inp, reg2["QQQ"], PRIOR,
                                                                                   R.DECISION)
    # the historical run does not turn the day into a zero-trade day: it refuses the day (and the run)
    with pytest.raises(InvalidRun):
        sim(reg2, pre)
    with pytest.raises(InvalidRun):
        run_account([day_input(*s1_day()), day_input(reg2, pre)], V10_PLUS)


def test_quality_mask_excludes_a_day_for_an_afternoon_gap_and_never_scores_it(tmp_path):
    """The v1.1 mask is quality-conditional: a missing 14:00 TQQQ bar removes the whole day (its morning trade
    included) before any account runs. Excluded days are listed with reasons, not scored as zero-return days."""
    base_db = make_db(tmp_path / "a.sqlite")
    base_mask = V.build_mask([V.load_segment(base_db, SEG)], CAL, {"test": True})
    day = "2023-07-25"
    assert day in base_mask["segments"]["T"]["scored"]
    db = make_db(tmp_path / "b.sqlite", drop={("TQQQ", day, T(14, 0))})
    mask = V.build_mask([V.load_segment(db, SEG)], CAL, {"test": True})
    seg = mask["segments"]["T"]
    assert day not in seg["scored"]
    ex = next(e for e in seg["excluded"] if e["date"] == day)
    assert ex["kinds"] == ["data_quality"] and "TQQQ" in " ".join(ex["reasons"])
    assert set(seg["scored"]) == set(base_mask["segments"]["T"]["scored"]) - {day}


# ---------------------------------------------------------------- fills


def test_gap_stop_exit_uses_the_open_as_reference_and_slippage_in_the_fill():
    assert sell_net(1.0, 97.0, CM030)["fill"] == pytest.approx(96.9515, abs=1e-12)
    assert sell_net(1.0, 97.0, CM020)["fill"] == 97.0
    reg, pre = gap_stop_day()
    for costs in (CM030, CM020):
        tr = first(sim(reg, pre, costs=costs), "S1")
        assert tr["exit_reason"] == "STOP_GAP" and tr["exit_raw"] == 49.0
        assert tr["exit_fill"] == pytest.approx(49.0 * (1 - costs.slippage), abs=1e-12)


def test_same_bar_stop_is_taken_before_the_s3_target():
    reg, pre = s3_day()
    base = first(sim(reg, pre), "S3")
    assert base["exit_reason"] == "S3_TARGET_PREV_CLOSE"
    hit = base["exit_t"]
    stop = base["stop"]
    reg2 = {**reg, "TQQQ": [Bar(b.t, b.open, b.high, stop - 0.01, b.close, b.volume) if b.t == hit else b
                            for b in reg["TQQQ"]]}
    tr = first(sim(reg2, pre), "S3")
    assert (tr["exit_t"], tr["exit_reason"], tr["exit_raw"]) == (hit, "STOP", stop)


# ---------------------------------------------------------------- cost identity (P1)

SCEN = {"S1": s1_day, "S1_S5": lambda: s1_day(qqq_after=lambda t: PREV * 1.03), "S3": s3_day,
        "gap_stop": gap_stop_day}


@pytest.mark.parametrize("costs", [CM030, CM020, CM030.scaled(1.5)], ids=["CM030", "CM020", "CM030x1.5"])
@pytest.mark.parametrize("name", sorted(SCEN))
def test_cost_identity_on_every_trade(name, costs):
    reg, pre = SCEN[name]()
    out = sim(reg, pre, V13, costs)
    assert out["trades"]
    for tr in out["trades"]:
        cb = tr["cost_breakdown"]
        assert cb["net_pnl"] == tr["pnl"]
        assert abs(cb["identity_residual"]) <= 1e-9 * max(1.0, tr["notional"])
        assert cb["slippage_impact"] == pytest.approx(
            tr["shares"] * costs.slippage * (tr["entry_raw"] + tr["exit_raw"]), rel=1e-9, abs=1e-12)
        assert cb["slippage_impact"] >= -1e-12 and cb["explicit_fees"] > 0
        assert cb["entry_notional"] == tr["notional"] and cb["exit_notional"] == tr["proceeds"]


def test_cost_identity_worked_example():
    q, pe, px, s, c, f = 10.0, 100.0, 101.0, 0.0005, 0.001, 0.0000206
    fe, fx = pe * (1 + s), px * (1 - s)
    tr = {"shares": q, "entry_raw": pe, "exit_raw": px, "entry_fill": fe, "exit_fill": fx, "notional": q * fe,
          "proceeds": q * fx, "buy_commission": q * fe * c, "sell_commission": q * fx * c, "sec_fee": q * fx * f}
    tr["pnl"] = q * fx * (1 - c - f) - q * fe * (1 + c)
    cb = cost_breakdown(tr)
    assert cb["reference_price_pnl"] == pytest.approx(10.0)
    assert cb["slippage_impact"] == pytest.approx(1.005)
    assert cb["explicit_fees"] == pytest.approx(q * fe * c + q * fx * c + q * fx * f)
    assert cb["net_pnl"] == pytest.approx(10.0 - 1.005 - cb["explicit_fees"])


# ---------------------------------------------------------------- account_sim vs fixed_qty_diagnostic (T-COST-2/3)


def two_day_account(costs, pol=V10_PLUS):
    return run_account([day_input(*s1_day()), day_input(*s1_day())], pol, costs)


def test_account_sim_resizes_after_a_cost_change_and_the_diagnostic_does_not():
    a030, a020 = two_day_account(CM030), two_day_account(CM020)
    assert a030["result_kind"] == a020["result_kind"] == ACCOUNT_SIM
    assert (a030["cost_model_id"], a020["cost_model_id"]) == ("CM030_v1", "CM020_diag_v1")
    d2_030, d2_020 = (first(a["days"][1], "S1") for a in (a030, a020))
    # day 2 is sized from day-2 equity, which differs by the day-1 costs
    assert a030["days"][1]["equity_start"] != a020["days"][1]["equity_start"]
    assert d2_030["notional"] != d2_020["notional"]
    diag = fixed_qty_diagnostic(a030, CM020)
    assert diag["result_kind"] == FIXED_QTY_DIAGNOSTIC and diag["not_an_account_return"] is True
    assert diag["trades"] == 2
    # same quantities as the CM030 account, so its total differs from the CM020 account_sim total
    acct_total = a020["equity_end"] - R.START_EQUITY
    assert diag["total_pnl"] != pytest.approx(acct_total, rel=1e-12)
    with pytest.raises(ValueError):
        fixed_qty_diagnostic(diag, CM030)


def test_cash_never_borrowed_and_nav_stays_positive_on_allowed_inputs():
    """No borrowing: cash after entries stays >= 0. NAV (liquidation value) stays > 0 here; that is a property of
    these inputs and the caps, not a guarantee from the -35% halt (gaps can overshoot it). The halt lifecycle is
    covered by tests/test_daytrade_v13.py::test_account_halt_carries_across_days_in_run_account."""
    crash = lambda: s1_day(qqq_after=lambda t: PREV * 0.97,
                           tqqq_fn=lambda t: tqqq_default(t) if t < T(10, 0) else 30.0)
    same = lambda: s1_day(qqq_after=lambda t: PREV * 1.03)
    for pol in (V10_PLUS, V13):
        acct = run_account([day_input(*crash()), day_input(*same())], pol, CM030)
        traced = [d["account_trace"] for d in acct["days"] if "account_trace" in d]
        assert len(traced) == 2
        for tr in traced:
            assert tr["min_cash"] >= -1e-9
            assert tr["min_nav"] > 0
        assert len(acct["days"][1]["trades"]) == 2  # both entries filled within cash


def test_fixed_qty_diagnostic_reports_a_negative_running_balance():
    fake = {"result_kind": ACCOUNT_SIM, "cost_model_id": "CM030_v1",
            "days": [{"trades": [{"shares": 100.0, "entry_raw": 50.0, "exit_raw": 20.0}]}]}
    diag = fixed_qty_diagnostic(fake, CM030)
    assert diag["went_below_zero"] and diag["min_running_balance"] < 0


# ---------------------------------------------------------------- labels, axes, run records


def test_cost_model_ids():
    assert cost_model_id(BASE_COSTS) == "CM030_v1"
    assert cost_model_id(Costs(R.COMMISSION, R.SEC_FEE, 0.0)) == "CM020_diag_v1"
    assert cost_model_id(BASE_COSTS.scaled(1.5)) == "CM030_v1x1.5"
    assert cost_model_id(Costs(0.002, 0.0, 0.0)).startswith("CUSTOM(")
    assert CM030 == Costs(0.001, 0.0000206, 0.0005)


def test_verdict_axes_never_turn_unmeasured_into_pass():
    v = verdict_axes("ENGINE_VALIDATION", causality={"status": PASS, "reason": "tests"})
    assert v["axes"]["causality"]["status"] == PASS
    assert all(a["status"] == NOT_EVALUATED for k, a in v["axes"].items() if k != "causality")
    assert v["engine_validation_is_not_strategy_validation"] is True
    with pytest.raises(ValueError):
        verdict_axes("ENGINE_VALIDATION", economics={"status": PASS, "reason": "x"})
    with pytest.raises(ValueError):
        verdict_axes("DEV_SELECTED", sample={"status": "OK", "reason": "x"})
    with pytest.raises(ValueError):
        verdict_axes("DEV_SELECTED", sample={"status": PASS})
    with pytest.raises(ValueError):
        verdict_axes("DEV_SELECTED", profit={"status": PASS, "reason": "x"})


def test_run_record_keeps_unknowns_unknown():
    rec = run_record(run_id="r1", commit="abc")
    assert set(rec) == set(RUN_RECORD_FIELDS)
    assert rec["run_id"] == "r1" and rec["family_id"]["value"] is None and rec["family_id"]["unknown_reason"]
    with pytest.raises(ValueError):
        run_record(winner=True)
    assert config_hash(V10_PLUS, CM030, 0) == config_hash(V10_PLUS, CM030, 0)
    assert config_hash(V10_PLUS, CM030, 0) != config_hash(V10_PLUS, CM020, 0)
