"""Entry study v1 data tests (PREREG_DAYTRADE_ENTRY_STUDY_V1 §11): seal, mask, manifest binding, gate, pipeline.
Synthetic data and temporary databases only; no real price file is read."""
from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.daytrade_paper.core import Bar
from src.entry_study_v1 import devrun
from src.entry_study_v1 import gate as G
from src.entry_study_v1 import mask as M
from src.entry_study_v1.bars import DataMismatch, DevBoundError, StudyRefused
from src.entry_study_v1.cli import check_sha, require_dev_inputs
from src.entry_study_v1.config import EVENTS_HISTORY, load_cells, sha256_file
from src.entry_study_v1.data import (
    DEV_CUTOFF,
    DEV_SEGMENTS,
    OOS_END,
    OOS_EXCLUDED,
    OOS_SEGMENT,
    Segment,
    SymbolData,
    assert_dev_window,
    load_symbol,
)
from src.entry_study_v1.pipeline import pair_trades, verify_against_manifest
from src.history import exchange_calendar as xcal
from src.history.oos_guard import OOSLockError, research_load
from src.history.store import SCHEMA
from src.models import ET

CELLS = load_cells()


# ------------------------------------------------------------------ synthetic DB (sealed-range tests)


def _row(sym, d: date, label: int, p: float):
    hh, mm = divmod(label, 60)
    et = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
    return (sym, "US", "1m", et.astimezone(timezone.utc).isoformat(), et.isoformat(), "IGNORED",
            p, p + 0.1, p - 0.1, p, 1000.0, "USD", 1, "test", "x", "x")


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})", rows)
    con.commit()
    con.close()
    return str(path)


@pytest.fixture()
def db(tmp_path):
    rows = [_row("SPY", date(2025, 12, 15), 10 * 60, 100.0), _row("SPY", date(2026, 1, 20), 10 * 60, 100.0),
            _row("SPY", date(2026, 3, 16), 10 * 60, 100.0), _row("SPY", date(2026, 9, 1), 10 * 60, 100.0)]
    return make_db(tmp_path / "h.sqlite", rows)


# ------------------------------------------------------------------ seal


def test_guard_raises_when_a_performance_load_overlaps_the_seal(db):
    with pytest.raises(OOSLockError):
        research_load(db, "SPY", "1m", start=datetime(2026, 1, 21, tzinfo=ET), end=datetime(2026, 7, 22, tzinfo=ET),
                      purpose="performance")
    with pytest.raises(OOSLockError):  # a window that only touches the first sealed day also raises
        research_load(db, "SPY", "1m", start=datetime(2026, 1, 20, tzinfo=ET), end=datetime(2026, 1, 21, 1, tzinfo=ET),
                      purpose="performance")
    assert research_load(db, "SPY", "1m", start=datetime(2026, 1, 21, tzinfo=ET), end=datetime(2026, 7, 22, tzinfo=ET),
                         purpose="coverage")  # coverage reads stay possible


def test_dev_upper_bound(db):
    assert DEV_CUTOFF == datetime(2026, 1, 21, tzinfo=ET)
    assert_dev_window(datetime(2026, 1, 20, tzinfo=ET), DEV_CUTOFF)  # [.., cutoff) is fine
    with pytest.raises(DevBoundError):
        assert_dev_window(datetime(2026, 1, 20, tzinfo=ET), DEV_CUTOFF + timedelta(minutes=1))
    with pytest.raises(DevBoundError):
        assert_dev_window(DEV_CUTOFF, DEV_CUTOFF + timedelta(days=1))
    with pytest.raises(DevBoundError):  # after the seal, which the guard itself does not block
        assert_dev_window(datetime(2026, 7, 22, tzinfo=ET), datetime(2026, 8, 1, tzinfo=ET))
    # the loader refuses before reading anything, for both purposes
    for purpose in ("performance", "coverage"):
        with pytest.raises(DevBoundError):
            load_symbol(db, "SPY", date(2026, 1, 20), date(2026, 1, 21), purpose=purpose)
        with pytest.raises(DevBoundError):
            load_symbol(db, "SPY", date(2026, 9, 1), date(2026, 9, 2), purpose=purpose)
    assert load_symbol(db, "SPY", date(2025, 12, 1), date(2025, 12, 31), purpose="performance").row_count == 1
    for seg in DEV_SEGMENTS:
        assert seg.bounds()[1] <= DEV_CUTOFF


