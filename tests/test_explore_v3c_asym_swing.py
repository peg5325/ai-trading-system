"""V3C corrections of the asymmetric trend swing engine (docs/research/EXPLORE_V3C_ASYM_SWING_2000USD.md).
Synthetic data only."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.explore_v3_asym_swing import engine as E
from src.explore_v3_asym_swing.engine_v3c import CONFIGS_C, ConfigC, EngineC
from src.explore_v3_asym_swing.indicators import M1, Bucket
from src.history.store import HistoryStore
from tests.test_explore_orb_vwap_v0 import _write_day
from tests.test_explore_pullback_vwap_v1 import _load_script

D1, D2 = "2024-03-04", "2024-03-05"
CFG = ConfigC("T", 14, 3.0, 0.0032, 1, 0.0060, 0.0040, 0.0045, 0.0, 0.0075, 0.0030, 0.0125, 0.0060, "bear_only")
TAC = replace(CFG, short_regime="tactical")
VOLC = replace(CFG, short_regime="bear_only_vol", short_vol_min=0.0042)


def m(day, minute, o, h=None, low=None, c=None):
    c = o if c is None else c
    return M1(day, minute, o, max(o, c) if h is None else h, min(o, c) if low is None else low, c, 1000.0)


def eng_ready(direction=1, sign=1, cfg=CFG, bear=False):
    """EMA9 100.1 / EMA21 100.0 (long) or 99.9 / 100.0 (short); previous 15m bar 99.8 .. 100.2."""
    eng = EngineC(cfg)
    eng.st_dir, eng.vol_ok, eng.sign, eng.bear = direction, True, sign, bear
    eng.atr_ratio = 0.005
    eng.ema9.value, eng.ema21.value = (100.1, 100.0) if sign > 0 else (99.9, 100.0)
    eng.prev15 = Bucket(D1, 0, 100.0, 100.2, 99.8, 100.0, 584)
    return eng


def bucket15(idx, o, c, day=D1, h=None, low=None):
    return Bucket(day, idx, o, max(o, c) if h is None else h, min(o, c) if low is None else low, c, 570 + idx * 15 + 14)


def enter(eng, side, day=D1, minute=600):
    eng.pending_entry = (side, "normal", minute - 1)
    eng.step(m(day, minute, 100.0), False)
    return eng.pos


# ---------------------------------------------------------------- presets
def test_presets():
    assert [c.name for c in CONFIGS_C] == ["V3C_REGIME_ASYM", "V3C_TACTICAL_SHORT", "V3C_STRICT_RUNNER"]
    r, t, s = CONFIGS_C
    assert all((c.st_period, c.st_mult, c.max_entries, c.hard_stop, c.short_tf) == (14, 3.0, 1, 0.004, 15) for c in CONFIGS_C)
    assert (r.vol_min, r.max_ema_dist, r.short_regime) == (0.0032, 0.004, "bear_only")
    assert (r.latch_1, r.lock_1, r.latch_2, r.lock_2, r.latch_3, r.lock_3) == (0.0045, 0.0, 0.0075, 0.003, 0.0125, 0.006)
    assert (t.vol_min, t.max_ema_dist, t.short_regime, t.latch_3, t.lock_3) == (0.0034, 0.0035, "tactical", 0.012, 0.0055)
    assert (s.vol_min, s.max_ema_dist, s.short_regime, s.short_vol_min) == (0.0035, 0.0035, "bear_only_vol", 0.0042)
    assert (s.latch_1, s.lock_1, s.latch_2, s.lock_2, s.latch_3, s.lock_3) == (0.005, 0.0005, 0.008, 0.0035, 0.013, 0.006)


# ---------------------------------------------------------------- rebound confirmation (spec §4.1)
def test_long_rebound_breakout_passes():
    eng = eng_ready()
    eng._on_short(bucket15(1, 100.0, 100.3))  # EMA9 100.1 -> 100.14 (rising), close > EMA9, close > prev high 100.2
    assert eng.pending_entry[:2] == (1, "normal")


@pytest.mark.parametrize("e9, prev_high, o, c", [
    (100.4, 100.2, 100.0, 100.3),   # EMA9 100.4 -> 100.38 falling (and close below EMA9)
    (100.1, 100.4, 100.0, 100.3),   # no breakout of the previous high, no fresh cross
    (100.25, 100.2, 100.0, 100.24),  # EMA9 rises to 100.248 but the close 100.24 is below it
])
def test_long_rebound_failures(e9, prev_high, o, c):
    eng = eng_ready()
    eng.ema9.value = e9
    eng.prev15 = Bucket(D1, 0, 100.0, prev_high, 99.8, 100.0, 584)
    eng._on_short(bucket15(1, o, c))
    assert eng.pending_entry is None and eng.counts["no_rebound_confirm"] == 1


def test_fresh_cross_replaces_breakout():
    eng = eng_ready()
    eng.sign = -1  # EMA9 was below EMA21 before this bar
    eng.ema9.value, eng.ema21.value = 99.98, 100.0
    eng.prev15 = Bucket(D1, 0, 100.0, 101.0, 99.8, 100.0, 584)  # previous high not broken
    eng._on_short(bucket15(1, 100.0, 100.3))  # EMA9 -> 100.044 > EMA21 -> 100.027: fresh golden cross
    assert eng.pending_entry[:2] == (1, "normal")


def test_short_rebound_mirror():
    eng = eng_ready(-1, -1, bear=True)
    eng._on_short(bucket15(1, 100.0, 99.7))  # EMA9 99.9 -> 99.86 falling, close < EMA9, close < prev low 99.8
    assert eng.pending_entry[:2] == (-1, "normal")
    eng = eng_ready(-1, -1, bear=True)
    eng._on_short(bucket15(1, 100.0, 99.85))  # close above the previous low 99.8 and EMA9 99.89 -> no
    assert eng.pending_entry is None and eng.counts["no_rebound_confirm"] == 1


# ---------------------------------------------------------------- regime (spec §4.2)
def test_bear_only_blocks_bull_regime_shorts_not_longs():
    eng = eng_ready(-1, -1, bear=False)
    eng._on_short(bucket15(1, 100.0, 99.7))
    assert eng.pending_entry is None and eng.counts["regime_short_block"] == 1
    eng = eng_ready(1, 1, bear=True)  # longs ignore the regime
    eng._on_short(bucket15(1, 100.0, 100.3))
    assert eng.pending_entry[:2] == (1, "normal")


def test_bear_only_vol_needs_atr_ratio():
    eng = eng_ready(-1, -1, cfg=VOLC, bear=True)
    eng.atr_ratio = 0.0041
    eng._on_short(bucket15(1, 100.0, 99.7))
    assert eng.pending_entry is None and eng.counts["regime_short_block"] == 1
    eng = eng_ready(-1, -1, cfg=VOLC, bear=True)
    eng.atr_ratio = 0.0042
    eng._on_short(bucket15(1, 100.0, 99.7))
    assert eng.pending_entry[:2] == (-1, "normal")


def test_regime_from_ema200_on_30m_close():
    eng = EngineC(CFG)
    eng.ema200.value = 100.0
    eng._on_30m(Bucket(D1, 1, 100.0, 100.0, 99.0, 99.5, 629))
    assert eng.bear
    eng._on_30m(Bucket(D1, 2, 100.0, 101.0, 100.0, 101.0, 659))
    assert not eng.bear
    fresh = EngineC(CFG)  # EMA200 not ready: counts as bull (shorts blocked)
    fresh._on_30m(Bucket(D1, 1, 100.0, 100.0, 99.0, 99.0, 629))
    assert not fresh.bear and not fresh._short_ok()


def test_pending_short_dropped_when_regime_turns_at_fill():
    eng = eng_ready(-1, -1, bear=True)
    eng.pending_entry = (-1, "normal", 599)
    eng.bear = False
    eng.step(m(D1, 600, 100.0), False)
    assert eng.pos is None and eng.counts["stale_signal"] == 1


def test_tactical_bull_short_closes_at_day_end_below_stage3():
    eng = eng_ready(-1, -1, cfg=TAC, bear=False)
    p = enter(eng, -1)
    assert p.bull_short and p.trade.bull_short
    eng.step(m(D1, 959, 99.8), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.exit_day, tr.legs[-1].raw, tr.bull_short) == ("eod_bull_short", D1, 99.8, True)


def test_tactical_stage3_and_bear_short_hold_overnight():
    eng = eng_ready(-1, -1, cfg=TAC, bear=False)
    p = enter(eng, -1)
    fill = p.trade.entry_fill
    eng.step(m(D1, 959, fill, c=fill * (1 - 0.0126)), False)  # promoted to Stage 3 at the last bar's close
    assert eng.pos is p and p.stage == 3
    eng = eng_ready(-1, -1, cfg=TAC, bear=True)
    p = enter(eng, -1)
    assert not p.bull_short
    eng.step(m(D1, 959, 100.0), False)
    assert eng.pos is p


def test_tactical_bull_short_missing_last_bar_exits_next_open():
    eng = eng_ready(-1, -1, cfg=TAC, bear=False)
    enter(eng, -1)
    eng.step(m(D1, 950, 100.0), False)  # 15:59 bar missing
    eng.step(m(D2, 570, 100.5), False)
    tr = eng.trades[-1]
    assert (tr.exit_reason, tr.exit_day, tr.legs[-1].raw) == ("gap_eod_bull_short", D2, 100.5)


# ---------------------------------------------------------------- three-stage latch (spec §4.3)
@pytest.mark.parametrize("close_mult, stage", [(1.0046, 1), (1.0076, 2), (1.0126, 3), (1.0124, 2)])
def test_stage_promotion_and_jump(close_mult, stage):
    eng = eng_ready()
    p = enter(eng, 1)
    fill = p.trade.entry_fill
    eng.step(m(D1, 601, fill, c=fill * close_mult), False)
    assert p.stage == stage and p.swing


@pytest.mark.parametrize("stage, lock, reason", [(1, 0.0, "lock_1"), (2, 0.003, "lock_2"), (3, 0.006, "lock_3")])
def test_stage_floors_long_and_short(stage, lock, reason):
    for side in (1, -1):
        eng = eng_ready(side, side, bear=True)
        p = enter(eng, side)
        p.stage, p.swing = stage, True
        fill = p.trade.entry_fill
        level = fill * (1 + side * lock)
        bar = (m(D1, 601, fill * 1.02, low=level - 0.001, c=fill * 1.02) if side > 0
               else m(D1, 601, fill * 0.98, h=level + 0.001, c=fill * 0.98))
        eng.step(bar, False)
        tr = eng.trades[-1]
        assert (tr.exit_reason, tr.stage) == (reason, stage)
        assert tr.legs[-1].raw == pytest.approx(level) and tr.legs[-1].fill == pytest.approx(level - side * 0.02)


def test_stage1_ignores_cross_stage0_does_not():
    eng = eng_ready()
    p = enter(eng, 1)
    eng.ema9.value, eng.ema21.value = 99.6, 100.0
    eng._on_short(bucket15(2, 100.0, 99.9))
    assert eng.pending_exit == "ema_cross"
    eng = eng_ready()
    p = enter(eng, 1)
    p.stage, p.swing = 3, True
    eng.ema9.value, eng.ema21.value = 99.6, 100.0
    eng._on_short(bucket15(2, 100.0, 99.9))
    assert eng.pending_exit is None


def test_stage3_st_flip_exit():
    eng = eng_ready()
    p = enter(eng, 1)
    p.stage, p.swing = 3, True
    st = eng.st
    st.dir, st.upper, st.lower = 1, 110.0, 100.0
    st.atr.value, st.atr.prev_close, st.prev_close = 1.0, 101.0, 101.0
    eng._on_30m(Bucket(D1, 1, 101, 101, 95, 95, 629))
    assert eng.pending_exit == "st_flip"
    eng.step(m(D1, 660, 95.0), False)
    assert eng.trades[-1].exit_reason == "st_flip" and eng.pending_entry is None


def test_v3b_engine_unchanged_by_hooks():
    from src.explore_v3_asym_swing.engine_v3b import CONFIGS_B, EngineB, V3BTrade
    eng = EngineB(CONFIGS_B[0])
    eng.st_dir, eng.vol_ok = 1, True
    eng.pending_entry = (1, "normal", 599)
    eng.step(m(D1, 600, 100.0), False)
    assert type(eng.pos.trade) is V3BTrade


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
    cli = _load_script("explore_v3c_asym_swing")
    db, d1 = _db(tmp_path)
    out = tmp_path / "out"
    md = cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))
    names = {p.name for p in out.iterdir()}
    assert "EXPLORE_V3C_ASYM_SWING.json" in names and len(names) == 4
    assert "Exit reasons" in md and "no_rebound_confirm" in md and "gear2_stage3" in md
    doc = json.loads((out / "EXPLORE_V3C_ASYM_SWING.json").read_text())
    assert set(doc["configs"]) == {c.name for c in CONFIGS_C}
    one = json.loads((out / "EXPLORE_V3C_ASYM_SWING_V3C_TACTICAL_SHORT.json").read_text())
    assert set(one["stages"]) == {"gear1", "gear2_stage1", "gear2_stage2", "gear2_stage3"}
    assert {"eod_bull_short", "lock_3"} <= set(one["exits"]) and set(one["signal_skips"]) == {
        "chase", "no_rebound_confirm", "regime_short_block", "cycle_cap"}
    with pytest.raises(SystemExit):
        cli.run_all(db, out, start=d1, end=d1 + timedelta(days=2))


def test_group_stats_and_preflight_series():
    cli = _load_script("explore_v3c_asym_swing")
    eng = eng_ready()
    p = enter(eng, 1)
    p.stage = 3
    eng.step(m(D2, 700, 99.0), False)  # lock_3 at the open gap
    g = cli.group(eng.trades)
    assert g["trades"] == 1 and g["hold_calendar_days_mean"] == 1 and g["win_rate"] == 0.0
    assert eng.trades[-1].exit_reason == "lock_3"
    pre = _load_script("explore_v3c_preflight")
    stream = [m("2024-03-04", t, 100.0 + (t % 7) * 0.01) for t in range(570, 960)]
    s = pre.series(stream)
    # one day = 13 30m bars (EMA200 never ready) and 26 15m bars (EMA21 ready from the 21st -> 6 values)
    assert s["vol"] == [] and len(s["dist"]) == 6 and len(s["reb_long"]) == len(s["reb_short"]) == 6


def test_run_end_to_end_flat():
    stream = [m(D1, 600, 100.0), m(D1, 959, 101.0), m(D2, 600, 102.0)]
    res = E.run(stream, CFG, [D1, D2], engine_cls=EngineC)
    assert [d.status for d in res.days] == ["flat", "flat"] and res.trades == []
