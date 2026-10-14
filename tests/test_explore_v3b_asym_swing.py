"""V3B corrections of the asymmetric trend switching engine (docs/research/EXPLORE_V3B_ASYM_SWING_2000USD.md).
Synthetic data only."""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.explore_v3_asym_swing import engine as E
from src.explore_v3_asym_swing.engine_v3b import CONFIGS_B, ConfigB, EngineB
from src.explore_v3_asym_swing.indicators import M1, Bucket
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import _write_day
from tests.test_explore_pullback_vwap_v1 import _load_script

D1, D2 = "2024-03-04", "2024-03-05"
CFG = ConfigB("T", 14, 3.0, 0.0028, 1, 0.0060, 0.0045, 0.0050, 0.0000, 0.0120, 0.0035)


def m(day, minute, o, h=None, low=None, c=None):
    c = o if c is None else c
    return M1(day, minute, o, max(o, c) if h is None else h, min(o, c) if low is None else low, c, 1000.0)


def eng_ready(direction=1, sign=1, cfg=CFG):
    eng = EngineB(cfg)
    eng.st_dir, eng.vol_ok, eng.sign = direction, True, sign
    eng.ema9.value, eng.ema21.value = (100.4, 100.0) if sign > 0 else (99.6, 100.0)
    return eng


def bucket15(idx, o, c, day=D1):
    return Bucket(day, idx, o, max(o, c), min(o, c), c, 570 + idx * 15 + 14)


def long_at_100(eng, day=D1, minute=600):
    eng.pending_entry = (1, "normal", minute - 1)
    eng.step(m(day, minute, 100.0), False)
    return eng.pos


# ---------------------------------------------------------------- presets
def test_presets():
    assert [c.name for c in CONFIGS_B] == ["V3B_BASE", "V3B_WIDE_HOLD", "V3B_STRICT_SELECT"]
    b, w, s = CONFIGS_B
    assert (b.st_period, b.st_mult, b.vol_min, b.max_entries, b.max_ema_dist, b.hard_stop) == (14, 3.0, 0.0028, 1, 0.006, 0.0045)
    assert (b.latch_1, b.lock_1, b.latch_2, b.lock_2) == (0.005, 0.0, 0.012, 0.0035)
    assert (w.st_mult, w.vol_min, w.hard_stop, w.latch_1, w.lock_1, w.latch_2, w.lock_2) == (3.5, 0.003, 0.005, 0.006, -0.001, 0.015, 0.004)
    assert (s.vol_min, s.max_ema_dist, s.hard_stop, s.latch_2) == (0.0034, 0.0035, 0.004, 0.011)
    assert all(c.short_tf == 15 for c in CONFIGS_B)


# ---------------------------------------------------------------- entries
def test_entry_needs_anti_chase_distance():
    eng = eng_ready()
    eng._on_short(bucket15(1, 100.0, 100.5))  # EMA21 ~100.02, distance ~0.48% <= 0.60%
    assert eng.pending_entry[:2] == (1, "normal")
    eng = eng_ready()
    eng._on_short(bucket15(1, 100.0, 101.0))  # distance ~0.95% > 0.60%
    assert eng.pending_entry is None and eng.counts["chase"] == 1


def test_vol_min_from_config_and_single_entry_per_cycle():
    eng = eng_ready()
    eng.atr14.value, eng.atr14.prev_close = 0.25, 100.0  # ATR/close ~0.25% < 0.28% after the update
    eng._on_30m(Bucket(D1, 1, 100.0, 100.1, 99.9, 100.0, 629))
    assert not eng.vol_ok
    eng = eng_ready()
    long_at_100(eng)
    eng.pending_exit = "x"
    eng.step(m(D1, 601, 100.0), False)
    eng.last_exit_gear1 = False
    eng._on_short(bucket15(3, 100.0, 100.3))
    assert eng.pending_entry is None and eng.counts["cycle_cap"] == 1


# ---------------------------------------------------------------- stages
def test_stage0_holds_overnight_and_exits_on_reverse_cross():
    eng = eng_ready()
    p = long_at_100(eng)
    eng.step(m(D1, 959, 100.1), False)
    assert eng.pos is p and p.stage == 0  # no end-of-day exit
    eng.ema9.value, eng.ema21.value = 99.6, 100.0
    eng._on_short(bucket15(0, 100.0, 99.9, day=D2))
    assert eng.pending_exit == "ema_cross"
    eng.step(m(D2, 585, 100.05), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.stage, tr.exit_day) == ("ema_cross", 0, D2)


def test_stage1_floor_and_cross_ignored():
    eng = eng_ready()
    p = long_at_100(eng)
    fill = p.trade.entry_fill
    eng.step(m(D1, 601, fill, c=fill * 1.0051), False)
    assert p.stage == 1 and p.swing
    eng.ema9.value, eng.ema21.value = 99.6, 100.0
    eng._on_short(bucket15(2, 100.0, 99.9))
    assert eng.pending_exit is None
    eng.step(m(D1, 602, fill * 1.003, low=fill - 0.01, c=fill * 1.003), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.stage) == ("lock_1", 1) and tr.legs[-1].raw == pytest.approx(fill)
    assert tr.legs[-1].fill == pytest.approx(fill - 0.02)