def test_oos_loader_still_hits_the_seal_without_a_grant(db):
    with pytest.raises(OOSLockError):
        load_symbol(db, "SPY", date(2026, 3, 1), date(2026, 3, 31), purpose="performance", dev=False)
    got = load_symbol(db, "SPY", date(2026, 3, 1), date(2026, 3, 31), purpose="coverage", dev=False)
    assert got.row_count == 1
    # after the seal, a performance read is not blocked by the guard - which is why dev code enforces its bound
    assert load_symbol(db, "SPY", date(2026, 9, 1), date(2026, 9, 2), purpose="performance",
                       dev=False).row_count == 1


def test_loader_rejects_bad_purpose(db):
    with pytest.raises(ValueError):
        load_symbol(db, "SPY", date(2025, 12, 1), date(2025, 12, 31), purpose="other")


# ------------------------------------------------------------------ synthetic sessions / mask

OPEN, CLOSE = 570, 960


def synth_cal(start="2023-04-01", end="2026-12-31", extra=()):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    events = [{"date": d.isoformat(), "type": "EARLY_CLOSE"} for d in xcal.early_close_days(d0, d1)]
    return {"version": "t", "coverage": {"start": start, "end": end}, "events": events + list(extra)}


def day_bars(price=100.0, close_min=CLOSE):
    return [Bar(m, price, price + 0.1, price - 0.1, price, 1000.0) for m in range(OPEN, close_min)]


def synth_data(seg: Segment, symbols=("REF", "L", "S"), price_fn=None):
    """Flat sessions (every NYSE session of the data segment) for each symbol."""
    out = {}
    for sym in symbols:
        sd = SymbolData(sym)
        for d in xcal.trading_days(seg.data_start, seg.data_end):
            iso = d.isoformat()
            p = price_fn(sym, iso) if price_fn else 100.0
            sd.regular[iso] = day_bars(p, xcal.regular_close_minute(d))
            sd.pre[iso] = [Bar(480, p, p, p, p, 100.0)]
            sd.labels[iso] = [b.t + 1 for b in sd.regular[iso]]
        out[sym] = sd
    return out


SEG = Segment("T", date(2024, 9, 3), date(2024, 12, 31), date(2024, 10, 15), date(2024, 12, 31))
ETFS = ("L", "S")


def seg_mask(data, cal=None, excluded=(), seg=SEG):
    return M.build_segment_mask(seg, data, "REF", ETFS, cal or synth_cal(), excluded)


def excluded_reasons(m):
    return {e["date"]: e["reasons"] for e in m["excluded"]}


def test_mask_baseline_scored_and_early_closes_excluded():
    data = synth_data(SEG)
    m = seg_mask(data)
    ex = excluded_reasons(m)
    assert ex == {"2024-11-29": ["early_close"], "2024-12-24": ["early_close"]}
    sessions = xcal.trading_days(SEG.score_start, SEG.score_end)
    assert m["scored_count"] == len(sessions) - 2 and m["normal_sessions"] == len(sessions) - 2
    assert "2024-11-28" not in m["scored"]  # Thanksgiving is not an NYSE session


def test_mask_excludes_389_bars_and_duplicates():
    data = synth_data(SEG)
    data["L"].regular["2024-10-22"] = data["L"].regular["2024-10-22"][:-1]  # 389 bars
    dup = data["S"].regular["2024-10-23"]
    data["S"].regular["2024-10-23"] = dup[:-1] + [dup[0]]  # 390 rows, 389 unique
    data["REF"].regular["2024-10-24"] = data["REF"].regular["2024-10-24"] + [dup[5]]  # 391 rows
    ex = excluded_reasons(seg_mask(data))
    assert ex["2024-10-22"][0].startswith("incomplete_session:L:389")
    assert ex["2024-10-23"][0].startswith("incomplete_session:S:390 bars, 389 unique")
    assert ex["2024-10-24"][0].startswith("incomplete_session:REF:391")


def test_mask_previous_session_gap_and_pc_bar():
    data = synth_data(SEG)
    del data["REF"].regular["2024-10-29"]  # prior session absent -> the next day has no PC bar
    ex = excluded_reasons(seg_mask(data))
    assert "pc_bar_missing:2024-10-29" in ex["2024-10-30"]
    assert "2024-10-29" in ex  # and the empty day itself is incomplete
    data = synth_data(SEG)
    data["REF"].regular["2024-11-05"] = [b for b in data["REF"].regular["2024-11-05"] if b.t != 959]
    ex = excluded_reasons(seg_mask(data))
    assert "pc_bar_missing:2024-11-05" in ex["2024-11-06"]
    # early-close previous session: the PC bar is 12:59 (t = 779)
    data = synth_data(SEG)
    assert "2024-12-02" not in excluded_reasons(seg_mask(data))
    data["REF"].regular["2024-11-29"] = [b for b in data["REF"].regular["2024-11-29"] if b.t != 779]
    assert "pc_bar_missing:2024-11-29" in excluded_reasons(seg_mask(data))["2024-12-02"]


