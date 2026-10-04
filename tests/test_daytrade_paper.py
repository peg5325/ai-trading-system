"""Exploratory day-trading Paper rules v1.0 (CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1).

Synthetic bars only. No real prices, no network.
"""
from __future__ import annotations

import pytest

from src.daytrade_paper import rules as R
from src.daytrade_paper import store
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    data_quality,
    high_mode,
    preopen_inputs,
)
from src.daytrade_paper.external import (
    events_for,
    fetch_external,
    is_trading_day,
    parse_cboe,
    prev_trading_day,
    prior_value,
)
from src.daytrade_paper.journal import RETRO_HEADINGS, render
from src.daytrade_paper.simulate import (
    buy_fill,
    run_day,
    sell_net,
    size_notional,
    walk_exit,
)
from src.daytrade_paper.situation import (
    classify_open,
    classify_s3,
    classify_s5,
    opening_signals,
)
from src.daytrade_paper.store import (
    RunRefused,
    check_chain,
    commit_outputs,
    load_ledger,
    new_ledger,
    write_once,
)

T = R.hm
NO_EVENTS = {"known": True, "types": [], "fomc": False, "early_close": False, "indicator": False, "items": []}
EXT_OK = {"vix": {"value": 15.0}, "vix9d": {"value": 14.0}, "gex": {"value": 1.0e9}}


def bars(fn, start=R.OPEN, end=R.LAST_BAR + 1, spread=0.0, vol=1000.0):
    """1m bars with open=close=fn(t) and high/low = price ± spread."""
    out = []
    for t in range(start, end):
        p = fn(t)
        out.append(Bar(t, p, p + spread, p - spread, p, vol))
    return out


def prior_sessions(n=60):
    """Rising QQQ history: session i flat at 100+0.1i, bars ±0.5 -> ATR ≈ 1%, BULL."""
    return [PriorSession(f"2026-06-{i:03d}", tuple(bars(lambda t, p=100 + 0.1 * i: p, spread=0.5)))
            for i in range(n)]


PRIOR = prior_sessions()
PREV = PRIOR[-1].close  # 105.9
PREV_DATE = PRIOR[-1].date
PREV_TIMES = {s: set(range(R.OPEN, R.LAST_BAR + 1)) for s in R.SYMBOLS}
QC = {"prev_times": PREV_TIMES, "expected_prev": PREV_DATE}  # prior-session data checks pass


def tqqq_default(t):
    """Climbs 50.0 -> 50.3 in the opening window, then flat (stop ≈ 0.75% below entry)."""
    return 50.0 + 0.02 * min(t - R.OPEN, 15)


def s1_day(qqq_after=None, tqqq_fn=None, qqq_vol=2000.0):
    """QQQ gaps +0.5% and climbs through the opening window with 2x volume -> S1."""
    def q(t):
        if qqq_after is not None and t >= R.DECISION:
            return qqq_after(t)
        return PREV * 1.005 + 0.01 * min(t - R.OPEN, 15)
    qqq = bars(q, spread=0.5, vol=qqq_vol)
    tq = bars(tqqq_fn or tqqq_default, spread=0.05)
    sq = bars(lambda t: 20.0, spread=0.02)
    pre = [Bar(T(9, 0), PREV * 1.005, PREV * 1.005, PREV * 1.005, PREV * 1.005, 10)]
    return {"QQQ": qqq, "TQQQ": tq, "SQQQ": sq}, pre


def inputs_for(regular, pre, external=EXT_OK):
    return preopen_inputs("2026-10-05", PRIOR, pre, regular["QQQ"], external)


# ---------------------------------------------------------------- look-ahead


def test_decision_uses_only_bars_before_decision_time():
    reg_a, pre = s1_day()
    reg_b, _ = s1_day(qqq_after=lambda t: 1.0)  # absurd future prices from 09:45 on
    ia, ib = inputs_for(reg_a, pre), inputs_for(reg_b, pre)
    sa = opening_signals(ia, reg_a["QQQ"], PRIOR, R.DECISION)
    sb = opening_signals(ib, reg_b["QQQ"], PRIOR, R.DECISION)
    assert sa == sb
    assert classify_open(ia, sa)["label"] == classify_open(ib, sb)["label"] == "S1"
    # S3 and S5 likewise ignore bars at/after their decision times
    crashed = [b if b.t < R.S3_DECISION else Bar(b.t, 1, 1, 1, 1, 1e9) for b in reg_a["QQQ"]]
    assert classify_s3(ia, reg_a["QQQ"], R.S3_DECISION) == classify_s3(ia, crashed, R.S3_DECISION)
    q5 = bars(lambda t: PREV * 1.03 if t < R.S5_DECISION else 1.0)
    assert classify_s5(ia, q5)["symbol"] == R.LONG


