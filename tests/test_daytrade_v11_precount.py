"""v1.1 pre-run count (PREREG v1.1 §6.1 mask, §6.2 count). Synthetic tmp databases and fixtures only."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest
from test_daytrade_paper import EXT_OK, NO_EVENTS, PRIOR, QC, T, s1_day, tqqq_default

from src.daytrade_paper import rules as R
from src.daytrade_paper import store
from src.daytrade_paper.core import Bar
from src.daytrade_paper.external import load_events
from src.daytrade_shadow.replay import replay_day
from src.daytrade_v11 import data as V
from src.daytrade_v11.precount import count_day, run_count
from src.history import exchange_calendar as xcal
from src.history.oos_guard import OOSLockError, research_load
from src.history.store import SCHEMA
from src.models import ET

ROOT = store.Path(__file__).resolve().parents[1]
CAL = load_events(ROOT / "config" / "daytrade_events_history_v1.yaml")
PRICES = {"QQQ": 100.0, "TQQQ": 50.0, "SQQQ": 20.0}
DATA_START, DATA_END = date(2023, 4, 11), date(2023, 7, 31)
SEG = V.Segment("T", date(2023, 5, 1), DATA_END)  # data also exists before the segment start


def _row(sym, d: date, label: int, p: float):
    hh, mm = divmod(label, 60)
    et = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
    return (sym, "US", "1m", et.astimezone(timezone.utc).isoformat(), et.isoformat(), "IGNORED",
            p, p + 0.1, p - 0.1, p, 1000.0, "USD", 1, "test", "x", "x")


def make_db(path, days=None, drop=frozenset(), drop_days=frozenset()):
    """Flat bars for every session: REGULAR labels 09:31..close (bar starts 09:30..close-1), one QQQ PRE bar.
    drop: {(symbol, iso day, bar start t)}; drop_days: iso days with no rows at all."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rows = []
    for d in days if days is not None else xcal.trading_days(DATA_START, DATA_END):
        if d.isoformat() in drop_days:
            continue
        close = xcal.regular_close_minute(d)
        for sym, p in PRICES.items():
            starts = list(range(R.OPEN, close)) + ([T(9, 0)] if sym == "QQQ" else [])
            rows.extend(_row(sym, d, t + 1, p) for t in starts if (sym, d.isoformat(), t) not in drop)
    con.executemany(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})", rows)
    con.commit()
    con.close()
    return path


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    db = make_db(tmp_path_factory.mktemp("base") / "h.sqlite")
    data = V.load_segment(db, SEG)
    return db, data, V.build_mask([data], CAL, {"test": True})


def _seg(mask):
    return mask["segments"]["T"]


# ---------------------------------------------------------------- bars and the seal


def test_candle_bar_equals_store_bar(tmp_path):
    days = [date(2023, 7, 3), date(2023, 7, 5)]  # early close, normal
    db = tmp_path / "h.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    labels = (0, T(9, 0), T(9, 30), T(9, 31), T(12, 59), T(13, 0), T(13, 1), T(16, 0), T(16, 1), T(19, 59))
    con.executemany(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})",
                    [_row("QQQ", d, lab, 100.0 + lab / 1000) for d in days for lab in labels])
    con.commit()
    raw = con.execute("SELECT ts_exchange, session, open, high, low, close, volume FROM candle_history "
                      "WHERE symbol='QQQ' ORDER BY ts_utc").fetchall()
    con.close()
    lo, hi = datetime(2023, 7, 1, tzinfo=ET), datetime(2023, 7, 6, tzinfo=ET)
    candles = research_load(str(db), "QQQ", "1m", start=lo, end=hi, purpose="performance")
    assert len(candles) == len(raw) == 20
    assert [V.candle_bar(c) for c in candles] == [store._bar(r) for r in raw]
    sessions = {(b[0], b[2].t): b[1] for b in map(V.candle_bar, candles) if b}
    assert sessions[("2023-07-03", T(12, 59))] == "REGULAR" and sessions[("2023-07-03", T(13, 0))] == "POST"
    assert sessions[("2023-07-05", T(15, 59))] == "REGULAR" and sessions[("2023-07-05", T(9, 29))] == "PRE"