def test_mask_prev_session_outside_segment():
    seg = Segment("T", date(2024, 10, 14), date(2024, 12, 31), date(2024, 10, 14), date(2024, 10, 18))
    data = synth_data(seg)
    ex = excluded_reasons(seg_mask(data, seg=seg))
    assert any(r.startswith("prev_session_outside_segment") for r in ex["2024-10-14"])
    assert "sigma_sessions_short" in ex["2024-10-14"]


def test_mask_sigma_sessions_must_lie_in_segment():
    seg = Segment("T", date(2024, 10, 1), date(2024, 12, 31), date(2024, 10, 1), date(2024, 12, 31))
    ex = excluded_reasons(seg_mask(synth_data(seg), seg=seg))
    # first day with 14 normal sessions behind it inside the segment: 2024-10-21
    assert "2024-10-18" in ex and "sigma_sessions_short" in ex["2024-10-18"]
    assert "2024-10-21" not in ex


def test_mask_sigma_session_without_open_bar_is_not_a_mask_exclusion():
    data = synth_data(SEG)
    data["REF"].regular["2024-10-30"] = [b for b in data["REF"].regular["2024-10-30"] if b.t != OPEN]
    ex = excluded_reasons(seg_mask(data))
    # the session itself is a ref bar short, but sigma-session gaps only make sigma undefined at run time
    assert "2024-10-31" not in ex and "2024-11-04" not in ex
    assert any(r.startswith("incomplete_session:REF") for r in ex["2024-10-30"])


def test_mask_bad_values_in_day_pc_and_sigma_sessions():
    base = synth_data(SEG)
    assert M.bars_valid(base["REF"].regular["2024-10-22"])
    # bad sigma-session value excludes the days that use that session (and only those)
    data = synth_data(SEG)
    bad = list(data["REF"].regular["2024-10-17"])
    bad[100] = Bar(bad[100].t, -1.0, 1.0, 1.0, 1.0, 1.0)
    data["REF"].regular["2024-10-17"] = bad
    ex = excluded_reasons(seg_mask(data))
    assert ex["2024-10-18"] == ["bad_value"]  # PC session
    assert ex["2024-10-25"] == ["bad_value"]  # inside the 14-session window
    assert "2024-11-08" not in ex  # 15 sessions later: out of the window
    # NaN price / negative volume on the scored day
    data = synth_data(SEG)
    b = data["L"].regular["2024-11-12"]
    b[3] = Bar(b[3].t, float("nan"), 1.0, 1.0, 1.0, 1.0)
    data["S"].regular["2024-11-13"][4] = Bar(OPEN + 4, 1.0, 1.0, 1.0, 1.0, -1.0)
    ex = excluded_reasons(seg_mask(data))
    assert ex["2024-11-12"] == ["bad_value"] and ex["2024-11-13"] == ["bad_value"]


def test_mask_ref_gap_over_15_percent():
    def prices(sym, iso):
        return 120.0 if iso == "2024-11-19" and sym == "REF" else 100.0
    data = synth_data(SEG, price_fn=prices)
    ex = excluded_reasons(seg_mask(data))
    assert ex["2024-11-19"][0].startswith("ref_gap_gt_15pct")
    assert ex["2024-11-20"][0].startswith("ref_gap_gt_15pct")  # 120 -> 100 the next morning
    assert "2024-11-18" not in ex
    # the filter looks at the reference only, not the ETFs
    data = synth_data(SEG, price_fn=lambda sym, iso: 150.0 if sym == "L" and iso == "2024-11-19" else 100.0)
    assert "2024-11-19" not in excluded_reasons(seg_mask(data))


def test_mask_is_deterministic_and_hash_stable():
    pairs = {"P": ("REF", "L", "S")}
    a = M.build_mask(pairs, {"T": synth_data(SEG)}, (SEG,), synth_cal(), "0" * 64)
    b = M.build_mask(pairs, {"T": synth_data(SEG)}, (SEG,), synth_cal(), "0" * 64)
    assert M.canonical_bytes(a) == M.canonical_bytes(b)
    assert M.sha256_bytes(M.canonical_bytes(a)) == M.sha256_bytes(M.canonical_bytes(b))
    c = M.build_mask(pairs, {"T": synth_data(SEG, price_fn=lambda s, i: 100.0 if i != "2024-10-22" else 101.0)},
                     (SEG,), synth_cal(), "0" * 64)
    assert M.canonical_bytes(c) == M.canonical_bytes(a)  # prices never enter the mask unless a rule trips