def test_wide_hold_negative_lock1():
    cfg = CONFIGS_B[1]
    eng = eng_ready(cfg=cfg)
    p = long_at_100(eng)
    fill = p.trade.entry_fill
    eng.step(m(D1, 601, fill, c=fill * 1.0061), False)
    assert p.stage == 1
    eng.step(m(D1, 602, fill, low=fill * 0.9995, c=fill), False)  # -0.05%: above the -0.10% floor
    assert eng.pos is p
    eng.step(m(D1, 603, fill, low=fill * 0.998, c=fill), False)
    assert eng.trades[-1].exit_reason == "lock_1" and eng.trades[-1].legs[-1].raw == pytest.approx(fill * 0.999)


def test_stage2_floor_and_direct_jump():
    eng = eng_ready()
    p = long_at_100(eng)
    fill = p.trade.entry_fill
    eng.step(m(D1, 601, fill, c=fill * 1.013), False)  # 0 -> 2 in one bar
    assert p.stage == 2
    eng.step(m(D2, 570, fill * 1.002, c=fill * 1.002), False)  # overnight gap below the +0.35% floor
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.stage) == ("lock_2", 2) and tr.legs[-1].raw == pytest.approx(fill * 1.002)


def test_st_flip_exit_only_without_switch_and_not_in_stage0():
    eng = eng_ready()
    p = long_at_100(eng)
    st = eng.st
    st.dir, st.upper, st.lower = 1, 110.0, 100.0
    st.atr.value, st.atr.prev_close, st.prev_close = 1.0, 101.0, 101.0
    eng.atr14.value, eng.atr14.prev_close = 1.0, 101.0
    eng._on_30m(Bucket(D1, 1, 101, 101, 95, 95, 629))
    assert eng.st_dir == -1 and eng.pending_exit is None  # stage 0: no flip exit
    p.stage = 1
    eng._on_30m(Bucket(D1, 2, 95, 95.5, 94.5, 95, 659))
    assert eng.pending_exit == "st_flip" and eng.pending_entry is None
    eng.step(m(D1, 660, 95.0), False)
    assert eng.trades[-1].exit_reason == "st_flip" and eng.pos is None


def test_hard_stop_stage0_and_stale_signal():
    eng = eng_ready()
    p = long_at_100(eng)
    level = p.trade.entry_fill * (1 - 0.0045)
    eng.step(m(D1, 601, 100.0, low=level - 0.01, c=100.0), False)
    assert eng.trades[-1].exit_reason == "hard_stop" and eng.trades[-1].legs[-1].raw == pytest.approx(level)
    eng = eng_ready()
    eng.pending_entry = (1, "normal", 599)
    eng.st_dir = -1
    eng.step(m(D1, 600, 100.0), False)
    assert eng.pos is None and eng.counts["stale_signal"] == 1


def test_end_of_data_and_run():
    stream = [m(D1, 600, 100.0), m(D1, 959, 101.0), m(D2, 600, 102.0)]
    res = E.run(stream, CFG, [D1, D2], engine_cls=EngineB)
    assert [d.status for d in res.days] == ["flat", "flat"] and res.trades == []
    eng = eng_ready()
    long_at_100(eng)
    eng.step(m(D1, 700, 100.1), True)
    assert eng.trades[-1].exit_reason == "end_of_data"


def test_v3a_run_default_engine_unchanged():
    stream = [m(D1, 600, 100.0), m(D1, 959, 101.0)]
    a = E.run(stream, E.CONFIGS[0], [D1])
    b = E.run(stream, E.CONFIGS[0], [D1], engine_cls=E.Engine)
    assert [vars(d) for d in a.days] == [vars(d) for d in b.days]


# ---------------------------------------------------------------- scripts
def _db(tmp_path):
    db = str(tmp_path / "m.sqlite")
    d1 = date(2024, 3, 4)
    with HistoryStore(db) as store:
        for k in range(3):
            _write_day(store, "QQQ", d1 + timedelta(days=k), p=100.0 + k)
        store.commit()
    return db, d1


def test_cli_writes_all_files_once(tmp_path):
    cli = _load_script("explore_v3b_asym_swing")
    db, d1 = _db(tmp_path)
    out = tmp_path / "out"
    md = cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))
    names = {p.name for p in out.iterdir()}
    assert "EXPLORE_V3B_ASYM_SWING.json" in names and len(names) == 4 and "Exit reasons" in md
    doc = json.loads((out / "EXPLORE_V3B_ASYM_SWING.json").read_text())
    assert set(doc["configs"]) == {c.name for c in CONFIGS_B}
    with pytest.raises(SystemExit):
        cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))


def test_group_stats_and_preflight_series():
    cli = _load_script("explore_v3b_asym_swing")
    eng = eng_ready()
    p = long_at_100(eng)
    p.stage = 1
    eng.step(m(D2, 700, 99.0), False)  # lock_1 at the open gap
    g = cli.group(eng.trades)
    assert g["trades"] == 1 and g["hold_calendar_days_mean"] == 1 and g["win_rate"] == 0.0
    pre = _load_script("explore_v3b_preflight")
    stream = [m("2024-03-04", t, 100.0 + (t % 7) * 0.01) for t in range(570, 960)]
    vols, dists = pre.series(stream)
    # one day = 13 30m bars (ATR14 never ready) and 26 15m bars (EMA21 ready from the 21st -> 6 distances)
    assert vols == [] and len(dists) == 6 and all(x >= 0 for x in dists)