def test_segments_are_outside_the_seal_and_touching_it_raises(tmp_path):
    db = make_db(tmp_path / "h.sqlite", days=[])
    assert V.SEGMENTS[1].bounds()[1] == datetime(2026, 1, 21, tzinfo=ET)  # exclusive end = seal start
    V.load_segment(db, V.SEGMENTS[1])  # allowed: ends right before the seal
    with pytest.raises(OOSLockError):
        V.load_segment(db, V.Segment("X", date(2026, 1, 2), date(2026, 1, 21)))
    with pytest.raises(OOSLockError):
        research_load(str(db), "QQQ", "1m", start=datetime(2026, 1, 20, tzinfo=ET),
                      end=datetime(2026, 1, 21, 0, 1, tzinfo=ET), purpose="performance")


def test_boundary_check_counts_dates_and_refuses_bars_in_the_gap(tmp_path):
    ok = make_db(tmp_path / "ok.sqlite", days=[date(2023, 4, 11), date(2024, 7, 24), date(2024, 8, 5)])
    out = V.boundary_check(ok)
    assert out["gap_A_B"]["symbols"]["QQQ"]["regular_session_dates"] == []
    bad = make_db(tmp_path / "bad.sqlite", days=[date(2024, 7, 26)])
    with pytest.raises(V.MaskRefused, match="gap_A_B"):
        V.boundary_check(bad)
    early = make_db(tmp_path / "early.sqlite", days=[date(2023, 4, 10)])
    with pytest.raises(V.MaskRefused, match="before_A"):
        V.boundary_check(early)


# ---------------------------------------------------------------- lookbacks and the mask


def test_first_scored_day_needs_50_in_segment_sessions(base):
    db, data, mask = base
    seg = _seg(mask)
    assert data.sessions[50] == "2023-07-13"
    assert seg["scored"][0] == "2023-07-13"
    assert seg["scored"] == data.sessions[50:]  # complete synthetic data: everything after the lookback
    first = seg["excluded"][0]
    assert first["date"] == "2023-05-01" and "0 in-segment sessions" in first["reasons"][0]
    assert len(seg["excluded"]) == 50
    # rows before the segment exist (store.load_bars would use them); the segment never does
    assert len(store.load_bars(db, "2023-05-01")["prior"]) > 10
    assert min(data.regular) == "2023-05-01"
    assert "2023-07-26" in seg["scored"]  # FOMC = S0 stays in the mask


def test_sigma_lookback_skips_early_close_sessions(base):
    _db, data, mask = base
    i = data.sessions.index("2023-07-13")
    sig = V.sigma_sessions(data.sessions, i)
    assert "2023-07-03" not in sig and len(sig) == 14 and sig[0] == "2023-06-21" and sig[-1] == "2023-07-12"
    # 07-03 has no bars after 12:59, so scoring 07-13 (which spans it) proves it was skipped
    assert data.regular["2023-07-03"]["QQQ"][-1].t == T(12, 59)
    assert "2023-07-13" in _seg(mask)["scored"]


def test_day_inputs_match_store_load_bars_on_a_scored_day(base):
    db, data, _mask = base
    day = "2023-07-20"
    ours = V.day_inputs(data, data.sessions.index(day))
    ref = store.load_bars(db, day)
    assert ours["regular"] == ref["regular"] and ours["qqq_pre"] == ref["qqq_pre"]
    assert ours["prior"] == ref["prior"][-V.V10_LOOKBACK:]
    assert ours["prev_session"] == ref["prev_session"] and ours["prev_times"] == ref["prev_times"]