def test_mask_marks_event_days_and_refuses_outside_calendar():
    cal = synth_cal(extra=[{"date": "2024-11-07", "type": "FOMC"}, {"date": "2024-11-13", "type": "CPI"},
                           {"date": "2024-11-14", "type": "PPI"}])
    m = seg_mask(synth_data(SEG), cal)
    assert m["event_days"] == {"2024-11-07": ["FOMC"], "2024-11-13": ["CPI"]}
    with pytest.raises(M.MaskRefused):
        seg_mask(synth_data(SEG), synth_cal(end="2024-12-15"))
    bad = synth_cal()
    bad["events"] = [e for e in bad["events"] if e["date"] != "2024-12-24"]  # calendar disagrees about early close
    with pytest.raises(M.MaskRefused):
        seg_mask(synth_data(SEG), bad)


def test_oos_mask_excludes_the_paper_week():
    assert OOS_EXCLUDED == tuple(date(2026, 10, d) for d in (5, 6, 7, 8, 9)) and OOS_END == date(2026, 10, 9)
    assert OOS_SEGMENT.score_start == date(2026, 1, 21) and OOS_SEGMENT.data_start == DEV_SEGMENTS[1].data_start
    seg = Segment("OOS", date(2026, 8, 3), date(2026, 10, 9), date(2026, 9, 21), date(2026, 10, 9))
    m = seg_mask(synth_data(seg), seg=seg, excluded=OOS_EXCLUDED)
    ex = excluded_reasons(m)
    for d in OOS_EXCLUDED:
        assert ex[d.isoformat()] == ["paper_week_already_seen"]
    assert "2026-10-02" in m["scored"] and "2026-10-05" not in m["scored"]
    # the OOS gate denominators skip them too
    assert G.normal_sessions(seg, OOS_EXCLUDED)[-1] == date(2026, 10, 2)


# ------------------------------------------------------------------ gate


def test_gate_g1_g3_g4():
    data = synth_data(SEG)
    syms = ("REF", "L", "S")
    r = G.g1(SEG, data, syms)
    assert r["ratio"] == 1.0 and r["pass"]
    for i, d in enumerate(xcal.trading_days(SEG.score_start, SEG.score_end)):  # break 6 of ~50 days
        if i < 6 and not xcal.is_early_close(d):
            data["L"].regular[d.isoformat()] = data["L"].regular[d.isoformat()][:-1]
    r = G.g1(SEG, data, syms)
    assert not r["pass"] and r["ratio"] < G.G1_MIN
    data = synth_data(SEG)
    assert G.g3(SEG, data, syms)["pass"]
    data["S"].labels["2024-10-22"] = [x + 1 for x in data["S"].labels["2024-10-22"]]  # shifted labels
    g3 = G.g3(SEG, data, syms)
    assert not g3["pass"] and g3["violation_list"] == ["2024-10-22:S"]


def _walk(seed, n):
    rng = random.Random(seed)
    p, out = 100.0, []
    for _ in range(n):
        p *= 1 + rng.gauss(0, 0.001)
        out.append(p)
    return out


def test_gate_g4_alignment():
    seg = Segment("T", date(2024, 10, 1), date(2024, 10, 31), date(2024, 10, 1), date(2024, 10, 31))
    ref = synth_data(seg, ("REF",))["REF"]
    long_etf, inverse, shifted = SymbolData("L"), SymbolData("S"), SymbolData("X")
    for k, d in enumerate(xcal.trading_days(seg.data_start, seg.data_end)):
        iso = d.isoformat()
        walk = _walk(k, 390)
        rb = [Bar(OPEN + i, walk[i], walk[i], walk[i], walk[i], 1.0) for i in range(390)]
        ref.regular[iso] = rb
        for sd, f in ((long_etf, lambda p: 3 * p), (inverse, lambda p: 500.0 / p), (shifted, None)):
            if f is None:  # lags the reference by one minute
                bars = [Bar(OPEN + i, rb[max(i - 1, 0)].close, 1, 1, rb[max(i - 1, 0)].close, 1.0)
                        for i in range(390)]
            else:
                bars = [Bar(b.t, f(b.close), 1, 1, f(b.close), 1.0) for b in rb]
            sd.regular[iso] = bars
            sd.labels[iso] = [b.t + 1 for b in bars]
        ref.labels[iso] = [b.t + 1 for b in rb]
    data = {"T": {"REF": ref, "L": long_etf, "S": inverse, "X": shifted}}
    assert G.g4((seg,), data, "REF", "L", False)["pass"]
    assert G.g4((seg,), data, "REF", "S", True)["pass"]  # inverse: absolute value
    assert not G.g4((seg,), data, "REF", "S", False)["pass"]  # negative correlation fails the long test
    assert not G.g4((seg,), data, "REF", "X", False)["pass"]  # the ETF lags: lag -1 is larger than lag 0