def test_run_day_entry_unchanged_by_future_bars():
    reg_a, pre = s1_day()
    reg_b, _ = s1_day(qqq_after=lambda t: PREV * 0.9)
    ra = run_day("2026-10-05", reg_a, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    rb = run_day("2026-10-05", reg_b, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert ra["situation"] == rb["situation"] == "S1"
    assert ra["trades"][0]["entry_fill"] == rb["trades"][0]["entry_fill"]
    assert ra["trades"][0]["entry_time"] == "09:45"


# ---------------------------------------------------------------- S1/S2 boundaries


def _sig(c=101.0, o=100.0, v=100.5, rvol=1.5, rng=0.01, rmin=0.004):
    return {"decision_time": "09:45", "last_close": c, "open_0930": o, "vwap": v,
            "rvol": rvol, "range_pct": rng, "range_min_pct": rmin}


def _inp(gap, bull=True):
    return {"gap": gap, "bull": bull}


@pytest.mark.parametrize("gap,bull,expect", [
    (0.003, False, "S1"),      # strong gap, BEAR still ok
    (0.0029, False, None),     # just below, BEAR
    (0.0, True, "S1"),         # BULL and gap >= 0
    (-0.0001, True, None),
])
def test_s1_gap_boundary(gap, bull, expect):
    assert classify_open(_inp(gap, bull), _sig())["label"] == expect


@pytest.mark.parametrize("rvol,rng,expect", [
    (1.2, 0.004, "S1"),
    (1.19, 0.004, None),
    (1.2, 0.0039, None),
])
def test_s1_rvol_and_range_boundary(rvol, rng, expect):
    assert classify_open(_inp(0.005), _sig(rvol=rvol, rng=rng))["label"] == expect


def test_s1_needs_close_above_open_and_vwap():
    assert classify_open(_inp(0.005), _sig(c=100.0, o=100.0))["label"] is None
    assert classify_open(_inp(0.005), _sig(c=101.0, v=101.0))["label"] is None


@pytest.mark.parametrize("gap,bull,expect", [
    (-0.003, True, "S2"),
    (-0.0029, True, None),
    (0.0, False, "S2"),
    (0.0001, False, None),
])
def test_s2_mirror_boundary(gap, bull, expect):
    sig = _sig(c=99.0, o=100.0, v=99.5)
    assert classify_open(_inp(gap, bull), sig)["label"] == expect


# ---------------------------------------------------------------- S3


def _s3_qqq(low_late=99.0, close_last=100.5):
    """Low 98.0 at 09:35, later lows `low_late`, 09:59 close above VWAP."""
    out = []
    for t in range(R.OPEN, T(10, 0)):
        p = 99.5
        lo = 98.0 if t == T(9, 35) else (low_late if t >= T(9, 50) else 99.0)
        c = close_last if t == T(9, 59) else p
        out.append(Bar(t, p, max(p, c), lo, c, 1000))
    return out


def _s3_inputs(gap=-0.01, bull=True, high=False):
    return {"gap": gap, "bull": bull, "high": high, "risk_mode": "HIGH" if high else "NORMAL",
            "prev_close": 101.0, "sma50": 100.0}


def test_s3_boundaries():
    assert classify_s3(_s3_inputs(), _s3_qqq(), R.S3_DECISION)["label"] == "S3"
    assert classify_s3(_s3_inputs(gap=-0.0099), _s3_qqq(), R.S3_DECISION)["label"] is None
    assert classify_s3(_s3_inputs(bull=False), _s3_qqq(), R.S3_DECISION)["label"] is None
    assert classify_s3(_s3_inputs(high=True), _s3_qqq(), R.S3_DECISION)["label"] is None
    # low equal to the early low after 09:50 = broken (not held)
    assert classify_s3(_s3_inputs(), _s3_qqq(low_late=98.0), R.S3_DECISION)["label"] is None
    # 09:55 5m close not above VWAP
    assert classify_s3(_s3_inputs(), _s3_qqq(close_last=99.0), R.S3_DECISION)["label"] is None


def test_s4_when_nothing_fires():
    reg, pre = s1_day(qqq_vol=1000.0)  # RVOL 1.0 -> S1 fails; gap positive -> S3 fails
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situation"] == "S4" and r["trades"] == []
    assert any(x.startswith("S4") for x in r["no_trade_reasons"])


# ---------------------------------------------------------------- S5 / S0


@pytest.mark.parametrize("move,expect", [(0.02, R.LONG), (0.0199, None), (-0.02, R.SHORT), (-0.0199, None)])
def test_s5_boundary(move, expect):
    q = [Bar(R.S5_DECISION - 1, 1, 1, 1, 100.0 * (1 + move), 1)]
    assert classify_s5({"prev_close": 100.0}, q)["symbol"] == expect


def test_s0_fomc_day_has_no_trades_even_with_s5():
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.05)
    ev = {**NO_EVENTS, "fomc": True, "types": ["FOMC"]}
    r = run_day("2026-10-28", reg, pre, PRIOR, EXT_OK, ev, 2000.0, **QC)
    assert r["situations"] == ["S0"] and r["trades"] == []


def test_indicator_day_moves_decision_to_1015():
    reg, pre = s1_day()
    ev = {**NO_EVENTS, "indicator": True, "types": ["CPI"]}
    r = run_day("2026-10-14", reg, pre, PRIOR, EXT_OK, ev, 2000.0, **QC)
    assert r["opening"]["decision_time"] == "10:15"
    assert r["opening"]["opening_window"] == "09:30~10:14"


# ---------------------------------------------------------------- exits


def test_stop_before_exit_in_same_bar():
    # S3 target (QQQ high >= prev close) and TQQQ stop in the same bar -> stop wins
    tq = bars(lambda t: 50.0, start=T(10, 0), end=T(16, 0), spread=0.01)
    tq[5] = Bar(tq[5].t, 50.0, 50.0, 49.0, 49.8, 1000)
    qqq = bars(lambda t: 99.0, start=T(10, 0), end=T(16, 0))
    qqq[5] = Bar(qqq[5].t, 99.0, 101.0, 99.0, 100.5, 1000)
    ex = walk_exit("S3", tq, qqq, T(10, 0), stop=49.5, prev_close=100.0)
    assert ex["reason"] == "STOP" and ex["raw"] == 49.5 and ex["t"] == T(10, 5)


def test_gap_through_stop_fills_at_open():
    tq = bars(lambda t: 50.0, start=T(9, 45), end=T(16, 0), spread=0.01)
    tq[10] = Bar(tq[10].t, 48.0, 48.2, 47.5, 48.1, 1000)  # opens below the 49.5 stop
    ex = walk_exit("S1", tq, tq, T(9, 45), stop=49.5, prev_close=100.0)
    assert ex["reason"] == "STOP_GAP" and ex["raw"] == 48.0


def test_pending_vwap_exit_at_next_open_but_gap_below_stop_is_stop():
    def p(t):
        return 50.0 if t < T(10, 30) else 49.8
    tq = bars(p, start=T(9, 30), end=T(16, 0), spread=0.01)
    # 10:30-10:34 5m close 49.8 < VWAP (~49.9) -> exit at 10:35 open
    ex = walk_exit("S1", tq, tq, T(9, 45), stop=49.0, prev_close=100.0)
    assert ex["reason"] == "VWAP_5M" and ex["t"] == T(10, 35)
    tq2 = list(tq)
    i = T(10, 35) - T(9, 30)
    tq2[i] = Bar(T(10, 35), 48.5, 48.6, 48.4, 48.5, 1000)
    ex2 = walk_exit("S1", tq2, tq2, T(9, 45), stop=49.0, prev_close=100.0)
    assert ex2["reason"] == "STOP_GAP" and ex2["raw"] == 48.5


def test_forced_exit_at_1555_open():
    tq = bars(lambda t: 50.0 + 0.001 * t, start=T(9, 30), end=T(16, 0), spread=0.01)
    ex = walk_exit("S1", tq, tq, T(9, 45), stop=40.0, prev_close=100.0)
    assert ex == {"t": R.FORCED_EXIT, "at": "open", "raw": tq[R.FORCED_EXIT - R.OPEN].open,
                  "reason": "FORCED_1555"}


# ---------------------------------------------------------------- stop cap and sizing


def test_stop_wider_than_1_5pct_means_no_entry():
    def tq(t):
        return 49.0 if t == T(9, 31) else tqqq_default(t)  # opening low 48.95 -> stop ~2.7% away
    reg, pre = s1_day(tqqq_fn=tq)
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situation"] == "S1" and r["trades"] == []
    assert any("1.5% 상한" in x for x in r["no_trade_reasons"])


def test_sizing_cap_and_high_halving():
    assert size_notional(2000, 0.001, False) == pytest.approx(700.0)  # 35% cap binds
    assert size_notional(2000, 0.001, True) == pytest.approx(350.0)
    assert size_notional(2000, 0.015, False) == pytest.approx(2000 * 0.005 / 0.015)
    assert size_notional(2000, 0.015, True) == pytest.approx(2000 * 0.005 / 0.015 / 2)
    assert size_notional(2000, 0.001, False, room=300.0) == pytest.approx(300.0)


def test_unknown_risk_mode_is_treated_as_high():
    assert high_mode(None, None) == ("UNKNOWN_TREATED_AS_HIGH", True)
    assert high_mode(20.0, None) == ("UNKNOWN_TREATED_AS_HIGH", True)
    assert high_mode(30.0, None) == ("HIGH", True)
    assert high_mode(20.0, 22.0) == ("NORMAL", False)
    assert high_mode(20.0, 22.1) == ("HIGH", True)
    reg, pre = s1_day()
    r = run_day("2026-10-05", reg, pre, PRIOR, {}, NO_EVENTS, 2000.0, **QC)
    assert r["trades"][0]["notional"] == pytest.approx(350.0)  # tight stop -> 35% cap, halved


def test_s5_same_direction_combined_cap():
    # S1 long stays open; QQQ +3% at 14:44 -> S5 TQQQ limited by 50% combined cap
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03)
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situations"] == ["S1", "S5"]
    s1, s5 = r["trades"]
    assert s1["notional"] == pytest.approx(700.0)
    assert s5["notional"] == pytest.approx(1000.0 - 700.0)


