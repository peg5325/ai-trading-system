"""V3 model A asymmetric trend switching (docs/research/EXPLORE_V3_ASYM_SWING_2000USD.md). Synthetic data only."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.daytrade_paper.core import Bar
from src.explore_v3_asym_swing import engine as E
from src.explore_v3_asym_swing.indicators import (
    EMA,
    M1,
    Aggregator,
    Bucket,
    SuperTrend,
    WilderATR,
    build_stream,
)
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import _write_day
from tests.test_explore_pullback_vwap_v1 import _load_script

D1, D2, D3 = "2024-03-04", "2024-03-05", "2024-03-06"
CFG = E.Config("T", 10, 2.5, 5, 0.0040, 0.0022, 0.0030)


def m(day, minute, o, h=None, low=None, c=None, v=1000.0):
    c = o if c is None else c
    return M1(day, minute, o, max(o, c) if h is None else h, min(o, c) if low is None else low, c, v)


def ready_engine(direction=1, sign=1, vol=True, cfg=CFG):
    eng = E.Engine(cfg)
    eng.st_dir, eng.vol_ok, eng.sign = direction, vol, sign
    eng.ema9.value, eng.ema21.value = (101.0, 100.0) if sign > 0 else (99.0, 100.0)
    return eng


def short_bucket(day, idx, o, c):
    return Bucket(day, idx, o, max(o, c), min(o, c), c, 570 + idx * 5 + 4)


# ---------------------------------------------------------------- indicators
def test_ema_seed_and_alpha():
    e = EMA(3)
    assert [e.update(x) for x in (1.0, 2.0, 3.0)] == [None, None, 2.0]
    assert e.update(6.0) == pytest.approx(2.0 + 0.5 * 4.0)


def test_wilder_atr():
    a = WilderATR(2)
    assert a.update(11, 9, 10) is None  # tr 2
    assert a.update(12, 10, 11) == pytest.approx((2 + 2) / 2)  # tr max(2, 2, 0) = 2
    assert a.update(15, 11, 14) == pytest.approx((2 * 1 + 4) / 2)  # tr max(4, 4, 0) = 4


def test_supertrend_flips_on_close_through_band():
    st = SuperTrend(2, 1.0)
    assert st.update(101, 99, 100) is None
    d = st.update(102, 100, 101.5)  # atr 2, hl2 101, bands 103 / 99, close 101.5 > 101 -> +1
    assert d == 1 and (st.upper, st.lower) == (103, 99)
    assert st.update(103, 101, 102) == 1  # lower ratchets up to 100
    assert st.lower == pytest.approx(100.0)
    assert st.update(100, 96, 97) == -1  # close below the final lower band


def test_aggregator_completes_on_last_minute_or_stale():
    a = Aggregator(5)
    assert a.add(m(D1, 570, 100)) is None and a.add(m(D1, 573, 101)) is None  # 571, 572 missing
    assert a.close_stale(m(D1, 574, 102)) is None
    b = a.add(m(D1, 574, 102, c=103))
    assert (b.index, b.o, b.c, b.last_minute) == (0, 100, 103, 574)
    assert a.add(m(D1, 575, 104)) is None  # 576..579 missing
    b = a.close_stale(m(D1, 580, 105))  # completes only when a later bucket's bar arrives
    assert (b.index, b.c) == (1, 104)


def test_build_stream_keeps_valid_regular_bars_in_order():
    reg = {D2: [Bar(570, 1, 1, 1, 1, 1)], D1: [Bar(571, 2, 2, 2, 2, 1), Bar(570, 2, 1.5, 2.5, 2, 1),  # low > high
                                               Bar(565, 2, 2, 2, 2, 1), Bar(959, 3, 3, 3, 3, 1)]}
    s = build_stream(reg)
    assert [(x.day, x.minute) for x in s] == [(D1, 571), (D1, 959), (D2, 570)]


# ---------------------------------------------------------------- entries
def test_long_entry_fills_next_open_with_slippage_and_sizing():
    eng = ready_engine()
    eng._on_short(short_bucket(D1, 2, 100.0, 100.5))  # bar 09:40~09:44 up, aligned
    assert eng.pending_entry[:2] == (1, "normal")
    eng.step(m(D1, 585, 100.6, c=100.6), False)
    tr = eng.pos.trade
    assert (tr.side, tr.entry_minute, tr.entry_raw) == (1, 585, 100.6)
    assert tr.entry_fill == pytest.approx(100.62) and tr.shares == round(2000 * 0.95 / 100.62, 4)
    assert eng.cycle_entries == 1


@pytest.mark.parametrize("direction, sign, o, c, want", [(1, 1, 100, 99, None), (1, -1, 100, 101, None),
                                                         (-1, -1, 100, 99, (-1, "normal")), (-1, -1, 100, 101, None)])
def test_entry_conditions(direction, sign, o, c, want):
    eng = ready_engine(direction, sign)
    eng.ema9.value, eng.ema21.value = (101.0, 100.0) if sign > 0 else (99.0, 100.0)
    eng._on_short(short_bucket(D1, 2, o, c))
    assert (eng.pending_entry[:2] if eng.pending_entry else None) == want


def test_no_entry_without_volatility_or_before_575_or_on_last_bar_or_before_start():
    eng = ready_engine(vol=False)
    eng._on_short(short_bucket(D1, 2, 100, 101))
    assert eng.pending_entry is None
    for minute, day in ((574, D1), (959, D1), (600, "2023-05-08")):
        eng = ready_engine()
        eng.pending_entry = (1, "normal", 599)
        eng.step(m(day, minute, 100), False)
        assert eng.pos is None


def test_short_entry_and_hard_stop_on_high():
    eng = ready_engine(-1, -1)
    eng.pending_entry = (-1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    tr = eng.pos.trade
    level = tr.entry_fill * 1.003
    eng.step(m(D1, 601, 100.0, h=level + 0.01, c=100.0), False)
    assert eng.pos is None and tr.exit_reason == "hard_stop"
    assert tr.legs[-1].raw == pytest.approx(level) and tr.legs[-1].fill == pytest.approx(level + 0.02)
    want = -1 * (level + 0.02 - tr.entry_fill) * tr.shares - 0.005 * 2 * tr.shares
    assert tr.net_pnl == pytest.approx(want)
    ref = -1 * (level - 100.0) * tr.shares - 0.001 * tr.shares * (100.0 + level)
    assert tr.ref_pnl == pytest.approx(ref)


def test_hard_stop_gap_fills_at_open():
    eng = ready_engine()
    eng.pending_entry = (1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    eng.step(m(D1, 601, 99.0, c=99.0), False)
    assert eng.trades[-1].legs[-1].raw == 99.0


# ---------------------------------------------------------------- gears
def _long_at_100(eng):
    eng.pending_entry = (1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    return eng.pos


def test_latch_on_close_then_fee_lock():
    eng = ready_engine()
    p = _long_at_100(eng)
    fill = p.trade.entry_fill
    eng.step(m(D1, 601, 100.1, h=fill * 1.005, c=fill * 1.0039), False)  # high above, close below latch
    assert not p.swing
    eng.step(m(D1, 602, fill * 1.0039, c=fill * 1.0041), False)
    assert p.swing
    eng._on_short(Bucket(D1, 6, 101, 101, 99, 99, 604))  # would be a reverse cross: ignored in gear 2
    eng.ema9.value, eng.ema21.value = 99.0, 100.0
    eng._on_short(Bucket(D1, 7, 101, 101, 99, 99, 609))
    assert eng.pending_exit is None
    level = fill * 1.0022
    eng.step(m(D1, 603, fill * 1.003, low=level - 0.01, c=fill * 1.003), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.swing) == ("fee_lock", True) and tr.legs[-1].raw == pytest.approx(level)


def test_gear1_reverse_cross_exit_and_eod_exit():
    eng = ready_engine()
    _long_at_100(eng)
    eng.ema9.value, eng.ema21.value = 99.0, 100.0
    eng._on_short(Bucket(D1, 6, 100, 100, 100, 100, 604))
    assert eng.pending_exit == "ema_cross"
    eng.step(m(D1, 605, 100.05), False)
    assert eng.trades[-1].exit_reason == "ema_cross" and eng.last_exit_gear1
    eng2 = ready_engine()
    _long_at_100(eng2)
    eng2.step(m(D1, 959, 100.1), False)
    assert eng2.trades[-1].exit_reason == "eod_gear1" and eng2.trades[-1].legs[-1].raw == 100.1


def test_swing_holds_overnight_and_flip_switches():
    eng = ready_engine()
    p = _long_at_100(eng)
    p.swing = True
    eng.step(m(D1, 959, 101.0), False)
    assert eng.pos is p
    eng.st_dir = 1
    eng.st.dir, eng.st.upper, eng.st.lower = 1, 110.0, 100.0
    eng.st.atr.value, eng.st.atr.prev_close, eng.st.prev_close = 1.0, 101.0, 101.0
    eng.atr14.value, eng.atr14.prev_close = 1.0, 101.0
    eng._on_30m(Bucket(D2, 0, 101, 101, 95, 95, 599))  # close below the lower band -> -1
    assert eng.st_dir == -1 and eng.pending_exit == "st_flip" and eng.pending_entry[:2] == (-1, "switch")
    assert eng.cycle_entries == 0
    eng.step(m(D2, 600, 95.0), False)
    assert eng.trades[-1].exit_reason == "st_flip" and eng.pos.trade.side == -1 and eng.pos.trade.kind == "switch"


def test_flip_without_volatility_exits_only():
    eng = ready_engine()
    _long_at_100(eng).swing = True
    eng.st.dir, eng.st.upper, eng.st.lower = 1, 110.0, 100.0
    eng.st.atr.value, eng.st.atr.prev_close, eng.st.prev_close = 0.01, 101.0, 101.0
    eng.atr14.value, eng.atr14.prev_close = 0.01, 101.0
    # narrow bar (ATR14 stays ~0.09% of price < 0.18%) closing below the 100 lower band
    eng._on_30m(Bucket(D1, 1, 100.0, 100.05, 99.95, 99.96, 629))
    assert eng.st_dir == -1 and not eng.vol_ok
    assert eng.pending_exit == "st_flip" and eng.pending_entry is None


def test_cooldown_needs_15_minutes_and_fresh_cross():
    eng = ready_engine()
    _long_at_100(eng)
    eng.step(m(D1, 601, 99.0), False)  # hard stop at 10:01
    assert eng.last_exit_gear1
    eng._on_short(short_bucket(D1, 7, 100, 101))  # 10:05~10:09 bucket ends 10:10: < 15 min, no fresh cross
    assert eng.pending_entry is None and eng.counts["cooldown"] == 1
    eng._on_short(short_bucket(D1, 9, 100, 101))  # ends 10:20: >= 15 min but still no fresh cross
    assert eng.pending_entry is None
    eng.ema9.value, eng.ema21.value = 99.0, 100.0
    eng._on_short(short_bucket(D1, 10, 100, 99.0))  # cross down
    eng.ema9.value, eng.ema21.value = 101.0, 100.0
    eng._on_short(short_bucket(D1, 11, 100, 101))  # fresh cross up at 10:30
    assert eng.pending_entry[:2] == (1, "normal")


def test_cycle_cap_two_entries():
    eng = ready_engine()
    for minute in (600, 602):  # same 30m bucket (10:00~10:29): no real 30m bar completes in between
        eng.pending_entry = (1, "normal", 599)
        eng.step(m(D1, minute, 100.0), False)
        eng.pending_exit = "x"
        eng.step(m(D1, minute + 1, 100.0), False)
    eng.last_exit_gear1 = False
    eng.vol_ok, eng.st_dir = True, 1  # the steps above completed a real (unwarmed) 30m bucket
    eng._on_short(short_bucket(D1, 30, 100, 101))
    assert eng.pending_entry is None and eng.counts["cycle_cap"] == 1


def test_gear1_missing_last_bar_exits_next_day_open():
    eng = ready_engine()
    _long_at_100(eng)
    eng.step(m(D1, 700, 100.1), False)  # day's data ends early
    eng.step(m(D2, 570, 100.3), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.exit_day, tr.legs[-1].raw) == ("gap_eod", D2, 100.3)


def test_30m_value_only_after_bucket_completes():
    eng = ready_engine()
    calls = []
    eng._on_30m = lambda b: calls.append((b.index, b.last_minute))
    for minute in range(570, 600):
        eng.step(m(D1, minute, 100.0), False)
        assert calls == ([] if minute < 599 else [(0, 599)])


# ---------------------------------------------------------------- run / CLI
def test_run_day_records_and_end_of_data():
    stream = [m(D1, 600, 100.0), m(D1, 959, 101.0), m(D3, 570, 102.0), m(D3, 959, 103.0)]
    eng_cfg = CFG
    res = E.run(stream, eng_cfg, [D1, D2, D3])
    assert [d.status for d in res.days] == ["flat", "no_data", "flat"]
    assert res.trades == [] and res.excluded_symbol_days == {"QQQ": 1}


def test_mark_day_values_open_position():
    eng = ready_engine()
    p = _long_at_100(eng)
    p.swing = True
    eng.mark_day(m(D1, 959, 101.0))
    mtm, ref, entries, holding = eng.day_end[D1]
    tr = p.trade
    assert mtm == pytest.approx(2000 + (101.0 - tr.entry_fill) * tr.shares - 0.005 * tr.shares)
    assert holding and entries == 1
    assert ref == pytest.approx(2000 + (101.0 - 100.0) * tr.shares - 0.001 * tr.shares * 100.0)


def test_cli_writes_all_files_once(tmp_path):
    cli = _load_script("explore_v3_asym_swing")
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(3):
            _write_day(store, "QQQ", d1 + timedelta(days=k), p=100.0 + k)
        store.commit()
    out = tmp_path / "out"
    md = cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))
    names = {p.name for p in out.iterdir()}
    assert "EXPLORE_V3_ASYM_SWING.json" in names and len(names) == 4 and "Main table" in md
    doc = json.loads((out / "EXPLORE_V3_ASYM_SWING.json").read_text())
    assert set(doc["configs"]) == {c.name for c in E.CONFIGS}
    assert doc["provenance"]["spec_path"] == "docs/research/EXPLORE_V3_ASYM_SWING_2000USD.md"
    with pytest.raises(SystemExit):
        cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))


# ---------------------------------------------------------------- review follow-ups
def test_stale_pending_entry_dropped_when_late_30m_turns_against_it():
    eng = ready_engine()
    eng.pending_entry = (1, "normal", 594)
    eng.st_dir = -1  # what a late-completed 30m bar at this open would have set
    eng.step(m(D1, 600, 100.0), False)
    assert eng.pos is None and eng.counts["stale_signal"] == 1
    eng = ready_engine(vol=False)
    eng.pending_entry = (1, "normal", 594)
    eng.step(m(D1, 600, 100.0), False)
    assert eng.pos is None and eng.counts["stale_signal"] == 1


def _flat_swing_long(eng):
    p = _long_at_100(eng)
    p.swing = True
    eng.st.dir, eng.st.upper, eng.st.lower = -1, 100.5, 90.0  # already against the long, no flip on the next bar
    eng.st_dir = -1
    eng.st.atr.value, eng.st.atr.prev_close, eng.st.prev_close = 1.0, 99.0, 99.0
    eng.atr14.value, eng.atr14.prev_close = 1.0, 99.0
    return p


def test_opposite_direction_without_flip_exits_but_does_not_switch():
    eng = ready_engine()
    _flat_swing_long(eng)
    eng._on_30m(Bucket(D1, 2, 99.0, 99.2, 98.8, 99.0, 659))  # stays -1
    assert eng.st_dir == -1 and eng.pending_exit == "st_flip" and eng.pending_entry is None


def test_short_fee_lock_level_and_gap():
    eng = ready_engine(-1, -1)
    eng.pending_entry = (-1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    p = eng.pos
    p.swing = True
    fill = p.trade.entry_fill
    level = fill * (1 - 0.0022)
    eng.step(m(D1, 601, fill * 0.995, h=level + 0.01, c=fill * 0.995), False)
    tr = eng.trades[-1]
    assert tr.exit_reason == "fee_lock" and tr.legs[-1].raw == pytest.approx(level)
    assert tr.legs[-1].fill == pytest.approx(level + 0.02)
    eng = ready_engine(-1, -1)
    eng.pending_entry = (-1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    eng.pos.swing = True
    eng.step(m(D2, 570, 101.0), False)  # overnight gap above the fee-lock level
    assert eng.trades[-1].legs[-1].raw == 101.0


def test_short_mfe_latch_and_end_of_data_exit():
    eng = ready_engine(-1, -1)
    eng.pending_entry = (-1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    fill = eng.pos.trade.entry_fill
    eng.step(m(D1, 601, fill * 0.996, c=fill * 0.9959), False)  # short MFE 0.41% on the close
    assert eng.pos.swing
    eng.step(m(D1, 959, fill * 0.996), False)  # swing: held over the day's last bar
    assert eng.pos is not None
    eng.step(m(D2, 600, fill * 0.995), True)
    tr = eng.trades[-1]
    assert tr.exit_reason == "end_of_data" and tr.swing and tr.legs[-1].raw == pytest.approx(fill * 0.995)


def test_real_supertrend_flip_increments_cycle_and_switches():
    eng = ready_engine()
    p = _long_at_100(eng)
    p.swing = True
    eng.cycle_entries = 2
    st = eng.st
    st.dir, st.upper, st.lower = 1, 110.0, 100.0
    st.atr.value, st.atr.prev_close, st.prev_close = 1.0, 101.0, 101.0
    eng.atr14.value, eng.atr14.prev_close = 1.0, 101.0
    cyc = eng.cycle
    eng._on_30m(Bucket(D1, 1, 101, 101, 95, 95, 629))
    assert eng.cycle == cyc + 1 and eng.cycle_entries == 0 and eng.pending_entry[:2] == (-1, "switch")
    eng.step(m(D1, 630, 95.0), False)
    assert eng.pos.trade.side == -1 and eng.pos.trade.cycle == cyc + 1 and eng.cycle_entries == 1
    assert eng.pos.trade.signal_minute == 629


def test_switch_at_next_day_open_dropped_by_opening_rule():
    eng = ready_engine()
    p = _long_at_100(eng)
    p.swing = True
    eng.pending_exit, eng.pending_entry = "st_flip", (-1, "switch", 959)
    eng.step(m(D2, 570, 99.0), False)
    assert eng.trades[-1].exit_reason == "st_flip" and eng.pos is None and eng.counts["opening_5min"] == 1