def test_missing_lookback_session_is_not_substituted(tmp_path):
    db = make_db(tmp_path / "h.sqlite", drop_days={"2023-06-15"})
    mask = V.build_mask([V.load_segment(db, SEG)], CAL, {})
    seg = _seg(mask)
    assert seg["scored"] == []
    why = {e["date"]: e["reasons"] for e in seg["excluded"]}
    assert any("not substituted" in r and "2023-06-15" in r for r in why["2023-07-13"])
    assert len(store.load_bars(db, "2023-07-13")["prior"]) >= 50  # v1.0 alone would reach older rows


def test_mask_excludes_data_quality_failure_with_reason(tmp_path):
    db = make_db(tmp_path / "h.sqlite", drop={("TQQQ", "2023-07-20", T(12, 0))})
    seg = _seg(V.build_mask([V.load_segment(db, SEG)], CAL, {}))
    why = {e["date"]: e["reasons"] for e in seg["excluded"]}
    assert "2023-07-20" not in seg["scored"]
    assert any(r.startswith("data_quality:") and "TQQQ" in r for r in why["2023-07-20"])
    assert "2023-07-19" in seg["scored"] and "2023-07-21" in seg["scored"]


def test_sigma_session_missing_a_minute_excludes_later_days(tmp_path):
    db = make_db(tmp_path / "h.sqlite", drop={("QQQ", "2023-07-14", T(14, 0))})
    seg = _seg(V.build_mask([V.load_segment(db, SEG)], CAL, {}))
    why = {e["date"]: e["reasons"] for e in seg["excluded"]}
    assert any(r.startswith("data_quality:") for r in why["2023-07-14"])
    assert any("sigma session 2023-07-14" in r and "14:00" in r for r in why["2023-07-17"])
    assert seg["scored"] == ["2023-07-13"]  # every later day in range has 07-14 in its 14 sessions


def test_events_outside_coverage_are_refused():
    for day in ("2023-03-31", "2026-02-02"):
        with pytest.raises(V.MaskRefused, match="coverage"):
            V.day_events(CAL, day)
    outside = V.SegmentData(V.Segment("Z", date(2023, 3, 27), date(2023, 4, 12)),
                            ["2023-03-31", "2023-04-03", "2023-04-11", "2023-04-12"])
    with pytest.raises(V.MaskRefused, match="coverage"):
        V.build_mask([outside], CAL, {})


def test_mask_file_is_write_once_and_hashed(base, tmp_path):
    _db, _data, mask = base
    p = tmp_path / "mask.json"
    sha = V.write_mask(p, mask)
    import hashlib
    assert hashlib.sha256(p.read_bytes()).hexdigest() == sha
    with pytest.raises(FileExistsError):
        V.write_mask(p, mask)


# ---------------------------------------------------------------- the 10:30 open stage


def _with_bar(reg, sym, t, o, h, lo, c):
    reg[sym] = [b if b.t != t else Bar(t, o, h, lo, c, b.volume) for b in reg[sym]]
    return reg


def _stop(reg):
    return min(b.low for b in reg["TQQQ"] if R.OPEN <= b.t < R.DECISION)


def _count(reg, pre):
    return count_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)


def test_count_baseline_s1_open_after_1030():
    reg, pre = s1_day()
    assert _count(reg, pre) == {"entry": "S1", "symbol": "TQQQ", "open_after_1030": True}


def test_intrabar_stop_in_1030_bar_still_counts_open():
    reg, pre = s1_day()
    stop = _stop(reg)
    reg = _with_bar(reg, "TQQQ", T(10, 30), 50.3, 50.35, stop - 1.0, 50.3)
    assert _count(reg, pre)["open_after_1030"] is True
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert (full["trades"][0]["exit_time"], full["trades"][0]["exit_reason"]) == ("10:30", "STOP")


def test_open_exactly_at_stop_in_1030_is_intrabar_so_still_open():
    reg, pre = s1_day()
    stop = _stop(reg)
    reg = _with_bar(reg, "TQQQ", T(10, 30), stop, stop, stop - 0.1, stop)
    assert _count(reg, pre)["open_after_1030"] is True