# ---------------------------------------------------------------- costs and halt


def test_round_trip_cost_is_about_0_3pct():
    notional = 1000.0
    fill = buy_fill(100.0)
    shares = notional / fill
    pnl = sell_net(shares, 100.0)["net"] - notional - notional * R.COMMISSION
    assert pnl / notional == pytest.approx(-0.00302, abs=2e-5)


def test_daily_loss_halt_blocks_s5():
    def tq(t):
        return tqqq_default(t) if t < T(10, 0) else 30.0  # gap through the stop on a 35% position
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03, tqqq_fn=tq)
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert len(r["trades"]) == 1 and r["trades"][0]["exit_reason"] == "STOP_GAP"
    assert r["halt"]["triggered"] is True
    assert r["pnl"]["day_pnl"] < -0.03 * 2000


# ---------------------------------------------------------------- data quality


def test_incomplete_data_makes_no_trades():
    reg, pre = s1_day()
    reg["TQQQ"] = reg["TQQQ"][:-1]  # 15:59 missing
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situation"] == "INCOMPLETE_DATA" and r["trades"] == []
    assert any("15:59" in x for x in r["data_quality"]["issues"])
    reg2, _ = s1_day()
    reg2["QQQ"] = reg2["QQQ"][1:]  # 09:30 missing
    assert not data_quality(reg2, PRIOR, False, R.DECISION, PREV_TIMES, PREV_DATE)["ok"]
    assert not data_quality(s1_day()[0], PRIOR[:10], False, R.DECISION, PREV_TIMES, PREV_DATE)["ok"]  # history too short