# ------------------------------------------------------------------ manifest binding


def test_data_hash_mismatch_refuses(tmp_path):
    rows = [_row("SPY", date(2025, 12, d), 10 * 60 + d, 100.0 + d) for d in (1, 2, 3)]
    path = tmp_path / "h.sqlite"
    db = make_db(path, rows)
    seg = Segment("A", date(2025, 12, 1), date(2025, 12, 31), date(2025, 12, 1), date(2025, 12, 31))
    seg_data = {"A": {"SPY": load_symbol(db, "SPY", seg.data_start, seg.data_end, purpose="performance")}}
    manifest = M.build_manifest(seg_data, (seg,))
    verify_against_manifest(seg_data, manifest)  # identical rows pass
    con = sqlite3.connect(path)
    con.execute("UPDATE candle_history SET close = close + 0.01 WHERE close = 101.0")  # one changed row
    con.commit()
    con.close()
    changed = {"A": {"SPY": load_symbol(db, "SPY", seg.data_start, seg.data_end, purpose="performance")}}
    with pytest.raises(DataMismatch):
        verify_against_manifest(changed, manifest)
    missing = {"rows": {}}
    with pytest.raises(DataMismatch):
        verify_against_manifest(seg_data, missing)
    con = sqlite3.connect(path)
    con.execute("DELETE FROM candle_history WHERE close = 102.0")  # a missing row changes the count
    con.commit()
    con.close()
    fewer = {"A": {"SPY": load_symbol(db, "SPY", seg.data_start, seg.data_end, purpose="performance")}}
    with pytest.raises(DataMismatch):
        verify_against_manifest(fewer, manifest)


def _fixed_files(tmp_path, events_sha=None):
    gate = tmp_path / "gate.json"
    mask = tmp_path / "mask.json"
    man = tmp_path / "manifest.json"
    gate.write_bytes(M.canonical_bytes({"pairs": {}}))
    man.write_bytes(M.canonical_bytes({"rows": {}}))
    mask.write_bytes(M.canonical_bytes({"pairs": {}, "events_calendar_sha256": events_sha or sha256_file(EVENTS_HISTORY),
                                        "gate_sha256": sha256_file(gate), "data_manifest_sha256": sha256_file(man)}))
    return gate, mask, man


def _args(tmp_path, **over):
    gate, mask, man = _fixed_files(tmp_path)
    base = {"gate": gate, "gate_sha256": sha256_file(gate), "mask": mask, "mask_sha256": sha256_file(mask),
            "manifest": man, "data_sha256": sha256_file(man), "allow_dirty": True, "i_have_user_approval": True}
    base.update(over)
    return argparse.Namespace(**base)


def test_dev_and_oos_refuse_without_approval_or_with_wrong_hashes(tmp_path):
    assert require_dev_inputs(_args(tmp_path))[0] == {"pairs": {}}
    with pytest.raises(StudyRefused):
        require_dev_inputs(_args(tmp_path, i_have_user_approval=False))
    with pytest.raises(DataMismatch):
        require_dev_inputs(_args(tmp_path, gate_sha256="0" * 64))
    with pytest.raises(DataMismatch):
        require_dev_inputs(_args(tmp_path, mask_sha256="0" * 64))
    with pytest.raises(DataMismatch):
        require_dev_inputs(_args(tmp_path, data_sha256="0" * 64))
    gate, mask, _man = _fixed_files(tmp_path, events_sha="f" * 64)
    with pytest.raises(DataMismatch):  # the event calendar changed after the mask was built
        require_dev_inputs(_args(tmp_path, mask=mask, mask_sha256=sha256_file(mask)))
    with pytest.raises(DataMismatch):
        check_sha(gate, "1" * 64, "gate")


