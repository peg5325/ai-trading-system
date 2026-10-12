"""Entry study v1 unit tests (PREREG_DAYTRADE_ENTRY_STUDY_V1 §11): config, bars, rules, costs, stats. Synthetic data only."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from src.daytrade_paper.core import Bar
from src.entry_study_v1 import rules as RL
from src.entry_study_v1 import stats as ST
from src.entry_study_v1 import trades as TR
from src.entry_study_v1.bars import InvalidRun, candle_bar
from src.entry_study_v1.config import CONFIG_PATH, hhmm, load_cells, load_config
from src.history import exchange_calendar as xcal
from src.models import ET, Candle

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "research" / "PREREG_DAYTRADE_ENTRY_STUDY_V1.md"
CELLS = load_cells()
PARAMS = {r: next(c.params for c in CELLS if c.rule == r) for r in ("E1", "E2", "E3", "E4", "E5")}


def t(s: str) -> int:
    return hhmm(s)


# ------------------------------------------------------------------ config vs the document table


def test_config_matches_document_tables():
    doc = PREREG.read_text(encoding="utf-8")
    pairs = re.findall(r"^\| (P\d) \| (\w+) \| (\w+) \| (\w+) \|", doc, flags=re.MULTILINE)
    assert [p[0] for p in pairs] == ["P1", "P2", "P3"]
    assert pairs == [("P1", "SMH", "SOXL", "SOXS"), ("P2", "QQQ", "TQQQ", "SQQQ"), ("P3", "SPY", "SPXL", "SPXS")]
    assert len(CELLS) == 30
    expect_order = [(p, r, s) for p, *_ in pairs for r in ("E1", "E2", "E3", "E4", "E5") for s in ("long", "short")]
    assert [(c.pair, c.rule, c.side) for c in CELLS] == expect_order
    by_pair = {p[0]: p for p in pairs}
    for c in CELLS:
        _, ref, long_etf, short_etf = by_pair[c.pair]
        assert c.ref == ref
        assert c.etf == (long_etf if c.side == "long" else short_etf)
        assert c.strategy_id == f"entry-study-v1-cell-{c.k}"
        assert c.holds == {"30m": 30, "60m": 60, "close": "15:55"}
        assert c.primary_hold == "close"
    assert [c.k for c in CELLS] == list(range(30))
    # rule parameters, copied by hand from the §3 table
    assert PARAMS["E1"] == {"signal_bar_start": "09:34", "entry": "09:35", "compare": "close_vs_open",
                            "equal": "no_trade"}
    assert PARAMS["E2"] == {"gap_min_abs": 0.0030, "pre_cutoff": "09:25", "flow_bar_start": "09:44",
                            "entry": "09:45", "flow": "close_minus_open"}
    assert PARAMS["E3"] == {"sigma_sessions": 14, "multiplier": 1.0, "first_check_bar_start": "10:00",
                            "last_check_bar_start": "15:00", "entry": "next_minute_open"}
    assert PARAMS["E4"] == {"signal_bar_start": "09:59", "entry": "15:30"}
    assert PARAMS["E5"] == {"signal_bar_start": "14:44", "entry": "14:45", "threshold": 0.015,
                            "sensitivity_thresholds": [0.010, 0.020]}
    assert load_config(CONFIG_PATH)["ranking"] == {
        "max_cells": 2, "order": ["holm_adjusted_p asc", "mean_net_base_close desc", "n desc", "cell_index asc"]}
    for needle in ("`C(09:34)` vs `O`", "0.0030", "배수 1.0", "`m ≥ +0.015`", "20261012 + k", "ceil(n_s / 5)"):
        assert needle in doc  # the numbers the config / code mirror are still in the document


def test_gap_threshold_is_the_paper_constant():
    from src.daytrade_paper import rules as PR
    assert PARAMS["E2"]["gap_min_abs"] == PR.GAP_STRONG


def test_package_does_not_use_forbidden_loaders():
    for path in (ROOT / "src" / "entry_study_v1").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "daytrade_v11" not in text, path
        assert "load_candles" not in text, path


# ------------------------------------------------------------------ bar labels


def _candle(label_hm: str, day=(2025, 3, 11), session="IGNORED") -> Candle:
    hh, mm = label_hm.split(":")
    c = Candle.from_moment("QQQ", "1m", datetime(*day, int(hh), int(mm), tzinfo=ET), 1, 1, 1, 1, 1, "USD", True, "US")
    return Candle(**{**c.__dict__, "session": session})


def test_bar_label_conversion():
    day, sess, bar = candle_bar(_candle("09:31"))
    assert (day, sess, bar.t) == ("2025-03-11", "REGULAR", t("09:30"))
    day, sess, bar = candle_bar(_candle("16:00", session="POST"))  # stored as POST, bar starts 15:59
    assert (sess, bar.t) == ("REGULAR", t("15:59"))
    assert candle_bar(_candle("09:30"))[1] == "PRE" and candle_bar(_candle("09:30"))[2].t == t("09:29")
    assert candle_bar(_candle("16:01"))[1] == "POST"
    early = (2025, 11, 28)
    assert xcal.is_early_close(date(*early))
    assert candle_bar(_candle("13:00", early))[1:] == ("REGULAR", candle_bar(_candle("13:00", early))[2])
    assert candle_bar(_candle("13:00", early))[2].t == t("12:59")
    assert candle_bar(_candle("13:01", early))[1] == "POST"


# ------------------------------------------------------------------ synthetic days


def mk(tt: int, o: float, c: float | None = None) -> Bar:
    c = o if c is None else c
    return Bar(tt, o, max(o, c) + 0.01, min(o, c) - 0.01, c, 1000.0)


def flat_bars(price=100.0) -> dict[int, Bar]:
    return {m: mk(m, price) for m in range(t("09:30"), t("16:00"))}


def sigma_days(sigma=0.01, n=14) -> tuple[dict[int, Bar], ...]:
    """Prior sessions where |C_d(t) / O_d - 1| = sigma at every minute."""
    out = []
    for _ in range(n):
        d = flat_bars(100.0)
        for m in d:
            d[m] = mk(m, 100.0, 100.0 * (1 + sigma))
        d[t("09:30")] = mk(t("09:30"), 100.0, 100.0 * (1 + sigma))
        out.append(d)
    return tuple(out)


def ref_day(bars=None, pre=(), pc=100.0, sigma=0.01) -> RL.RefDay:
    return RL.RefDay("2025-03-11", bars if bars is not None else flat_bars(), tuple(pre), "2025-03-10",
                     mk(t("15:59"), pc), sigma_days(sigma))


def with_bar(bars: dict[int, Bar], tt: int, o: float, c: float | None = None) -> dict[int, Bar]:
    return {**bars, tt: mk(tt, o, c)}


# ------------------------------------------------------------------ E1


def test_e1_long_short_equal():
    base = flat_bars()
    up = RL.e1(ref_day(with_bar(base, t("09:34"), 100, 100.5)), PARAMS["E1"])
    assert up.signal.side == "long" and up.signal.entry_t == t("09:35")
    dn = RL.e1(ref_day(with_bar(base, t("09:34"), 100, 99.5)), PARAMS["E1"])
    assert dn.signal.side == "short" and dn.signal.entry_t == t("09:35")
    eq = RL.e1(ref_day(base), PARAMS["E1"])
    assert eq.signal is None and eq.reason == "equal"


# ------------------------------------------------------------------ E2


def test_e2_signal_and_boundaries():
    p = PARAMS["E2"]
    pre = [mk(t("08:00"), 100.5)]
    bars = with_bar(flat_bars(), t("09:44"), 100, 100.2)  # f > 0
    res = RL.e2(ref_day(bars, pre), p)
    assert res.signal.side == "long" and res.signal.entry_t == t("09:45") and not res.info["pre_substituted"]
    # short: gap down and flow down
    res = RL.e2(ref_day(with_bar(flat_bars(), t("09:44"), 100, 99.8), [mk(t("08:00"), 99.5)]), p)
    assert res.signal.side == "short"
    # flow disagrees / f == 0
    assert RL.e2(ref_day(with_bar(flat_bars(), t("09:44"), 100, 99.8), pre), p).signal is None
    zero = RL.e2(ref_day(flat_bars(), pre), p)
    assert zero.signal is None and zero.reason == "flow_disagrees_or_zero"
    # no PRE bar before 09:25: the 09:30 open stands in, and it is counted
    sub = RL.e2(ref_day(with_bar(flat_bars(), t("09:30"), 100.5, 100.5), []), p)
    assert sub.info["pre_substituted"] and sub.info["gap_source"].startswith("09:30")
    # a PRE bar at or after 09:25 is not used
    late = RL.e2(ref_day(bars, [mk(t("09:25"), 100.5)]), p)
    assert late.info["pre_substituted"]


def test_e2_gap_threshold_is_inclusive_in_float():
    bars = with_bar(flat_bars(), t("09:44"), 100, 100.2)
    day = ref_day(bars, [mk(t("08:00"), 100.3)])
    g = RL.gap_of(day, RL.visible(bars, t("09:45")), PARAMS["E2"])[0]
    assert RL.e2(day, {**PARAMS["E2"], "gap_min_abs": g}).signal.side == "long"  # |g| == threshold exactly
    assert RL.e2(day, {**PARAMS["E2"], "gap_min_abs": math.nextafter(g, 1)}).signal is None
    # no rounding: 100.3 / 100 - 1 is 0.0029999999999998916 in float64, below 0.0030, so no trade
    assert g < PARAMS["E2"]["gap_min_abs"] and RL.e2(day, PARAMS["E2"]).signal is None
    assert RL.e2(ref_day(bars, [mk(t("08:00"), 100.31)]), PARAMS["E2"]).signal is not None


# ------------------------------------------------------------------ E3


def upper(sig: float, o=100.0, pc=100.0) -> float:
    return max(o, pc) * (1 + sig)


def test_e3_breakout_and_boundaries():
    p = PARAMS["E3"]
    sig = RL.sigma_at(sigma_days(0.01), t("10:00"), 14)
    assert sig == pytest.approx(0.01)
    u = upper(sig)
    # C(10:00) > U -> long, fill at 10:01
    res = RL.e3(ref_day(with_bar(flat_bars(), t("10:00"), 100, u + 0.5)), p)
    assert res.signal.side == "long" and res.signal.entry_t == t("10:01")
    # C == U is inside
    assert RL.e3(ref_day(with_bar(flat_bars(), t("10:00"), 100, u)), p).signal is None
    # short side
    lo = 100 * (1 - sig)
    res = RL.e3(ref_day(with_bar(flat_bars(), t("10:00"), 100, lo - 0.5)), p)
    assert res.signal.side == "short"
    # first breakout wins; later opposite breakouts do not matter
    bars = with_bar(with_bar(flat_bars(), t("10:05"), 100, u + 1), t("10:07"), 100, lo - 1)
    assert RL.e3(ref_day(bars), p).signal.entry_t == t("10:06")
    # 15:00 is the last check (fill 15:01); a breakout at 15:01 is not looked at
    assert RL.e3(ref_day(with_bar(flat_bars(), t("15:00"), 100, u + 1)), p).signal.entry_t == t("15:01")
    assert RL.e3(ref_day(with_bar(flat_bars(), t("15:01"), 100, u + 1)), p).signal is None
    # breakout before 10:00 is ignored
    assert RL.e3(ref_day(with_bar(flat_bars(), t("09:59"), 100, u + 1)), p).signal is None
    # bands use max/min(O, PC): PC above O raises the upper band
    assert RL.e3(ref_day(with_bar(flat_bars(), t("10:00"), 100, u + 0.5), pc=105.0), p).signal is None


def test_e3_sigma_undefined_minute_is_skipped_and_counted():
    p = PARAMS["E3"]
    sdays = list(sigma_days(0.01))
    del sdays[3][t("10:00")]  # one of the 14 sessions lacks bar 10:00
    u = upper(0.01)
    bars = with_bar(with_bar(flat_bars(), t("10:00"), 100, u + 1), t("10:01"), 100, u + 1)
    day = RL.RefDay("2025-03-11", bars, (), "2025-03-10", mk(t("15:59"), 100.0), tuple(sdays))
    res = RL.e3(day, p)
    assert res.signal.entry_t == t("10:02") and res.info["sigma_undefined"] == 1  # 10:00 skipped, 10:01 fires
    assert RL.sigma_undefined_minutes(day, p) == [t("10:00")]
    # a session without its 09:30 bar makes sigma undefined for every check minute
    no_open = list(sigma_days(0.01))
    del no_open[0][t("09:30")]
    day2 = RL.RefDay("2025-03-11", bars, (), "2025-03-10", mk(t("15:59"), 100.0), tuple(no_open))
    res2 = RL.e3(day2, p)
    assert res2.signal is None and res2.info["sigma_undefined"] == 301


def test_sigma_uses_prior_sessions_only():
    day = ref_day(with_bar(flat_bars(), t("10:00"), 100, 150))
    other = ref_day(with_bar(flat_bars(), t("10:00"), 100, 50))
    assert RL.sigma_at(day.sigma_days, t("10:00"), 14) == RL.sigma_at(other.sigma_days, t("10:00"), 14)
    with pytest.raises(InvalidRun):
        RL.sigma_at(day.sigma_days[:13], t("10:00"), 14)


# ------------------------------------------------------------------ E4 / E5


def test_e4_sign_and_zero():
    p = PARAMS["E4"]
    assert RL.e4(ref_day(with_bar(flat_bars(), t("09:59"), 100, 100.1)), p).signal.side == "long"
    assert RL.e4(ref_day(with_bar(flat_bars(), t("09:59"), 100, 99.9)), p).signal.side == "short"
    zero = RL.e4(ref_day(flat_bars()), p)  # C(09:59) == PC -> r1 == 0
    assert zero.signal is None and zero.reason == "zero"
    assert RL.e4(ref_day(with_bar(flat_bars(), t("09:59"), 100, 100.1)), p).signal.entry_t == t("15:30")


def test_e5_threshold_boundaries():
    p = PARAMS["E5"]
    c = 101.5
    day = ref_day(with_bar(flat_bars(), t("14:44"), 101, c))
    m = c / 100.0 - 1
    assert RL.e5(day, p, threshold=m).signal.side == "long"  # m == +threshold exactly
    assert RL.e5(day, p, threshold=math.nextafter(m, 1)).signal is None
    dn = ref_day(with_bar(flat_bars(), t("14:44"), 99, 98.5))
    m2 = 98.5 / 100.0 - 1
    assert RL.e5(dn, p, threshold=-m2).signal.side == "short"  # m == -threshold exactly
    assert RL.e5(dn, p, threshold=math.nextafter(-m2, 1e9)).signal is None
    assert RL.e5(ref_day(with_bar(flat_bars(), t("14:44"), 101, 101.6)), p).signal.entry_t == t("14:45")
    assert RL.e5(day, p).signal is None  # 101.5 / 100 - 1 is 0.014999999999999902 in float64, below 0.015
    # 1.0% / 2.0% sensitivity
    assert RL.e5(day, p, threshold=0.010).signal is not None
    assert RL.e5(day, p, threshold=0.020).signal is None


# ------------------------------------------------------------------ look-ahead


def _scenarios():
    u = upper(0.01)
    return {
        "E1": [with_bar(flat_bars(), t("09:34"), 100, 100.5), flat_bars()],
        "E2": [with_bar(flat_bars(), t("09:44"), 100, 100.2)],
        "E3": [with_bar(flat_bars(), t("10:30"), 100, u + 1), flat_bars()],
        "E4": [with_bar(flat_bars(), t("09:59"), 100, 100.1)],
        "E5": [with_bar(flat_bars(), t("14:44"), 101, 101.6), flat_bars()],
    }


@pytest.mark.parametrize("rule", ["E1", "E2", "E3", "E4", "E5"])
def test_no_lookahead(rule):
    """Replacing every bar that starts at or after the decision time T leaves the result unchanged."""
    p = PARAMS[rule]
    pre = [mk(t("08:00"), 100.5)]
    for bars in _scenarios()[rule]:
        day = ref_day(bars, pre)
        full = RL.evaluate(rule, day, p)
        cut = RL.decision_end(rule, p)
        if full.signal is not None:
            assert RL.decision_end(rule, p) >= full.signal.entry_t if rule == "E3" else True
            cut = full.signal.entry_t
        garbage = {m: (mk(m, 1e6, 1e6) if m >= cut else b) for m, b in bars.items()}
        truncated = {m: b for m, b in bars.items() if m < cut}
        for other in (garbage, truncated):
            res = RL.evaluate(rule, ref_day(other, pre), p)
            assert res.signal == full.signal
            assert res.reason == full.reason


def test_e3_decision_at_t_does_not_see_later_bars():
    p = PARAMS["E3"]
    u = upper(0.01)
    bars = with_bar(flat_bars(), t("10:00"), 100, u + 1)
    for later in (t("10:01"), t("12:00"), t("15:59")):
        altered = with_bar(bars, later, 1e6, 1e6)
        assert RL.e3(ref_day(altered), p).signal == RL.e3(ref_day(bars), p).signal


# ------------------------------------------------------------------ costs, horizons, MAE


def test_cost_round_trip_flat():
    assert round(TR.COSTS["base"].round_trip_flat() * 100, 3) == -0.302
    assert round(TR.COSTS["stress"].round_trip_flat() * 100, 3) == -0.453
    assert round(TR.COSTS["fee_only_0.20"].round_trip_flat() * 100, 3) == -0.202
    assert round(TR.COSTS["fee_only_0.10"].round_trip_flat() * 100, 3) == -0.102
    c = TR.COSTS["stress"]
    assert (c.c, c.s, c.f) == pytest.approx((0.0015, 0.00075, 0.0000309))
    b = TR.COSTS["base"]
    assert (b.c, b.s, b.f) == pytest.approx((0.001, 0.0005, 0.0000206))


def test_cost_hand_examples():
    base, stress = TR.COSTS["base"], TR.COSTS["stress"]
    assert TR.net_return(100, 101, base) == pytest.approx(0.0069607290, abs=1e-9)
    assert TR.net_return(100, 90, base) == pytest.approx(-0.1028171721, abs=1e-9)
    assert TR.net_return(100, 101, stress) == pytest.approx(0.0054422436, abs=1e-9)


def test_sec_fee_applies_to_the_sell_only():
    with_fee = TR.CostSet("a", 0.001, 0.0005, 0.0000206)
    without = TR.CostSet("b", 0.001, 0.0005, 0.0)
    pe, px = 100 * 1.0005, 105 * 0.9995
    diff = TR.net_return(100, 105, with_fee) - TR.net_return(100, 105, without)
    assert diff == pytest.approx(-0.0000206 * px / pe, abs=1e-12)  # proportional to the sell amount only


def test_horizon_caps():
    forced = t("15:55")
    cell = CELLS[0]
    exits = lambda e: TR.hold_exits(cell, e)
    assert exits(t("09:35")) == {"30m": t("10:05"), "60m": t("10:35"), "close": forced}
    assert exits(t("09:45")) == {"30m": t("10:15"), "60m": t("10:45"), "close": forced}
    assert exits(t("14:45")) == {"30m": t("15:15"), "60m": t("15:45"), "close": forced}
    assert exits(t("14:56")) == {"30m": t("15:26"), "60m": forced, "close": forced}  # E3 e >= 14:56: 60m = close
    assert exits(t("14:55")) == {"30m": t("15:25"), "60m": forced, "close": forced}
    assert exits(t("14:54"))["60m"] == t("15:54")
    assert exits(t("15:01")) == {"30m": t("15:31"), "60m": forced, "close": forced}
    assert exits(t("15:30")) == {"30m": forced, "60m": forced, "close": forced}  # E4: all three equal


def test_trade_fills_and_mae():
    cell = next(c for c in CELLS if c.rule == "E1" and c.side == "long")
    bars = {m: Bar(m, 50.0, 50.5, 49.5, 50.0, 10.0) for m in range(t("09:30"), t("16:00"))}
    bars[t("09:35")] = Bar(t("09:35"), 50.0, 51.0, 49.0, 50.2, 10.0)
    bars[t("10:05")] = Bar(t("10:05"), 51.0, 51.5, 50.8, 51.2, 10.0)
    res = TR.trade(cell, t("09:35"), bars, "2025-03-11")
    assert res["30m"]["exit_t"] == t("10:05")
    assert res["30m"]["r"]["base"] == pytest.approx(TR.net_return(50.0, 51.0, TR.COSTS["base"]))
    # MAE = min(lows of bars 09:35..10:04, open at exit) / open_e - 1 = 49.0 / 50 - 1
    assert res["30m"]["mae"] == pytest.approx(49.0 / 50.0 - 1)
    assert res["close"]["exit_t"] == t("15:55")
    with pytest.raises(InvalidRun):
        TR.trade(cell, t("09:35"), {k: v for k, v in bars.items() if k != t("12:00")}, "2025-03-11")


# ------------------------------------------------------------------ statistics


def _indep_bootstrap_p(x, seg, seed, b_count):
    """Direct transcription of the §7.2 pseudo-code."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 2 or np.std(x, ddof=1) == 0:
        return None
    parts = {}
    for v, s in zip(x, seg):
        parts.setdefault(s, []).append(v)
    tested = [np.array(parts[s]) for s in ("A", "B") if s in parts and len(parts[s]) > 5]
    if not tested:
        return None
    m_obs = np.mean(np.concatenate(tested))
    rng = np.random.default_rng(seed)
    stars = []
    for _ in range(b_count):
        pieces = []
        for xs in tested:
            n_s = len(xs)
            starts = rng.integers(0, n_s, size=-(-n_s // 5))
            pieces.append(np.array([xs[(st + j) % n_s] for st in starts for j in range(5)])[:n_s])
        stars.append(np.mean(np.concatenate(pieces)))
    stars = np.array(stars)
    if np.var(stars, ddof=1) == 0:
        return None
    return (1 + np.sum(stars - m_obs >= m_obs)) / (b_count + 1)


def _toy():
    rng = np.random.default_rng(7)
    x = list(rng.normal(0.004, 0.02, 40))
    return x, ["A"] * 17 + ["B"] * 23


def test_bootstrap_matches_pseudocode_and_is_reproducible():
    x, seg = _toy()
    for seed in (20261012, 20261012 + 1000 * 3 + 7):
        got = ST.block_bootstrap_p(x, seg, seed, 400)
        assert got == _indep_bootstrap_p(x, seg, seed, 400)
        assert got == ST.block_bootstrap_p(x, seg, seed, 400)
    assert 0 < ST.block_bootstrap_p(x, seg, 20261012, 400) <= 1
    assert ST.seed_for(0, 5) == 20261017 and ST.seed_for(50, 3) == 20311015


def test_bootstrap_direction():
    seg = ["A"] * 30
    rng = np.random.default_rng(1)
    strong = list(rng.normal(0.02, 0.005, 30))
    assert ST.block_bootstrap_p(strong, seg, 1, 500) < 0.01
    assert ST.block_bootstrap_p([-v for v in strong], seg, 1, 500) > 0.9


def test_bootstrap_small_segment_dropped_and_none_cases():
    rng = np.random.default_rng(3)
    xb = list(rng.normal(0.01, 0.02, 20))
    xa = list(rng.normal(-0.5, 0.02, 5))  # n_A == 5 -> out of the test only
    p_mixed = ST.block_bootstrap_p(xa + xb, ["A"] * 5 + ["B"] * 20, 99, 300)
    assert p_mixed == ST.block_bootstrap_p(xb, ["B"] * 20, 99, 300)
    assert ST.block_bootstrap_p(xa, ["A"] * 5, 99, 300) is None  # nothing testable
    assert ST.block_bootstrap_p([0.01] * 10, ["A"] * 10, 1, 100) is None  # zero variance
    assert ST.block_bootstrap_p([0.01], ["A"], 1, 100) is None  # n < 2
    s = ST.summarize(xa + xb, ["A"] * 5 + ["B"] * 20)
    assert s["n_by_segment"] == {"A": 5, "B": 20}  # excluded segment is still reported


def test_holm_hand_examples():
    adj = ST.holm_adjust({0: 0.01, 1: 0.04, 2: 0.03}, 3)
    assert adj == pytest.approx({0: 0.03, 2: 0.06, 1: 0.06})  # (3*.01, 2*.03, max(.04, .06))
    adj = ST.holm_adjust({0: 0.01, 1: None, 2: 0.02, 3: 0.5}, 4)  # None counts in K, adjusted p = 1
    assert adj == pytest.approx({0: 0.04, 2: 0.06, 3: 1.0, 1: 1.0})
    assert ST.holm_adjust({0: 0.02, 1: 0.02}, 2) == pytest.approx({0: 0.04, 1: 0.04})  # ties by cell index
    assert ST.holm_adjust({5: 0.5, 7: 0.9}, 2) == {5: 1.0, 7: 1.0}  # capped at 1
    with pytest.raises(ValueError):
        ST.holm_adjust({0: 0.1}, 10)


def test_dev_pass_boundaries():
    ok = {"n": 30, "holm_p": 0.0499, "mean_a": 0.001, "mean_b": 0.001, "mean_stress": 0.0001}
    assert ST.dev_pass(**ok)["pass"]
    assert not ST.dev_pass(**{**ok, "holm_p": 0.05})["pass"]
    assert not ST.dev_pass(**{**ok, "holm_p": None})["pass"]
    assert not ST.dev_pass(**{**ok, "n": 29})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_a": 0.0})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_b": None})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_stress": 0.0})["pass"]