# ---------------------------------------------------------------- external, events, ledger, journal


def test_external_prior_day_only():
    rows = parse_cboe("DATE,OPEN,HIGH,LOW,CLOSE\n10/01/2026,1,1,1,16.4\n10/02/2026,1,1,1,15.3\n")
    assert prior_value(rows, "2026-10-02")["value"] == 16.4
    assert prior_value(rows, "2026-10-05")["value"] == 15.3
    assert prior_value(rows, "2026-10-20")["value"] is None  # stale


def test_events_lookup():
    cal = {"version": "v1", "coverage": {"start": "2026-10-01", "end": "2026-12-31"},
           "events": [{"date": "2026-10-28", "type": "FOMC", "source": "x"}]}
    assert events_for(cal, "2026-10-28")["fomc"] is True
    assert events_for(cal, "2026-09-30")["known"] is False


def test_write_once_and_ledger_chain(tmp_path):
    p = tmp_path / "r.json"
    write_once(p, "{}")
    with pytest.raises(RunRefused):
        write_once(p, "{}")
    led = new_ledger()
    assert check_chain(led, "2026-10-05", "2026-10-02") == R.START_EQUITY
    led["days"].append({"date": "2026-10-05", "equity_end": 1990.0})
    assert check_chain(led, "2026-10-06", "2026-10-05") == 1990.0
    with pytest.raises(RunRefused):
        check_chain(led, "2026-10-07", "2026-10-06")  # 10-06 missing from the ledger
    with pytest.raises(RunRefused):
        check_chain(led, "2026-10-05", "2026-10-02")  # repeat