def test_scripts_refuse_before_touching_data(tmp_path):
    from scripts import entry_study_v1_dev, entry_study_v1_oos
    gate, mask, man = _fixed_files(tmp_path)
    common = ["--db", str(tmp_path / "none.sqlite"), "--gate", str(gate), "--gate-sha256", sha256_file(gate),
              "--mask", str(mask), "--mask-sha256", sha256_file(mask), "--manifest", str(man),
              "--data-sha256", sha256_file(man), "--allow-dirty", "--out-dir", str(tmp_path / "out")]
    with pytest.raises(StudyRefused, match="approval"):
        entry_study_v1_dev.main(common)
    with pytest.raises(StudyRefused, match="approval"):
        entry_study_v1_oos.main(["run", *common, "--dev-result", "x", "--dev-result-sha256", "0" * 64,
                                 "--prepare", "p", "--prepare-sha256", "0" * 64, "--oos-mask", "m", "--oos-mask-sha256", "0" * 64,
                                 "--oos-snapshot", "s", "--oos-snapshot-sha256", "0" * 64,
                                 "--snapshot-manifest-path", "x", "--snapshot-hash-field", "y"])
    assert not (tmp_path / "out").exists()


# ------------------------------------------------------------------ pipeline on synthetic days


SEG_A = Segment("A", date(2024, 1, 2), date(2024, 3, 28), date(2024, 2, 26), date(2024, 3, 28))
SEG_B = Segment("B", date(2024, 4, 1), date(2024, 6, 28), date(2024, 5, 1), date(2024, 6, 28))
PAIR = {"ref": "QQQ", "long": "TQQQ", "short": "SQQQ"}


def random_data(seg, seed):
    rng = random.Random(seed)
    out = {s: SymbolData(s) for s in ("QQQ", "TQQQ", "SQQQ")}
    last = {"QQQ": 400.0, "TQQQ": 50.0, "SQQQ": 20.0}
    for d in xcal.trading_days(seg.data_start, seg.data_end):
        iso = d.isoformat()
        move = rng.gauss(0, 0.0007)
        for sym, sd in out.items():
            lev = {"QQQ": 1, "TQQQ": 3, "SQQQ": -3}[sym]
            bars = []
            p = last[sym]
            for m in range(OPEN, xcal.regular_close_minute(d)):
                o = p
                p = o * (1 + lev * (move + rng.gauss(0, 0.0003)))
                bars.append(Bar(m, o, max(o, p) * 1.0001, min(o, p) * 0.9999, p, 1000.0))
            last[sym] = p
            sd.regular[iso] = bars
            sd.pre[iso] = [Bar(500, bars[0].open, bars[0].open, bars[0].open, bars[0].open, 10.0)]
            sd.labels[iso] = [b.t + 1 for b in bars]
    return out


@pytest.fixture(scope="module")
def p2_trades():
    seg_data = {"A": random_data(SEG_A, 1), "B": random_data(SEG_B, 2)}
    cal = synth_cal()
    mask = {"ref": "QQQ", "long": "TQQQ", "short": "SQQQ", "segments": {
        s.name: M.build_segment_mask(s, seg_data[s.name], "QQQ", ("TQQQ", "SQQQ"), cal) for s in (SEG_A, SEG_B)}}
    res = pair_trades("P2", CELLS, mask, seg_data, (SEG_A, SEG_B))
    return mask, seg_data, res


def test_pipeline_trades_follow_rules(p2_trades):
    mask, seg_data, res = p2_trades
    cells = {c.k: c for c in CELLS if c.pair == "P2"}
    assert set(res["main"]) == set(cells)
    scored = sum(len(v["scored"]) for v in mask["segments"].values())
    assert scored > 60
    e1 = {c.side: res["main"][c.k] for c in cells.values() if c.rule == "E1"}
    # E1: at most one trade per day, long and short are exclusive, fill 09:35, exit 15:55 for the close hold
    days = [r.day for r in e1["long"]] + [r.day for r in e1["short"]]
    assert len(days) == len(set(days)) <= scored
    for r in e1["long"] + e1["short"]:
        assert r.entry_t == 575 and r.res["close"]["exit_t"] == 955 and r.res["30m"]["exit_t"] == 605
    # the unconditional baseline trades every scored day at the same minute with the same ETF
    for c in cells.values():
        if c.rule in ("E1", "E2", "E4", "E5"):
            assert len(res["unconditional"][c.k]) == scored
    for c in cells.values():
        if c.rule == "E3":
            assert c.k not in res["unconditional"]
    # trade returns agree with the bars: recompute one trade by hand
    r = e1["long"][0] if e1["long"] else e1["short"][0]
    etf = "TQQQ" if e1["long"] else "SQQQ"
    bars = {b.t: b for b in seg_data[r.seg][etf].regular[r.day]}
    assert r.res["close"]["r"]["base"] == pytest.approx(
        (bars[955].open * 0.9995) / (bars[575].open * 1.0005) * (1 - 0.001 - 0.0000206) - 1.001)
    # E3: entries are in 10:01..15:01 and the trade list is date-ordered
    for c in cells.values():
        if c.rule == "E3":
            assert all(601 <= r.entry_t <= 901 for r in res["main"][c.k])
            ds = [r.day for r in res["main"][c.k]]
            assert ds == sorted(ds)