def test_oos_selection_and_verdict():
    passing = [{"k": 4, "holm_p": 0.01, "mean": 0.002, "n": 50}, {"k": 2, "holm_p": 0.01, "mean": 0.003, "n": 40},
               {"k": 9, "holm_p": 0.01, "mean": 0.003, "n": 60}, {"k": 1, "holm_p": 0.02, "mean": 0.9, "n": 99}]
    assert ST.rank_dev_pass(passing) == [9, 2]  # p asc, mean desc, n desc, index asc; max 2
    assert ST.rank_dev_pass([]) == []
    assert ST.oos_verdict(9, 0.1, 0.001) == "OOS_INCONCLUSIVE"
    assert ST.oos_verdict(10, 0.1, 0.0999) == "OOS_PASS"
    assert ST.oos_verdict(10, 0.1, 0.10) == "OOS_FAIL"
    assert ST.oos_verdict(10, 0.0, 0.01) == "OOS_FAIL"
    assert ST.oos_verdict(10, 0.1, None) == "OOS_FAIL"


def test_power_examples_from_the_document():
    k = 30
    a = 0.05 / k
    assert round(ST.power_delta(0.0356, 300, a) * 100, 2) == 0.78
    assert round(ST.power_delta(0.0356, 100, a) * 100, 2) == 1.34
    assert round(ST.power_delta(0.0129, 300, a) * 100, 2) == 0.28
    assert round(ST.power_delta(0.0575, 300, a) * 100, 2) == 1.25
    assert round(ST.power_delta(0.0575, 100, a) * 100, 2) == 2.17
    assert round(ST.power_delta(0.0285, 300, a) * 100, 2) == 0.62
    assert round(ST.power_delta(0.0285, 100, a) * 100, 2) == 1.08