def test_journal_has_facts_and_retro_headings():
    reg, pre = s1_day()
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    md = render(r)
    assert "EXPLORATORY_PAPER" in md and "## 회고 (assistant 작성)" in md
    for h in RETRO_HEADINGS:
        assert f"### {h}" in md
    assert "TQQQ 매수→매도" in md and "QQQ 1배" in md


def test_fetch_failure_is_recorded_as_missing(tmp_path):
    def boom(url):
        raise OSError("offline")
    ext = fetch_external("2026-10-05", tmp_path, download=boom)
    assert all(e["value"] is None and e["status"].startswith("missing") for e in ext.values())
    assert preopen_inputs("2026-10-05", PRIOR, [], s1_day()[0]["QQQ"], ext)["risk_mode"] == \
        "UNKNOWN_TREATED_AS_HIGH"


# ---------------------------------------------------------------- 2026-10-04 review fixes


def test_entry_open_at_or_below_stop_means_no_entry():
    # opening-window low 100.00, 09:45 open 99.98 -> already through the stop -> no trade
    def tq(t):
        return 100.05 if t < R.DECISION else 99.98
    reg, pre = s1_day(tqqq_fn=tq)
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situation"] == "S1" and r["trades"] == []
    assert any("진입 시가가 이미 손절가 아래" in x for x in r["no_trade_reasons"])


CAL = {"version": "v1", "coverage": {"start": "2026-10-01", "end": "2026-12-31"},
       "holidays": [{"date": "2026-11-26"}, {"date": "2026-12-25"}], "events": []}


def test_nyse_calendar_previous_trading_day():
    assert prev_trading_day(CAL, "2026-10-05") == "2026-10-02"  # Monday -> Friday
    assert prev_trading_day(CAL, "2026-11-27") == "2026-11-25"  # skips Thanksgiving
    assert prev_trading_day(CAL, "2026-12-28") == "2026-12-24"  # skips Christmas + weekend
    assert not is_trading_day(CAL, "2026-11-26") and not is_trading_day(CAL, "2026-10-03")
    assert is_trading_day(CAL, "2026-11-27")
    real = store.Path(__file__).resolve().parents[1] / "config" / "daytrade_events_v1.yaml"
    import yaml
    holidays = {str(h["date"]) for h in yaml.safe_load(real.read_text())["holidays"]}
    assert holidays == {"2026-11-26", "2026-12-25"}


def test_missing_collector_day_is_incomplete_and_breaks_the_chain():
    # the calendar says the previous trading day is later than the DB's last session
    q = data_quality(s1_day()[0], PRIOR, False, R.DECISION, PREV_TIMES, "2026-10-02")
    assert not q["ok"] and any("전 거래일 2026-10-02" in x for x in q["issues"])
    reg, pre = s1_day()
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0,
                prev_times=PREV_TIMES, expected_prev="2026-10-02")
    assert r["situation"] == "INCOMPLETE_DATA" and r["trades"] == []
    led = new_ledger()
    led["days"].append({"date": "2026-10-01", "equity_end": 2000.0})
    with pytest.raises(RunRefused):  # 10-02 never ran -> 10-05 refused
        check_chain(led, "2026-10-05", prev_trading_day(CAL, "2026-10-05"))
    led["days"].append({"date": "2026-11-25", "equity_end": 2010.0})
    assert check_chain(led, "2026-11-27", prev_trading_day(CAL, "2026-11-27")) == 2010.0