def test_run_dev_end_to_end_is_json_serialisable(p2_trades):
    _, _, res = p2_trades
    out = devrun.run_dev(CELLS, {"P2": res}, b_count=200)
    assert out["k_total"] == 10 and len(out["cells"]) == 10
    assert out["verdict"] in ("NO_PASS", "DEV_PASS")
    for r in out["cells"]:
        assert set(r["variants"]) == set(devrun.REPORT_VARIANTS)
        assert 0 <= r["holm_p"] <= 1
    assert M.canonical_bytes(out)  # serialises, int keys included
    again = devrun.run_dev(CELLS, {"P2": res}, b_count=200)
    assert M.canonical_bytes(again) == M.canonical_bytes(out)  # same input, same seeds, same output
    with pytest.raises(ValueError):
        devrun.run_dev(CELLS, {"P2": res, "P9": res}, b_count=10)


def test_precount_counts_signals_and_power(p2_trades):
    from src.entry_study_v1 import precount as PC
    mask, seg_data, res = p2_trades
    counts = PC.count_pair("P2", CELLS, mask, seg_data, (SEG_A, SEG_B))
    cells = {(c.rule, c.side): c for c in CELLS if c.pair == "P2"}
    for rule in ("E1", "E2", "E3", "E4", "E5"):
        for side in ("long", "short"):
            got = counts["signals"].get(f"{rule}:{side}", {"A": 0, "B": 0})
            assert got["A"] + got["B"] == len(res["main"][cells[(rule, side)].k])
    assert counts["e2_scored_days"] == sum(len(m["scored"]) for m in mask["segments"].values())
    table = PC.power_table("P2", counts, 30)
    assert all(r["close"]["delta_alpha_0.05_over_K"] > r["close"]["delta_alpha_0.05"] for r in table if r["n"])


def test_gate_script_runs_on_a_tiny_database(tmp_path):
    from scripts import entry_study_v1_gate
    db = make_db(tmp_path / "h.sqlite", [_row("SPY", date(2025, 12, 15), 10 * 60, 100.0)])
    assert entry_study_v1_gate.main(["--db", db, "--out-dir", str(tmp_path / "out")]) == 0
    text = (tmp_path / "out" / "gate.json").read_text(encoding="utf-8")
    assert text.count("DATA_GATE_FAIL") == 3


def test_mask_must_record_the_same_gate_and_manifest(tmp_path):
    _gate, mask, man = _fixed_files(tmp_path)
    other = tmp_path / "other_gate.json"
    other.write_bytes(M.canonical_bytes({"pairs": {"x": 1}}))
    with pytest.raises(DataMismatch, match="gate"):
        require_dev_inputs(_args(tmp_path, gate=other, gate_sha256=sha256_file(other)))
    other_man = tmp_path / "other_man.json"
    other_man.write_bytes(M.canonical_bytes({"rows": {"x": 1}}))
    with pytest.raises(DataMismatch, match="manifest"):
        require_dev_inputs(_args(tmp_path, manifest=other_man, data_sha256=sha256_file(other_man)))
    assert man.exists() and mask.exists()


def test_fixed_mask_must_equal_the_rebuilt_mask():
    from src.entry_study_v1.cli import check_mask_rebuild
    pairs = {"P": ("REF", "L", "S")}
    data = {"T": synth_data(SEG)}
    fixed = M.build_mask(pairs, data, (SEG,), synth_cal(), "0" * 64)
    check_mask_rebuild(fixed, M.build_mask(pairs, data, (SEG,), synth_cal(), "0" * 64))
    data["T"]["L"].regular["2024-10-22"] = data["T"]["L"].regular["2024-10-22"][:-1]
    with pytest.raises(DataMismatch):
        check_mask_rebuild(fixed, M.build_mask(pairs, data, (SEG,), synth_cal(), "0" * 64))