def test_gap_stop_at_1030_open_counts_closed():
    reg, pre = s1_day()
    stop = _stop(reg)
    reg = _with_bar(reg, "TQQQ", T(10, 30), stop - 1.0, stop - 1.0, stop - 1.1, stop - 1.0)
    r = _count(reg, pre)
    assert r["entry"] == "S1" and r["open_after_1030"] is False
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert (full["trades"][0]["exit_time"], full["trades"][0]["exit_reason"]) == ("10:30", "STOP_GAP")


def test_stop_at_1020_counts_closed():
    reg, pre = s1_day()
    stop = _stop(reg)
    reg = _with_bar(reg, "TQQQ", T(10, 20), 50.3, 50.35, stop - 1.0, 50.3)
    assert _count(reg, pre) == {"entry": "S1", "symbol": "TQQQ", "open_after_1030": False}


def test_count_never_needs_bars_after_1030():
    reg, pre = s1_day(tqqq_fn=lambda t: tqqq_default(t) if t <= T(10, 30) else 1.0)
    cut = {s: [b for b in v if b.t <= T(10, 30)] for s, v in reg.items()}
    assert _count(reg, pre) == _count(cut, pre) == {"entry": "S1", "symbol": "TQQQ", "open_after_1030": True}


def test_s0_and_s4_days_have_no_entry():
    reg, pre = s1_day()
    fomc = {**NO_EVENTS, "fomc": True, "types": ["FOMC"]}
    assert count_day("2026-10-05", reg, pre, PRIOR, EXT_OK, fomc, 2000.0, **QC)["entry"] is None
    reg, pre = s1_day(qqq_vol=1000.0)
    assert _count(reg, pre)["entry"] is None


# ---------------------------------------------------------------- end to end (synthetic)


def _csvs(tmp_path):
    days = [date.fromordinal(o) for o in range(date(2023, 4, 1).toordinal(), date(2023, 8, 31).toordinal())]
    vix = tmp_path / "VIX_History.csv"
    vix.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(f"{d:%m/%d/%Y},1,1,1,15\n" for d in days))
    vix9d = tmp_path / "VIX9D_History.csv"
    vix9d.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(f"{d:%m/%d/%Y},1,1,1,14\n" for d in days))
    dix = tmp_path / "DIX.csv"
    dix.write_text("date,price,dix,gex\n" + "".join(f"{d.isoformat()},1,0.4,1000\n" for d in days))
    return vix, vix9d, dix


def test_run_count_totals(base, tmp_path):
    _db, data, mask = base
    ext = V.load_external(*_csvs(tmp_path))
    out = run_count(mask, [data], CAL, ext)
    seg = out["segments"]["T"]
    assert seg["days"] == _seg(mask)["scored_count"] == out["total"]["days"]
    assert seg["entry_days"] == sum(seg["entries"].values()) == 0  # flat synthetic prices: no S1/S2/S3


def test_script_end_to_end_on_synthetic_db(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("v11_precount", ROOT / "scripts" / "daytrade_v11_precount.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = make_db(tmp_path / "h.sqlite")
    vix, vix9d, dix = _csvs(tmp_path)
    out = tmp_path / "out" / "counts.json"
    argv = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--dix-csv", str(dix),
            "--out", str(out)]
    assert mod.main(argv) == 0
    doc = json.loads(out.read_text())
    mask = json.loads((tmp_path / "out" / "counts_mask.json").read_text())
    assert doc["counts"]["segments"]["A"]["days"] == mask["segments"]["A"]["scored_count"] > 0
    assert doc["counts"]["segments"]["B"]["days"] == 0
    m = doc["manifest"]
    assert set(m["external_csv_sha256"]) == {"vix", "vix9d", "gex"} and len(m["mask_sha256"]) == 64
    assert m["numpy"] and m["python"] and m["events_sha256"]["history"]
    assert mod.main(argv) == 2  # write-once