def test_previous_session_needs_final_bar_for_every_symbol():
    reg = s1_day()[0]
    pt = {**PREV_TIMES, "SQQQ": set(range(R.OPEN, R.LAST_BAR))}  # SQQQ 15:59 missing
    q = data_quality(reg, PRIOR, False, R.DECISION, pt, PREV_DATE)
    assert not q["ok"] and any("SQQQ" in x and "15:59" in x for x in q["issues"])
    early = {s: set(range(R.OPEN, R.EARLY_CLOSE_LAST_BAR + 1)) for s in R.SYMBOLS}
    assert data_quality(reg, PRIOR, False, R.DECISION, early, PREV_DATE, prev_early_close=True)["ok"]
    assert not data_quality(reg, PRIOR, False, R.DECISION, early, PREV_DATE)["ok"]


def test_rvol_baseline_sessions_need_complete_opening_window():
    reg = s1_day()[0]
    holed = list(PRIOR)
    s = holed[-3]
    holed[-3] = PriorSession(s.date, tuple(b for b in s.bars if b.t != T(9, 40)))
    q = data_quality(reg, holed, False, R.DECISION, PREV_TIMES, PREV_DATE)
    assert not q["ok"] and any("RVOL 기준 세션" in x and "09:40" in x for x in q["issues"])
    late = list(PRIOR)
    late[-3] = PriorSession(s.date, tuple(b for b in s.bars if b.t != T(10, 14)))
    assert data_quality(reg, late, False, R.DECISION, PREV_TIMES, PREV_DATE)["ok"]
    assert not data_quality(reg, late, False, R.DECISION_INDICATOR_DAY, PREV_TIMES, PREV_DATE)["ok"]
    # older than the 20-session baseline: not checked
    old = list(PRIOR)
    old[-25] = PriorSession(old[-25].date, tuple(b for b in old[-25].bars if b.t != T(9, 40)))
    assert data_quality(reg, old, False, R.DECISION, PREV_TIMES, PREV_DATE)["ok"]


def test_halt_recorded_even_without_s5():
    def tq(t):
        return tqqq_default(t) if t < T(10, 0) else 30.0  # gap through the stop, no S5 move
    reg, pre = s1_day(tqqq_fn=tq)
    r = run_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert r["situations"] == ["S1"]
    assert r["halt"]["triggered"] is True and r["halt"]["s5_signal"] is False
    assert r["halt"]["reached_by_close"] is True
    assert any("도달" in x for x in r["no_trade_reasons"])
    assert "일일 손실 −3% 도달" in render(r)
    ok = run_day("2026-10-05", *s1_day(), PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert ok["halt"]["triggered"] is False


def test_commit_outputs_cleans_up_on_failure(tmp_path, monkeypatch):
    rp, jp, lp = tmp_path / "r" / "d.json", tmp_path / "j" / "d.md", tmp_path / "r" / "ledger.json"

    def boom(path, ledger):
        raise OSError("disk full")
    monkeypatch.setattr(store, "write_ledger", boom)
    with pytest.raises(OSError):
        commit_outputs(rp, "{}", jp, "# j", lp, new_ledger(), {"date": "2026-10-05"})
    assert not rp.exists() and not jp.exists() and not lp.exists()
    monkeypatch.undo()
    commit_outputs(rp, "{}", jp, "# j", lp, new_ledger(), {"date": "2026-10-05"})  # rerun works
    assert rp.exists() and jp.exists() and len(load_ledger(lp)["days"]) == 1
    # an existing journal is someone else's: refuse, remove only our new report
    rp2 = tmp_path / "r" / "e.json"
    with pytest.raises(RunRefused):
        commit_outputs(rp2, "{}", jp, "# j", lp, new_ledger(), {"date": "2026-10-06"})
    assert not rp2.exists() and jp.read_text() == "# j"


def _script():
    import importlib.util
    path = store.Path(__file__).resolve().parents[1] / "scripts" / "run_daytrade_paper.py"
    spec = importlib.util.spec_from_file_location("run_daytrade_paper", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_run_with_incomplete_data_refuses_and_writes_nothing(tmp_path, capsys):
    import sqlite3
    db = tmp_path / "h.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE candle_history (symbol TEXT, interval TEXT, ts_utc TEXT, "
                "ts_exchange TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    con.executemany("INSERT INTO candle_history VALUES ('QQQ','1m',?,?,'REGULAR',1,1,1,1,1)",
                    [(f"2026-10-05T13:{m:02d}:00+00:00", f"2026-10-05T09:{m:02d}:00-04:00")
                     for m in range(30, 40)])
    con.commit()
    con.close()
    out = tmp_path / "out"
    rc = _script().main(["--date", "2026-10-05", "--db", str(db), "--out-root", str(out)])
    assert rc == 3 and not out.exists()
    assert "--accept-incomplete" in capsys.readouterr().err
    assert _script().main(["--date", "2026-10-03", "--db", str(db), "--out-root", str(out)]) == 2  # Saturday


# ---------------------------------------------------------------- --auto (launchd catch-up)

def _cal():
    from src.daytrade_paper.external import load_events
    return load_events(store.Path(__file__).resolve().parents[1] / "config" / "daytrade_events_v1.yaml")


def _utc(y, mo, d, h, mi):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def _receipt(root, day, status="COMPLETE"):
    import json
    folder = root / "reports" / "history-collection"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day.replace('-', '')}.operation.json").write_text(
        json.dumps({"status": status, "trading_day": day}))