def test_e2_gap_reference_must_be_finite_and_positive():
    from test_entry_study_v1 import mk, ref_day, t, with_bar

    from src.entry_study_v1 import rules as RL
    from src.entry_study_v1.bars import InvalidRun
    p = next(c.params for c in CELLS if c.rule == "E2")
    bars = with_bar({m: mk(m, 100.0) for m in range(570, 960)}, t("09:44"), 100, 100.2)
    for bad in (0.0, float("nan"), -1.0, float("inf")):
        with pytest.raises(InvalidRun):
            RL.e2(ref_day(bars, [mk(t("08:00"), bad)]), p)


def test_oos_snapshot_binding_and_implementation_paths():
    from types import SimpleNamespace

    from scripts import entry_study_v1_oos as O
    O.check_snapshot_binding("a" * 64, SimpleNamespace(snapshot_hash="a" * 64))
    with pytest.raises(DataMismatch):
        O.check_snapshot_binding("a" * 64, SimpleNamespace(snapshot_hash="b" * 64))
    root = Path(__file__).resolve().parents[1]
    for need in ("src/daytrade_paper/rules.py", "src/daytrade_paper/core.py", "src/daytrade_paper/store.py",
                 "src/history/exchange_calendar.py", "src/entry_study_v1/rules.py", "scripts/entry_study_v1_oos.py"):
        assert need in O.IMPLEMENTATION_PATHS
    assert all((root / p).exists() for p in O.IMPLEMENTATION_PATHS)


def test_oos_gates_detect_a_change(monkeypatch):
    from scripts import entry_study_v1_oos as O
    seg = Segment("OOS", date(2026, 8, 3), date(2026, 10, 9), date(2026, 9, 21), date(2026, 10, 9))
    data = synth_data(seg)
    monkeypatch.setattr(O, "OOS_SEGMENT", seg)
    before = O.oos_gates({"OOS": data}, {"P": ("REF", "L", "S")})
    assert before["P"]["status"] == "PASS"
    data["L"].regular["2026-09-22"] = data["L"].regular["2026-09-22"][:-1]
    after = O.oos_gates({"OOS": data}, {"P": ("REF", "L", "S")})
    assert after != before


def test_oos_calendar_must_exist_and_cover_the_oos_span(tmp_path):
    import yaml

    from src.entry_study_v1.cli import load_oos_calendar
    with pytest.raises(StudyRefused, match="does not exist"):
        load_oos_calendar(tmp_path / "missing.yaml")
    short = tmp_path / "short.yaml"
    short.write_text(yaml.safe_dump(synth_cal(end="2026-09-30")), encoding="utf-8")
    with pytest.raises(StudyRefused, match="coverage"):
        load_oos_calendar(short)
    late = tmp_path / "late.yaml"
    late.write_text(yaml.safe_dump(synth_cal(start="2026-02-01", end="2026-12-31")), encoding="utf-8")
    with pytest.raises(StudyRefused, match="coverage"):
        load_oos_calendar(late)
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(synth_cal()), encoding="utf-8")
    cal, sha = load_oos_calendar(good)
    assert sha == sha256_file(good) and cal["coverage"]["end"] == "2026-12-31"


def test_oos_prepare_refuses_without_an_oos_calendar(tmp_path):
    from scripts import entry_study_v1_oos
    gate, mask, man = _fixed_files(tmp_path)
    common = ["--db", str(tmp_path / "none.sqlite"), "--gate", str(gate), "--gate-sha256", sha256_file(gate),
              "--mask", str(mask), "--mask-sha256", sha256_file(mask), "--manifest", str(man),
              "--data-sha256", sha256_file(man), "--allow-dirty", "--out-dir", str(tmp_path / "out"),
              "--dev-result", "x", "--dev-result-sha256", "0" * 64,
              "--oos-events", str(tmp_path / "nope.yaml")]
    with pytest.raises(StudyRefused, match="does not exist"):
        entry_study_v1_oos.main(["prepare", *common])
    assert not (tmp_path / "out").exists()


def test_power_table_has_all_cells_and_the_60m_hold():
    from src.entry_study_v1 import precount as PC
    counts = {"signals": {"E1:long": {"A": 10, "B": 20, "event_days": 1}}}
    rows = PC.power_table("P3", counts, 30)
    assert len(rows) == 10 and rows[0]["n"] == 30 and rows[1]["n"] == 0
    assert rows[1]["close"]["delta_alpha_0.05"] is None and "60m" in rows[0]
    assert PC.SD["P3"]["60m"] == pytest.approx(0.0117)


def test_e2_pre_cutoff_is_the_paper_constant():
    from src.daytrade_paper import rules as PR
    from src.entry_study_v1.config import hhmm
    assert hhmm(next(c.params for c in CELLS if c.rule == "E2")["pre_cutoff"]) == PR.GAP_PRE_CUTOFF
