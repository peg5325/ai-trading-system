"""Reversal study v1 unit tests (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §11): config, rules T1-T4, overlay, costs, stats,
seeds. Synthetic data only; no real price file is read and no return is computed on real data."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pytest

from src.daytrade_paper import rules as PR
from src.daytrade_paper.core import Bar
from src.entry_study_v1 import stats as ST
from src.entry_study_v1 import trades as TR
from src.entry_study_v1.bars import InvalidRun
from src.entry_study_v1.config import CONFIG_PATH as ENTRY_CONFIG
from src.entry_study_v1.config import hhmm, load_cells, sha256_file
from src.entry_study_v1.pipeline import TradeRec
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import devrun
from src.reversal_study_v1 import overlay as OV
from src.reversal_study_v1 import rules as RL

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "research" / "PREREG_DAYTRADE_REVERSAL_STUDY_V1.md"
CELLS = RC.load_cells()
PARAMS = {r: next(c.params for c in CELLS if c.rule == r) for r in RC.RULES}


def t(s: str) -> int:
    return hhmm(s)


# ------------------------------------------------------------------ synthetic days


def mk(tt: int, o: float, c: float | None = None) -> Bar:
    c = o if c is None else c
    return Bar(tt, o, max(o, c) + 0.01, min(o, c) - 0.01, c, 1000.0)


def fb(tt: int, p: float, v: float = 1.0) -> Bar:
    """Flat bar: open = high = low = close = p (typical price exactly p)."""
    return Bar(tt, p, p, p, p, v)


def flat_bars(price=100.0) -> dict[int, Bar]:
    return {m: mk(m, price) for m in range(t("09:30"), t("16:00"))}


def flat_fb(price=100.0) -> dict[int, Bar]:
    return {m: fb(m, price) for m in range(t("09:30"), t("16:00"))}


def sigma_days(sigma=0.01, n=14) -> tuple[dict[int, Bar], ...]:
    """Prior sessions where |C_d(t) / O_d - 1| = sigma at every minute."""
    out = []
    for _ in range(n):
        d = flat_bars(100.0)
        for m in d:
            d[m] = mk(m, 100.0, 100.0 * (1 + sigma))
        out.append(d)
    return tuple(out)


def ref_day(bars=None, pre=(), pc=100.0, sigma=0.01) -> RL.RefDay:
    return RL.RefDay("2025-03-11", bars if bars is not None else flat_bars(), tuple(pre), "2025-03-10",
                     mk(t("15:59"), pc), sigma_days(sigma))


def with_bar(bars: dict[int, Bar], tt: int, o: float, c: float | None = None) -> dict[int, Bar]:
    return {**bars, tt: mk(tt, o, c)}


def with_px(bars: dict[int, Bar], first: str, last: str, p: float) -> dict[int, Bar]:
    return {**bars, **{m: fb(m, p) for m in range(t(first), t(last) + 1)}}


# ------------------------------------------------------------------ config vs the document


def _doc_params() -> dict[str, dict]:
    doc = PREREG.read_text(encoding="utf-8")
    block = doc.split("**params 키:**")[1].split("실행자는")[0]
    out: dict[str, dict] = {}
    for chunk in " ".join(block.split()).split(" · "):
        rule, rest = chunk.split(" ", 1)
        params: dict = {}
        for item in re.split(r",\s+(?=`)", rest.strip().rstrip(".")):
            keys = re.findall(r"`(\w+)`", item)
            vals = [json.loads(v) for v in item[item.rindex("`") + 1:].strip().split("/")]
            assert len(keys) == len(vals), item
            params.update(zip(keys, vals, strict=True))
        out[rule] = params
    return out


def test_config_matches_document():
    doc = PREREG.read_text(encoding="utf-8")
    pairs = re.findall(r"^\| (P\d) \| (\w+) \| (\w+) \| (\w+) \|", doc, flags=re.MULTILINE)
    assert pairs == [("P1", "SMH", "SOXL", "SOXS"), ("P2", "QQQ", "TQQQ", "SQQQ")]
    assert RC.pair_map() == {p: (r, lo, sh) for p, r, lo, sh in pairs}
    assert len(CELLS) == 16 == RC.K_TOTAL
    expect = [(p, r, s) for p, *_ in pairs for r in RC.RULES for s in ("long", "short")]
    assert [(c.pair, c.rule, c.side) for c in CELLS] == expect
    by_pair = {p[0]: p for p in pairs}
    for c in CELLS:
        _, ref, lo, sh = by_pair[c.pair]
        assert c.ref == ref and c.etf == (lo if c.side == "long" else sh)
        assert c.strategy_id == f"reversal-study-v1-cell-{c.k}"
        assert c.holds == {"30m": 30, "60m": 60, "close": "15:55"} and c.primary_hold == "close"
    assert [c.k for c in CELLS] == list(range(16))
    # the parameter block of §8.1, parsed from the document text itself
    assert _doc_params() == PARAMS
    # ... and copied by hand
    assert PARAMS["T1"] == {"gap_min_abs": 0.003, "pre_cutoff": "09:25", "window_first": "09:35",
                            "window_last": "10:29", "fill_equal": "filled"}
    assert PARAMS["T2"] == {"or_first": "09:30", "or_last": "09:44", "breakout_first": "09:45",
                            "breakout_last": "11:59", "fail_within_min": 30, "fail_last": "11:59"}
    assert PARAMS["T3"] == {"morning_first": "09:30", "morning_last": "10:59", "morning_frac": 0.80, "run_bars": 5,
                            "reclaim_first": "11:00", "reclaim_last": "14:59", "tp": "hlc3"}
    assert PARAMS["T4"] == {"sigma_sessions": 14, "multiplier": 1.0, "first_check_bar_start": "10:00",
                            "last_check_bar_start": "15:00"}
    for needle in ("K = 16", "20261013 + 1000 * variant + k", "reversal-study-v1-oos-a", "0.2021%", "0.3019%",
                   "0.1021%", ("variant: 0 = 주 검정, 1 = 30분, 2 = 60분, 3 = 스트레스 0.30%, 4 = 민감도 0.10%, "
                               "6 = 이벤트일 제외, 50 = OOS 주 검정")):
        assert needle in doc
    cfg = RC.cfg()
    assert cfg["seed"]["base"] == 20261013 and cfg["seed"]["variant_step"] == 1000
    assert cfg["seed"]["variants"] == {"main": 0, "hold_30m": 1, "hold_60m": 2, "stress_0.30": 3, "sens_0.10": 4,
                                       "no_event_days": 6, "oos_main": 50}
    assert cfg["ranking"]["max_cells"] == 1
    for name, sha in RC.frozen_inputs().items():
        assert sha in doc, name
    assert RC.frozen_inputs()["entry_dev_result_sha256"].startswith("542a9ffd")


def test_config_params_are_the_paper_constants():
    assert PARAMS["T1"]["gap_min_abs"] == PR.GAP_STRONG
    assert hhmm(PARAMS["T1"]["pre_cutoff"]) == PR.GAP_PRE_CUTOFF
    assert PARAMS["T3"]["morning_frac"] * 90 == 72.0  # 80% of the 90 morning bars, exactly 72


def test_designated_oos_cell_is_entry_k2_byte_for_byte():
    cfg = RC.cfg()["oos_designated"]
    entry = json.loads(ENTRY_CONFIG.read_text(encoding="utf-8"))["cells"][2]
    fields = ("params", "holds", "etf", "ref", "side", "rule")

    def canon(d):
        return json.dumps({f: d[f] for f in fields}, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert canon(cfg) == canon(entry)
    assert (entry["k"], entry["pair"], entry["rule"], entry["side"], entry["etf"]) == (2, "P1", "E2", "long", "SOXL")
    assert cfg["source_config_sha256"] == sha256_file(ENTRY_CONFIG)
    assert (cfg["k"], cfg["strategy_id"], cfg["pair"], cfg["primary_hold"]) == (
        16, "reversal-study-v1-oos-a", "P1", "close")
    cell = RC.load_designated()
    assert cell == type(cell)(**{**entry, "k": 16, "strategy_id": "reversal-study-v1-oos-a"})


def test_entry_config_is_loadable_with_the_same_format():
    assert [c.k for c in load_cells(RC.CONFIG_PATH)] == list(range(16))


def test_config_costs_equal_rules_derived_values():
    c = RC.cfg()["costs"]
    assert c["base_0.20"] == {"c": PR.COMMISSION, "s": 0.0, "f": PR.SEC_FEE}
    assert c["stress_0.30"] == {"c": PR.COMMISSION, "s": PR.SLIPPAGE, "f": PR.SEC_FEE}
    assert c["sens_0.10"] == {"c": PR.COMMISSION / 2, "s": 0.0, "f": PR.SEC_FEE}
    assert set(RC.COSTS) == {"base_0.20", "stress_0.30", "sens_0.10"}
    for name, cs in RC.COSTS.items():
        assert (cs.name, cs.c, cs.s, cs.f) == (name, c[name]["c"], c[name]["s"], c[name]["f"])


# ------------------------------------------------------------------ costs


def test_cost_round_trips():
    flat = {n: cs.round_trip_flat() for n, cs in RC.COSTS.items()}
    assert round(flat["base_0.20"] * 100, 4) == -0.2021
    assert round(flat["stress_0.30"] * 100, 4) == -0.3019
    assert round(flat["sens_0.10"] * 100, 4) == -0.1021
    assert RC.COSTS["base_0.20"].s == 0.0  # the base set has no slippage


def test_cost_hand_examples_and_sec_fee_on_sells_only():
    base, stress, sens = RC.COSTS["base_0.20"], RC.COSTS["stress_0.30"], RC.COSTS["sens_0.10"]
    # Pe = 100, Px = 101: buy 100 + 0.10 commission; sell 101 less 0.10 commission and 0.002081 SEC fee
    assert TR.net_return(100.0, 101.0, base) == pytest.approx((101.0 * (1 - 0.001 - 0.0000206)) / 100.0 - 1.001)
    assert TR.net_return(100.0, 101.0, base) == pytest.approx(0.00796919, abs=1e-8)
    # stress: both fills move 0.05% against us
    pe, px = 100.0 * 1.0005, 101.0 * 0.9995
    assert TR.net_return(100.0, 101.0, stress) == pytest.approx(px / pe * (1 - 0.001 - 0.0000206) - 1.001)
    assert TR.net_return(100.0, 101.0, stress) == pytest.approx(0.00696, abs=1e-5)
    # sensitivity: commission 0.05% per side
    assert TR.net_return(50.0, 49.0, sens) == pytest.approx(0.98 * (1 - 0.0005 - 0.0000206) - 1.0005)
    # the fee touches only the sell amount: a fee-only set loses (1 - f) of a flat round trip, nothing on the buy
    only_fee = TR.CostSet("f", 0.0, 0.0, 0.01)
    assert TR.net_return(100.0, 100.0, only_fee) == pytest.approx(-0.01)
    assert TR.net_return(100.0, 110.0, only_fee) == pytest.approx(1.1 * 0.99 - 1)


def test_trade_fills_at_the_next_bar_open_with_reversal_costs():
    cell = next(c for c in CELLS if c.rule == "T3" and c.side == "long")
    bars = {m: Bar(m, 50.0 + m / 1000, 60.0, 40.0, 55.0, 1.0) for m in range(t("09:30"), t("16:00"))}
    res = TR.trade(cell, t("11:01"), bars, "d", RC.COSTS)
    assert res["close"]["exit_t"] == t("15:55") and res["30m"]["exit_t"] == t("11:31")
    assert res["close"]["r"]["base_0.20"] == TR.net_return(bars[t("11:01")].open, bars[t("15:55")].open,
                                                            RC.COSTS["base_0.20"])
    assert set(res["close"]["r"]) == {"base_0.20", "stress_0.30", "sens_0.10"}


def test_hold_clipping_for_late_entries():
    cell = next(c for c in CELLS if c.rule == "T3" and c.side == "long")
    assert TR.hold_exits(cell, t("14:55")) == {"30m": t("15:25"), "60m": t("15:55"), "close": t("15:55")}
    assert TR.hold_exits(cell, t("14:56"))["60m"] == t("15:55")  # e + 60 > 15:55 -> the close bar
    assert TR.hold_exits(cell, t("15:00"))["60m"] == t("15:55")
    assert TR.hold_exits(cell, t("14:54"))["60m"] == t("15:54")
    assert TR.hold_exits(cell, t("15:01")) == {"30m": t("15:31"), "60m": t("15:55"), "close": t("15:55")}


# ------------------------------------------------------------------ T1


def t1_day(bars, pre=None, pc=100.0):
    return ref_day(bars, [mk(480, 100.5)] if pre is None else pre, pc)


def test_t1_gap_up_fill_and_boundaries():
    p = PARAMS["T1"]
    up = flat_bars(100.5)
    res = RL.t1(t1_day(with_bar(up, t("09:50"), 100.5, 99.9)), p)
    assert res.signal.side == "short" and res.signal.entry_t == t("09:51") and not res.info["pre_substituted"]
    # C == PC counts as filled
    assert RL.t1(t1_day(with_bar(up, t("09:50"), 100.5, 100.0)), p).signal.entry_t == t("09:51")
    # 09:35 is the first minute looked at; 10:29 the last (fill 10:30)
    assert RL.t1(t1_day(with_bar(up, t("09:35"), 100.5, 99.9)), p).signal.entry_t == t("09:36")
    assert RL.t1(t1_day(with_bar(up, t("10:29"), 100.5, 99.9)), p).signal.entry_t == t("10:30")
    assert RL.t1(t1_day(with_bar(up, t("10:30"), 100.5, 99.9)), p).signal is None
    # a fill before 09:35 is not a signal; it is only counted
    early = RL.t1(t1_day(with_bar(up, t("09:34"), 100.5, 99.9)), p)
    assert early.signal is None and early.reason == "not_filled" and early.info["filled_before_window"]
    both = RL.t1(t1_day(with_bar(with_bar(up, t("09:32"), 100.5, 99.9), t("09:50"), 100.5, 99.9)), p)
    assert both.signal.entry_t == t("09:51")
    # the open already on the other side of PC
    assert RL.t1(t1_day({**up, t("09:30"): mk(t("09:30"), 99.0)}), p).info["open_already_filled"]
    assert not RL.t1(t1_day(up), p).info["open_already_filled"]
    assert RL.t1(t1_day(up), p).reason == "not_filled"


def test_t1_gap_down_gives_long_and_small_gap_gives_nothing():
    p = PARAMS["T1"]
    dn = flat_bars(99.5)
    res = RL.t1(t1_day(with_bar(dn, t("10:00"), 99.5, 100.2), pre=[mk(480, 99.5)]), p)
    assert res.signal.side == "long" and res.signal.entry_t == t("10:01")
    assert RL.t1(t1_day(flat_bars(100.1), pre=[mk(480, 100.1)]), p).reason == "gap_small"


def test_t1_threshold_inclusive_pre_fallback_and_pre_cutoff():
    p = PARAMS["T1"]
    up = with_bar(flat_bars(100.5), t("10:00"), 100.5, 99.9)
    day = t1_day(up)
    g = RL.gap_of(day, RL.visible(up, t("09:31")), p)[0]
    assert RL.t1(day, {**p, "gap_min_abs": g}).signal is not None  # |g| == 0.0030-style threshold exactly
    assert RL.t1(day, {**p, "gap_min_abs": math.nextafter(g, 1)}).signal is None
    # 100.3 / 100 - 1 is 0.0029999999999998916 in float64: below 0.0030, so no signal (no rounding)
    assert RL.t1(t1_day(up, pre=[mk(480, 100.3)]), p).reason == "gap_small"
    # no PRE bar before 09:25: the 09:30 open stands in; a PRE bar at 09:25 or later is not used
    sub = RL.t1(t1_day(up, pre=[]), p)
    assert sub.signal is not None and sub.info["pre_substituted"] and sub.info["gap_source"].startswith("09:30")
    assert RL.t1(t1_day(up, pre=[mk(t("09:25"), 100.5)]), p).info["pre_substituted"]


# ------------------------------------------------------------------ T2


def test_t2_failure_signals_and_entry_minute():
    p = PARAMS["T2"]
    base = flat_bars()
    up = RL.t2(ref_day(with_bar(base, t("10:00"), 100, 100.5)), p)  # b = 10:00, back inside at 10:01
    assert up.signal.side == "short" and up.signal.entry_t == t("10:02") and up.info["breakout_t"] == t("10:00")
    dn = RL.t2(ref_day(with_bar(base, t("10:00"), 100, 99.5)), p)
    assert dn.signal.side == "long" and dn.signal.entry_t == t("10:02")
    # the earliest decision: breakout at 09:45, failure at 09:46, fill 09:47
    assert RL.t2(ref_day(with_bar(base, t("09:45"), 100, 100.5)), p).signal.entry_t == t("09:47")
    # no breakout at all
    assert RL.t2(ref_day(base), p).reason == "no_breakout"


def test_t2_equality_and_opening_range_window():
    p = PARAMS["T2"]
    base = flat_bars()
    orh, orl = RL.opening_range(base, p)
    assert (orh, orl) == (max(b.high for m, b in base.items() if m <= t("09:44")),
                          min(b.low for m, b in base.items() if m <= t("09:44")))
    assert RL.t2(ref_day(with_bar(base, t("10:00"), 100, orh)), p).reason == "no_breakout"  # C == ORH is inside
    assert RL.t2(ref_day(with_bar(base, t("10:00"), 100, orl)), p).reason == "no_breakout"
    # a 09:30..09:44 high / low sets the range; 09:45 bars do not
    wide = with_bar(base, t("09:40"), 100, 101.0)
    assert RL.opening_range(wide, p)[0] == pytest.approx(101.01)
    assert RL.t2(ref_day(with_bar(wide, t("10:00"), 100, 100.5)), p).reason == "no_breakout"
    assert RL.opening_range(with_bar(base, t("09:45"), 100, 105.0), p) == (orh, orl)
    with pytest.raises(InvalidRun):
        RL.opening_range({m: b for m, b in base.items() if m != t("09:40")}, p)


def test_t2_failure_window_is_b_plus_30_inclusive():
    p = PARAMS["T2"]
    base = flat_bars()
    stay = with_px(base, "10:01", "10:29", 100.5)
    stay = {m: (mk(m, 100, 100.5) if t("10:00") <= m <= t("10:29") else b) for m, b in base.items()}
    assert RL.t2(ref_day(stay), p).signal.entry_t == t("10:31")  # u = b + 30 = 10:30 is the first bar back inside
    stay31 = {m: (mk(m, 100, 100.5) if t("10:00") <= m <= t("10:30") else b) for m, b in base.items()}
    res = RL.t2(ref_day(stay31), p)  # back inside only at b + 31
    assert res.signal is None and res.reason == "no_failure"


def test_t2_upper_limit_11_59_and_late_breakouts():
    p = PARAMS["T2"]
    base = flat_bars()
    # b = 11:30: u is capped at 11:59 (not b + 30 = 12:00); a failure at 11:59 gives the 12:00 fill
    run = {m: (mk(m, 100, 100.5) if t("11:30") <= m <= t("11:58") else b) for m, b in base.items()}
    assert RL.t2(ref_day(run), p).signal.entry_t == t("12:00")
    run2 = {m: (mk(m, 100, 100.5) if t("11:30") <= m <= t("11:59") else b) for m, b in base.items()}
    assert RL.t2(ref_day(run2), p).reason == "no_failure"  # 12:00 is not looked at
    # b = 11:59: no u exists
    last = RL.t2(ref_day(with_bar(base, t("11:59"), 100, 100.5)), p)
    assert last.signal is None and last.info["breakout_t"] == t("11:59")
    # a breakout at 12:00 is outside the breakout window
    assert RL.t2(ref_day(with_bar(base, t("12:00"), 100, 100.5)), p).reason == "no_breakout"


def test_t2_far_side_close_counts_as_failure_and_only_first_breakout_is_looked_at():
    p = PARAMS["T2"]
    base = flat_bars()
    through = with_bar(with_bar(base, t("10:00"), 100, 100.5), t("10:01"), 100, 99.0)  # up breakout, then below ORL
    res = RL.t2(ref_day(through), p)
    assert res.signal.side == "short" and res.signal.entry_t == t("10:02")
    stay = {m: (mk(m, 100, 100.5) if t("10:00") <= m <= t("10:30") else b) for m, b in base.items()}
    stay = with_bar(with_bar(stay, t("11:00"), 100, 99.5), t("11:01"), 100, 100.0)  # second breakout would fail
    assert RL.t2(ref_day(stay), p).signal is None


# ------------------------------------------------------------------ T3


def t3_bars(n_extreme=72, up=False, v0=1e9) -> dict[int, Bar]:
    """Heavy 09:30 bar at 100 pins VWAP just beside 100; the next n_extreme bars sit on one side of it
    (99 below / 101 above), every other bar is 100 (the other side)."""
    x = 101.0 if up else 99.0
    bars = {m: fb(m, 100.0) for m in range(t("09:30"), t("16:00"))}
    bars[t("09:30")] = fb(t("09:30"), 100.0, v0)
    for m in range(t("09:31"), t("09:31") + n_extreme):
        bars[m] = fb(m, x)
    return bars


def test_vwap_includes_the_current_bar_and_uses_typical_price():
    bars = {t("09:30"): Bar(t("09:30"), 100, 102, 98, 101, 2.0), t("09:31"): Bar(t("09:31"), 101, 103, 99, 102, 3.0)}
    tp0, tp1 = (102 + 98 + 101) / 3, (103 + 99 + 102) / 3
    s = RL.vwap_series(bars, t("09:31"))
    assert s[t("09:30")] == pytest.approx(tp0)  # bar t is inside VWAP(t)
    assert s[t("09:31")] == pytest.approx((tp0 * 2 + tp1 * 3) / 5)
    # later bars never change an earlier value
    later = {**bars, t("09:32"): Bar(t("09:32"), 1, 1e6, 1, 1e6, 1e9)}
    assert RL.vwap_series(later, t("09:32"))[t("09:31")] == s[t("09:31")]
    with pytest.raises(InvalidRun):
        RL.vwap_series({t("09:31"): bars[t("09:31")]}, t("09:31"))  # the series starts at 09:30
    zero = {**bars, t("09:30"): Bar(t("09:30"), 100, 102, 98, 101, 0.0)}
    z = RL.vwap_series(zero, t("09:31"))
    assert z[t("09:30")] is None and z[t("09:31")] == pytest.approx(tp1)  # sum V = 0 -> undefined, then defined


def test_t3_down_morning_reclaim_threshold_72_of_90():
    p = PARAMS["T3"]
    res = RL.t3(ref_day(t3_bars(72)), p)  # 72 bars below VWAP, the rest above: run 10:56..11:00 above -> long
    assert res.signal.side == "long" and res.signal.entry_t == t("11:01") and res.info["morning"] == "down"
    res71 = RL.t3(ref_day(t3_bars(71)), p)  # 71/90 is not a trend morning
    assert res71.signal is None and res71.reason == "no_trend_morning" and res71.info["below"] == 71


def test_t3_up_morning_loss_gives_short():
    res = RL.t3(ref_day(t3_bars(72, up=True)), PARAMS["T3"])
    assert res.signal.side == "short" and res.signal.entry_t == t("11:01") and res.info["morning"] == "up"
    assert RL.t3(ref_day(t3_bars(71, up=True)), PARAMS["T3"]).reason == "no_trend_morning"


def test_t3_run_must_be_five_consecutive_bars_and_may_start_before_11_00():
    p = PARAMS["T3"]
    # 10:56..10:59 below again, so the run has to start at 11:00 ... and bar 11:04 breaks 11:00..11:03 (4 bars)
    bars = with_px(t3_bars(72), "10:56", "10:59", 99.0)
    bars = {**bars, t("11:04"): fb(t("11:04"), 99.0)}
    res = RL.t3(ref_day(bars), p)
    assert res.signal.entry_t == t("11:10")  # 11:05..11:09 are the first five above VWAP in a row
    # without the break the five bars 11:00..11:04 fire at 11:04
    assert RL.t3(ref_day(with_px(t3_bars(72), "10:56", "10:59", 99.0)), p).signal.entry_t == t("11:05")
    # 10:56..10:59 above plus 11:00: the run reaches back into the morning
    assert RL.t3(ref_day(t3_bars(72)), p).signal.entry_t == t("11:01")


def test_t3_close_equal_to_vwap_is_on_neither_side():
    p = PARAMS["T3"]
    bars = with_px(t3_bars(72), "10:56", "10:59", 99.0)
    target = t("11:02")
    base = RL.vwap_series(bars, target)[target]
    # find a flat-bar close that equals VWAP(11:02) with the bar itself included (float residue aside)
    c = next(x for x in (base, math.nextafter(base, 0), math.nextafter(base, 200), math.nextafter(base, -1))
             if RL.vwap_series({**bars, target: fb(target, x)}, target)[target] == x)
    eq = {**bars, target: fb(target, c)}
    assert RL.vwap_series(eq, target)[target] == c
    res = RL.t3(ref_day(eq), p)
    # 11:00, 11:01 above, 11:02 equal (breaks), 11:03..11:07 above -> fires at 11:07, fill 11:08
    assert res.signal.entry_t == t("11:08")


def test_t3_last_reclaim_minute_is_14_59():
    p = PARAMS["T3"]
    low = with_px(t3_bars(72), "11:00", "14:54", 99.0)  # 14:55 onwards is above VWAP again
    assert RL.t3(ref_day(low), p).signal.entry_t == t("15:00")  # 14:55..14:59 is the last possible run
    late = with_px(low, "14:55", "14:55", 99.0)  # the fifth bar would be 15:00, which is not looked at
    res = RL.t3(ref_day(late), p)
    assert res.signal is None and res.reason == "no_reclaim"


def test_t3_zero_volume_makes_vwap_undefined_and_the_day_has_no_signal():
    bars = {**t3_bars(72), t("09:30"): fb(t("09:30"), 100.0, 0.0)}
    res = RL.t3(ref_day(bars), PARAMS["T3"])
    assert res.signal is None and res.reason == "vwap_undefined"
    zero_all = {m: fb(m, 100.0, 0.0) for m in range(t("09:30"), t("16:00"))}
    assert RL.t3(ref_day(zero_all), PARAMS["T3"]).reason == "vwap_undefined"


# ------------------------------------------------------------------ T4


def band(sig=0.01, o=100.0, pc=100.0):
    return max(o, pc) * (1 + sig), min(o, pc) * (1 - sig)


def test_t4_opposite_breakout_gives_the_reverse_trade():
    p = PARAMS["T4"]
    u, lo = band()
    bars = with_bar(with_bar(flat_bars(), t("10:00"), 100, u + 1), t("10:05"), 100, lo - 0.5)
    res = RL.t4(ref_day(bars), p)
    assert res.signal.side == "short" and res.signal.entry_t == t("10:06") and res.info["first_dir"] == "up"
    bars = with_bar(with_bar(flat_bars(), t("10:00"), 100, lo - 1), t("10:05"), 100, u + 0.5)
    res = RL.t4(ref_day(bars), p)
    assert res.signal.side == "long" and res.signal.entry_t == t("10:06") and res.info["first_dir"] == "down"
    assert RL.t4(ref_day(flat_bars()), p).reason == "no_first_breakout"
    only_first = with_bar(flat_bars(), t("10:00"), 100, u + 1)
    assert RL.t4(ref_day(only_first), p).reason == "no_opposite"
    # an up breakout followed by another up breakout is not a reversal
    assert RL.t4(ref_day(with_bar(only_first, t("10:05"), 100, u + 1)), p).signal is None


def test_t4_equality_is_inside_and_the_last_minute_is_15_00():
    p = PARAMS["T4"]
    sig = RL.sigma_at(sigma_days(0.01), t("10:05"), 14)
    u, lo = RL.bands(ref_day(flat_bars()), sig, 1.0)
    first = with_bar(flat_bars(), t("10:00"), 100, u + 1)
    assert RL.t4(ref_day(with_bar(first, t("10:05"), 100, lo)), p).signal is None  # C == L is inside
    assert RL.t4(ref_day(with_bar(first, t("10:05"), 100, math.nextafter(lo, 0))), p).signal.side == "short"
    # first breakout at 14:59, opposite at 15:00 (last check) fills 15:01; at 15:01 it is not looked at
    f2 = with_bar(flat_bars(), t("14:59"), 100, u + 1)
    assert RL.t4(ref_day(with_bar(f2, t("15:00"), 100, lo - 1)), p).signal.entry_t == t("15:01")
    assert RL.t4(ref_day(with_bar(f2, t("15:01"), 100, lo - 1)), p).signal is None
    # t1 = 15:00: nothing can follow
    f3 = with_bar(flat_bars(), t("15:00"), 100, u + 1)
    assert RL.t4(ref_day(with_bar(f3, t("15:01"), 100, lo - 1)), p).reason == "no_opposite"
    # first breakouts before 10:00 do not exist
    assert RL.t4(ref_day(with_bar(flat_bars(), t("09:59"), 100, u + 1)), p).reason == "no_first_breakout"


def test_t4_sigma_undefined_minute_is_skipped_for_the_opposite_breakout():
    p = PARAMS["T4"]
    u, lo = band()
    sdays = list(sigma_days(0.01))
    del sdays[3][t("10:05")]  # sigma(10:05) undefined
    bars = with_bar(with_bar(with_bar(flat_bars(), t("10:00"), 100, u + 1), t("10:05"), 100, lo - 1),
                    t("10:06"), 100, lo - 1)
    day = RL.RefDay("2025-03-11", bars, (), "2025-03-10", mk(t("15:59"), 100.0), tuple(sdays))
    res = RL.t4(day, p)
    assert res.signal.entry_t == t("10:07") and res.info["sigma_undefined"] == 1  # 10:05 skipped, 10:06 fires


def test_sigma_uses_prior_sessions_only():
    day = ref_day(with_bar(flat_bars(), t("10:00"), 100, 150))
    other = ref_day(with_bar(flat_bars(), t("10:00"), 100, 50))
    assert RL.sigma_at(day.sigma_days, t("10:00"), 14) == RL.sigma_at(other.sigma_days, t("10:00"), 14)


# ------------------------------------------------------------------ look-ahead (T1-T4 and the overlay)


def _scenarios():
    u, lo = band()
    p3 = with_px(t3_bars(72), "10:56", "10:59", 99.0)
    return {
        "T1": [(with_bar(flat_bars(100.5), t("10:00"), 100.5, 99.9), t("10:30")), (flat_bars(100.5), t("10:30"))],
        "T2": [(with_bar(flat_bars(), t("10:00"), 100, 100.5), t("12:00")), (flat_bars(), t("12:00")),
               (with_bar(flat_bars(), t("11:59"), 100, 100.5), t("12:00"))],
        "T3": [(p3, t("15:00")), (t3_bars(71), t("15:00")), (with_px(p3, "11:00", "11:04", 99.0), t("15:00"))],
        "T4": [(with_bar(with_bar(flat_bars(), t("10:00"), 100, u + 1), t("10:05"), 100, lo - 1), t("15:01")),
               (with_bar(flat_bars(), t("10:00"), 100, u + 1), t("15:01"))],
    }


@pytest.mark.parametrize("rule", RC.RULES)
def test_no_lookahead(rule):
    """Replacing or deleting every ref bar that starts at or after the fill minute (or, with no signal, after the
    last decision minute of the rule) leaves the result unchanged."""
    p = PARAMS[rule]
    pre = [mk(480, 100.5)]
    for bars, last_decision_end in _scenarios()[rule]:
        full = RL.evaluate(rule, ref_day(bars, pre), p)
        cut = full.signal.entry_t if full.signal is not None else last_decision_end
        garbage = {m: (fb(m, 1e6, 1e9) if m >= cut else b) for m, b in bars.items()}
        truncated = {m: b for m, b in bars.items() if m < cut}
        for other in (garbage, truncated):
            res = RL.evaluate(rule, ref_day(other, pre), p)
            assert res.signal == full.signal and res.reason == full.reason


def test_t2_opening_range_is_fixed_by_0944_and_vwap_by_t():
    p = PARAMS["T2"]
    bars = with_bar(flat_bars(), t("10:00"), 100, 100.5)
    changed = {m: (fb(m, 5e5, 9e9) if m >= t("09:45") else b) for m, b in bars.items()}
    assert RL.opening_range(changed, p) == RL.opening_range(bars, p)
    for tt in (t("09:45"), t("10:30"), t("14:59")):
        a = RL.vwap_series(bars, tt)
        b = RL.vwap_series({m: (fb(m, 5e5, 9e9) if m > tt else x) for m, x in bars.items()}, tt)
        assert a == b


def test_t4_uses_no_bar_of_today_for_sigma():
    u, lo = band()
    bars = with_bar(with_bar(flat_bars(), t("10:00"), 100, u + 1), t("10:05"), 100, lo - 1)
    day = ref_day(bars)
    sig_before = RL.sigma_at(day.sigma_days, t("10:05"), 14)
    scrambled = {m: (fb(m, 1e3) if m != t("10:00") and m != t("10:05") else b) for m, b in bars.items()}
    assert RL.sigma_at(ref_day(scrambled).sigma_days, t("10:05"), 14) == sig_before


# ------------------------------------------------------------------ overlay


T3P, T4P = PARAMS["T3"], PARAMS["T4"]


def ov(bars, side="long", entry="09:35", sigma=0.01, pc=100.0):
    return OV.overlay_exit(ref_day(bars, (), pc, sigma), side, t(entry), T3P, T4P)


def test_overlay_t3_type_five_bars_below_vwap_for_a_long_etf():
    bars = with_px(flat_fb(), "10:00", "10:04", 99.9)  # above L = 99, so the T4-type stays silent
    res = ov(bars)
    assert (res.exit_t, res.reason) == (t("10:05"), "T3")
    four = with_px(flat_fb(), "10:00", "10:03", 99.9)
    assert ov(four).reason == "close" and ov(four).exit_t == t("15:55")
    # the inverse ETF exits when the reference is ABOVE vwap (and above U = 101 for the T4-type)
    up = with_px(flat_fb(), "10:00", "10:04", 100.5)
    assert (ov(up, side="short").exit_t, ov(up, side="short").reason) == (t("10:05"), "T3")
    assert ov(up, side="long").exit_t != t("10:05")  # five bars ABOVE vwap do not close a long ETF


def test_overlay_t4_type_and_window_limits():
    bars = with_px(flat_fb(), "10:30", "10:30", 98.5)  # C < L = 99
    assert (ov(bars).exit_t, ov(bars).reason) == (t("10:31"), "T4")
    up = with_px(flat_fb(), "10:30", "10:30", 101.5)  # C > U = 101 closes the inverse ETF
    assert (ov(up, side="short").exit_t, ov(up, side="short").reason) == (t("10:31"), "T4")
    assert ov(up, side="long").reason != "T4"  # C > U does not close a long ETF
    # T4-type needs t >= 10:00 and t <= 15:00
    assert ov(with_px(flat_fb(), "09:59", "09:59", 98.5)).reason == "close"
    assert ov(with_px(flat_fb(), "10:00", "10:00", 98.5)).exit_t == t("10:01")
    assert ov(with_px(flat_fb(), "15:00", "15:00", 98.5)).exit_t == t("15:01")
    assert ov(with_px(flat_fb(), "15:01", "15:01", 98.5)).reason == "close"
    # ... and it needs sigma(t): a missing sigma-session bar silences it for that minute only
    sdays = list(sigma_days(0.01))
    del sdays[0][t("10:30")]
    day = RL.RefDay("d", with_px(flat_fb(), "10:30", "10:30", 98.5), (), "p", mk(t("15:59"), 100.0), tuple(sdays))
    assert OV.overlay_exit(day, "long", t("09:35"), T3P, T4P).reason == "close"


def test_overlay_is_one_combined_rule_first_signal_wins_and_both():
    # T4-type at 10:30, T3-type would only complete at 11:04: one exit, at the first signal, reason T4
    bars = with_px(with_px(flat_fb(), "10:30", "10:30", 98.5), "11:00", "11:04", 99.9)
    res = ov(bars)
    assert (res.exit_t, res.reason) == (t("10:31"), "T4")
    # T3-type first (10:04), T4-type later: T3
    bars = with_px(with_px(flat_fb(), "10:00", "10:04", 99.9), "10:30", "10:30", 98.5)
    assert (ov(bars).exit_t, ov(bars).reason) == (t("10:05"), "T3")
    # both on the same minute: four bars at 99.5 (below VWAP, above L) then 98.5 (below VWAP and below L)
    both = with_px(with_px(flat_fb(), "10:00", "10:03", 99.5), "10:04", "10:04", 98.5)
    res = ov(both)
    assert (res.exit_t, res.reason) == (t("10:05"), "both")
    both_short = with_px(with_px(flat_fb(), "10:00", "10:03", 100.5), "10:04", "10:04", 101.5)
    assert ov(both_short, side="short").reason == "both"


def test_overlay_run_must_start_at_or_after_the_entry_bar_and_last_signal_is_15_53():
    bars = with_px(flat_fb(), "09:56", "10:00", 99.9)
    assert ov(bars, entry="10:00").reason == "close"  # only one of the five bars is at/after the entry bar
    assert ov(bars, entry="09:56").exit_t == t("10:01")  # entry bar included: the run is 09:56..10:00
    assert ov(with_px(flat_fb(), "15:49", "15:53", 99.9)).exit_t == t("15:54")
    assert ov(with_px(flat_fb(), "15:50", "15:54", 99.9)).reason == "close"
    # entering late: the entry bar is bar e itself and counts for the run
    assert ov(with_px(flat_fb(), "15:30", "15:34", 99.9), entry="15:30").exit_t == t("15:35")
    assert ov(with_px(flat_fb(), "15:30", "15:34", 99.9), entry="15:31").reason == "close"


def test_overlay_vwap_runs_from_the_open_not_from_the_entry():
    # heavy early volume at 100, then the entry-time price level is 99.9: VWAP stays ~100, so 99.9 is below VWAP
    bars = {m: fb(m, 99.9) for m in range(t("09:31"), t("16:00"))}
    bars[t("09:30")] = fb(t("09:30"), 100.0, 1e9)
    res = ov(bars, entry="14:00")
    assert (res.exit_t, res.reason) == (t("14:05"), "T3")
    # VWAP anchored at the entry would equal 99.9 and never be undercut
    assert res.exit_t != t("15:55")


@pytest.mark.parametrize("side", ["long", "short"])
def test_overlay_no_lookahead(side):
    x = 98.5 if side == "long" else 101.5
    cases = [with_px(flat_fb(), "10:30", "10:30", x), flat_fb(),
             with_px(flat_fb(), "10:00", "10:04", 99.9 if side == "long" else 100.1)]
    for bars in cases:
        full = ov(bars, side)
        cut = full.exit_t if full.reason != "close" else t("15:55")
        garbage = {m: (fb(m, 1e6, 1e9) if m >= cut else b) for m, b in bars.items()}
        truncated = {m: b for m, b in bars.items() if m < cut}
        assert ov(garbage, side) == full and ov(truncated, side) == full


# ------------------------------------------------------------------ statistics, seeds


def _x_seg():
    rng = np.random.default_rng(7)
    x = list(rng.normal(0.004, 0.03, 40))
    return x, ["A"] * 20 + ["B"] * 20


def test_stats_reproduce_p_and_holm_for_fixed_inputs():
    x, seg = _x_seg()
    p1 = ST.block_bootstrap_p(x, seg, RC.seed(0, 3), 500)
    assert p1 == ST.block_bootstrap_p(x, seg, 20261016, 500)  # RC.seed(0, 3) == 20261013 + 3
    assert p1 != ST.block_bootstrap_p(x, seg, 20261017, 500)
    ps = {k: (None if k in (3, 9) else (k + 1) / 100) for k in range(16)}
    adj = ST.holm_adjust(ps, 16)
    assert adj[3] == adj[9] == 1.0  # untestable cells count in K and sort last
    order = [k for k in sorted(ps, key=lambda k: (ps[k] is None, ps[k] or 0, k)) if ps[k] is not None]
    run = 0.0
    for j, k in enumerate(order, start=1):  # Holm step-down, written out independently
        run = max(run, min(1.0, (16 - j + 1) * ps[k]))
        assert adj[k] == pytest.approx(run)
    with pytest.raises(ValueError):
        ST.holm_adjust({k: 0.5 for k in range(15)}, 16)


def test_small_segments_are_dropped_from_the_test_only_and_zero_variance_gives_none():
    x, seg = _x_seg()
    a_only = ST.block_bootstrap_p(x[:20], seg[:20], 99, 300)
    five_b = ST.block_bootstrap_p(x[:20] + [0.5] * 5, seg[:20] + ["B"] * 5, 99, 300)
    assert a_only == five_b  # n_s <= 5 -> segment B dropped
    s = ST.summarize(x[:20] + [0.5] * 5, seg[:20] + ["B"] * 5, 99, 300)
    assert s["n"] == 25 and s["n_by_segment"] == {"A": 20, "B": 5}  # still counted in the summary
    assert ST.block_bootstrap_p([0.01] * 30, ["A"] * 30, 1, 100) is None
    assert ST.block_bootstrap_p([0.01, 0.02] * 5, ["A"] * 5 + ["B"] * 5, 1, 100) is None  # no testable segment


def test_dev_pass_boundaries_and_ranking():
    ok = {"n": 30, "holm_p": 0.049, "mean_a": 0.001, "mean_b": 0.002, "mean_stress": 0.0005}
    assert ST.dev_pass(**ok)["pass"]
    assert not ST.dev_pass(**{**ok, "holm_p": 0.05})["pass"]
    assert not ST.dev_pass(**{**ok, "holm_p": None})["pass"]
    assert not ST.dev_pass(**{**ok, "n": 29})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_b": 0.0})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_b": None})["pass"]
    assert not ST.dev_pass(**{**ok, "mean_stress": 0.0})["pass"]
    rows = [{"k": 5, "holm_p": 0.02, "mean": 0.01, "n": 40}, {"k": 2, "holm_p": 0.02, "mean": 0.01, "n": 50},
            {"k": 9, "holm_p": 0.01, "mean": 0.001, "n": 30}]
    assert ST.rank_dev_pass(rows, max_cells=1) == [9]
    assert ST.rank_dev_pass(rows[:2], max_cells=1) == [2]  # equal p and mean: larger n first


def test_seeds_are_passed_directly_and_the_entry_seed_helper_is_never_used(monkeypatch):
    assert RC.seed(0, 0) == 20261013 and RC.seed(50, 16) == 20261013 + 50_000 + 16 and RC.seed(6, 15) == 20261013 + 6015
    for path in [*(ROOT / "src" / "reversal_study_v1").glob("*.py"), *ROOT.joinpath("scripts").glob("reversal_*")]:
        text = path.read_text(encoding="utf-8")
        assert "seed_for" not in text and "BASE_SEED" not in text, path

    def boom(*a, **k):
        raise AssertionError("entry seed helper called")

    monkeypatch.setattr(ST, "seed_for", boom)
    seen = []
    real = ST.block_bootstrap_p

    def spy(x, seg, seed, b_count=ST.B_DEFAULT):
        seen.append(seed)
        return real(x, seg, seed, b_count)

    monkeypatch.setattr(ST, "block_bootstrap_p", spy)
    cell = CELLS[5]
    recs = _recs(cell, 40)
    devrun.cell_report(cell, recs, 50)
    assert seen == [RC.seed(v, 5) for v in (0, 1, 2, 3, 4, 6)]
    seen.clear()
    devrun.oos_cell("a", RC.load_designated(), _recs(RC.load_designated(), 40), 50)
    assert seen == [RC.seed(50, 16)]


def _recs(cell, n, seed=3) -> list[TradeRec]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        res = {}
        for hold in ("30m", "60m", "close"):
            base = float(rng.normal(0.003, 0.02))
            res[hold] = {"exit_t": 955, "mae": -abs(float(rng.normal(0.01, 0.01))),
                         "r": {RC.BASE: base, RC.STRESS: base - 0.001, RC.SENS: base + 0.001}}
        out.append(TradeRec(cell.k, f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}", "A" if i < n // 2 else "B",
                            i % 10 == 0, 600 + i, res))
    return out


def test_run_dev_requires_16_cells_and_is_deterministic():
    trades = {c.k: _recs(c, 35, seed=c.k) for c in CELLS}
    out = devrun.run_dev(CELLS, trades, b_count=100)
    assert out["k_total"] == 16 and len(out["cells"]) == 16
    assert out["verdict"] in ("NO_PASS", "DEV_PASS") and len(out["selected_for_oos"]) <= 1
    for r in out["cells"]:
        assert set(r["variants"]) == set(devrun.REPORT_VARIANTS) and 0 <= r["holm_p"] <= 1
        assert r["entry_time"]["min"] == 600
    assert json.dumps(out) == json.dumps(devrun.run_dev(CELLS, trades, b_count=100))
    with pytest.raises(ValueError):
        devrun.run_dev(CELLS[:15], {c.k: trades[c.k] for c in CELLS[:15]}, b_count=10)
    # selection: one cell at most, and only when DEV_PASS
    strong = {c.k: _recs(c, 60, seed=c.k) for c in CELLS}
    boosted = {k: [TradeRec(r.k, r.day, r.seg, r.event_day, r.entry_t,
                            {h: {**v, "r": {n: x + 0.05 for n, x in v["r"].items()}} for h, v in r.res.items()})
                   for r in recs] if k == 4 else recs for k, recs in strong.items()}
    res = devrun.run_dev(CELLS, boosted, b_count=1500)
    assert res["verdict"] == "DEV_PASS" and res["selected_for_oos"] == [4] and 4 in res["dev_pass_cells"]
    assert all(r["dev_pass"]["pass"] == (r["k"] in res["dev_pass_cells"]) for r in res["cells"])