def test_latest_closed_session_from_the_1107_kst_slot():
    s, cal = _script(), _cal()
    # Tue 2026-10-06 11:07 KST = Mon 22:07 EDT -> Monday's session
    assert s.latest_closed_session(cal, _utc(2026, 10, 6, 2, 7)) == "2026-10-05"
    # Mon 11:07 KST = Sun 22:07 EDT -> Friday
    assert s.latest_closed_session(cal, _utc(2026, 10, 5, 2, 7)) == "2026-10-02"
    # less than an hour after the bell -> the previous session
    assert s.latest_closed_session(cal, _utc(2026, 10, 5, 20, 30)) == "2026-10-02"
    assert s.latest_closed_session(cal, _utc(2026, 10, 5, 21, 0)) == "2026-10-05"


def test_pending_sessions_start_at_paper_start_and_catch_up_gaps():
    s, cal = _script(), _cal()
    assert s.pending_sessions(cal, new_ledger(), "2026-10-02") == []
    assert s.pending_sessions(cal, new_ledger(), "2026-10-07") == ["2026-10-05", "2026-10-06", "2026-10-07"]
    ledger = {**new_ledger(), "days": [{"date": "2026-10-05"}]}
    assert s.pending_sessions(cal, ledger, "2026-10-12") == [
        "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12"]
    ledger = {**new_ledger(), "days": [{"date": "2026-10-09"}]}
    assert s.pending_sessions(cal, ledger, "2026-10-09") == []


def test_auto_waits_for_collection_receipt_and_runs_in_order(tmp_path, monkeypatch, capsys):
    s = _script()
    ran: list[str] = []
    monkeypatch.setattr(s, "run_session", lambda day, args: ran.append(day) or 0)
    now = _utc(2026, 10, 7, 2, 7)  # Tue 22:07 EDT -> pending 10-05, 10-06
    _receipt(tmp_path, "2026-10-05")
    _receipt(tmp_path, "2026-10-06", "INCOMPLETE")
    assert s.main(["--auto", "--out-root", str(tmp_path)], now=now) == 3
    assert ran == ["2026-10-05"]
    assert "WAITING" in capsys.readouterr().err
    _receipt(tmp_path, "2026-10-06")
    ran.clear()
    assert s.main(["--auto", "--out-root", str(tmp_path)], now=now) == 0
    assert ran == ["2026-10-05", "2026-10-06"]  # the stub never writes the ledger


def test_auto_stops_at_first_failed_session_and_refuses_accept_incomplete(tmp_path, monkeypatch):
    s = _script()
    ran: list[str] = []
    monkeypatch.setattr(s, "run_session", lambda day, args: ran.append(day) or 3)
    _receipt(tmp_path, "2026-10-05")
    _receipt(tmp_path, "2026-10-06")
    assert s.main(["--auto", "--out-root", str(tmp_path)], now=_utc(2026, 10, 7, 2, 7)) == 3
    assert ran == ["2026-10-05"]
    with pytest.raises(SystemExit):
        s.main(["--auto", "--accept-incomplete", "--out-root", str(tmp_path)])
