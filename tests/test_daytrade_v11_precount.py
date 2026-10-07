"""v1.1 pre-run count (PREREG v1.1 §6.1 mask, §6.2 count). Synthetic tmp databases and fixtures only."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest
from test_daytrade_paper import EXT_OK, NO_EVENTS, PRIOR, QC, T, s1_day, tqqq_default
from test_daytrade_shadow import s2_day, s3_day

from src.daytrade_paper import rules as R
from src.daytrade_paper import store
from src.daytrade_paper.core import Bar
from src.daytrade_paper.external import load_events
from src.daytrade_shadow.replay import replay_day
from src.daytrade_v11 import data as V
from src.daytrade_v11 import precount as P
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
    # Segment A is defined by QQQ: ETF-only history before 2023-04-11 is recorded, not refused.
    qqq_gone = {("QQQ", "2023-04-10", t) for t in range(24 * 60)}
    etf_early = make_db(tmp_path / "etf_early.sqlite", days=[date(2023, 4, 10)], drop=qqq_gone)
    out = V.boundary_check(etf_early)
    assert out["before_A"]["judged_symbols"] == ["QQQ"]
    assert out["before_A"]["symbols"]["TQQQ"]["regular_session_dates"] == ["2023-04-10"]
    assert out["gap_A_B"]["judged_symbols"] == ["QQQ"]
    # Same for the A-B gap: ETF-only rows on 2024-07-25 are recorded, QQQ rows there are refused (above).
    qqq_gone_gap = {("QQQ", "2024-07-25", t) for t in range(24 * 60)}
    etf_gap = make_db(tmp_path / "etf_gap.sqlite", days=[date(2024, 7, 25)], drop=qqq_gone_gap)
    out = V.boundary_check(etf_gap)
    assert out["gap_A_B"]["symbols"]["SQQQ"]["regular_session_dates"] == ["2024-07-25"]


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


def test_sigma_minute_gap_keeps_later_days_scored_and_is_diagnosed(tmp_path):
    """Per-T reading (§2): a missing minute only makes sigma(t) undefined at that t at runtime."""
    db = make_db(tmp_path / "h.sqlite", drop={("QQQ", "2023-07-14", T(14, 0))})
    seg = _seg(V.build_mask([V.load_segment(db, SEG)], CAL, {}))
    why = {e["date"]: e for e in seg["excluded"]}
    assert set(why) == set(V.load_segment(db, SEG).sessions[:50]) | {"2023-07-14"}
    assert why["2023-07-14"]["kinds"] == ["data_quality"]
    diag = seg["sigma_undefined_minutes_diagnostic"]
    assert diag["2023-07-13"] == [] and diag["2023-07-17"] == diag["2023-07-31"] == ["14:00"]
    # sessions 0..13 also lack 14 sigma sessions, except FOMC 2023-05-03 (S0: no sigma condition)
    assert seg["excluded_by_kind"] == {"data_quality": 1, "sigma_lookback_short": 13, "v10_lookback_short": 50}
    assert why["2023-05-03"]["kinds"] == ["v10_lookback_short"]
    assert why["2023-05-02"]["kinds"] == ["sigma_lookback_short", "v10_lookback_short"]


def test_sigma_diagnostic_marks_every_minute_when_a_session_lacks_its_open(tmp_path):
    db = make_db(tmp_path / "h.sqlite", drop={("QQQ", "2023-07-14", R.OPEN)})
    data = V.load_segment(db, SEG)  # (v1.0 RVOL already excludes those days from the mask; function only)
    diag = V.sigma_undefined_minutes(data, data.sessions.index("2023-07-17"))
    assert len(diag) == V.SIGMA_LAST_T - V.SIGMA_FIRST_T + 1 and diag[0] == "10:29" and diag[-1] == "15:54"


def test_off_grid_timestamp_is_refused(tmp_path):
    db = make_db(tmp_path / "h.sqlite", days=[date(2023, 7, 5)])
    con = sqlite3.connect(db)
    et = datetime(2023, 7, 5, 10, 0, 30, tzinfo=ET)
    con.execute(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})",
                ("QQQ", "US", "1m", et.astimezone(timezone.utc).isoformat(), et.isoformat(), "X",
                 1, 1, 1, 1, 1, "USD", 1, "t", "x", "x"))
    con.commit()
    con.close()
    with pytest.raises(V.MaskRefused, match="minute grid"):
        V.load_segment(db, V.Segment("G", date(2023, 7, 3), date(2023, 7, 7)))


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


def test_run_count_totals_flat_prices(base, tmp_path):
    _db, data, mask = base
    ext = V.load_external(*_csvs(tmp_path))
    out = run_count(mask, [data], CAL, ext)
    seg = out["segments"]["T"]
    assert seg["days"] == _seg(mask)["scored_count"] == out["total"]["days"]
    assert seg["entry_days"] == sum(seg["entries"].values()) == 0  # flat synthetic prices: no S1/S2/S3
    assert seg["risk_mode"] == {"NORMAL": seg["days"], "HIGH": 0, "UNKNOWN_TREATED_AS_HIGH": 0}
    assert seg["external_non_ok"] == {"vix": 0, "vix9d": 0}


def test_run_count_breakdown_with_entries(base, tmp_path, monkeypatch):
    """Label/direction/open breakdowns and the segment -> total roll-up with nonzero entries."""
    _db, data, mask = base
    script = [{"entry": "S1", "symbol": "TQQQ", "open_after_1030": True},
              {"entry": "S2", "symbol": "SQQQ", "open_after_1030": False},
              {"entry": "S3", "symbol": "TQQQ", "open_after_1030": True},
              {"entry": "S2", "symbol": "SQQQ", "open_after_1030": True},
              {"entry": None, "symbol": None, "open_after_1030": False}]
    calls = iter(script * 10)
    monkeypatch.setattr(P, "count_day", lambda *a, **k: next(calls))
    seg2 = V.SegmentData(V.Segment("U", SEG.start, SEG.end), data.sessions, data.regular, data.qqq_pre)
    mask2 = {"segments": {"T": _seg(mask), "U": {"scored": _seg(mask)["scored"][:5]}}}
    vix, vix9d, _ = _csvs(tmp_path)
    out = P.run_count(mask2, [data, seg2], CAL, V.load_external(vix, vix9d, None))
    n = _seg(mask)["scored_count"]
    assert n == 13
    t = out["segments"]["T"]  # 13 days = 2 full cycles + S1, S2, S3
    assert t["entries"] == {"S1": 3, "S2": 5, "S3": 3} and t["entry_days"] == 11 and t["days"] == 13
    assert t["entries_by_direction"] == {"TQQQ": 6, "SQQQ": 5}
    assert t["open_after_1030"] == {"TQQQ": 6, "SQQQ": 2} and t["open_after_1030_days"] == 8
    u = out["segments"]["U"]  # continues the cycle: S2 open, None, S1, S2 closed, S3
    assert u["entries"] == {"S1": 1, "S2": 2, "S3": 1} and u["open_after_1030"] == {"TQQQ": 2, "SQQQ": 1}
    tot = out["total"]
    assert tot["days"] == 18 and tot["entry_days"] == 15
    assert tot["entries"] == {"S1": 4, "S2": 7, "S3": 4}
    assert tot["entries_by_direction"] == {"TQQQ": 8, "SQQQ": 7}
    assert tot["open_after_1030"] == {"TQQQ": 8, "SQQQ": 3} and tot["open_after_1030_days"] == 11
    assert tot["risk_mode"]["NORMAL"] == 18


# ---------------------------------------------------------------- indicator days, S3, UNKNOWN risk

IND = {**NO_EVENTS, "indicator": True, "types": ["CPI"]}


def _count_ev(reg, pre, events, external=EXT_OK):
    return count_day("2026-10-05", reg, pre, PRIOR, external, events, 2000.0, **QC)


def s3_day_at(t3):
    """s3_day with the 5m close above VWAP at t3 - 1 (10:15 decision on indicator days)."""
    reg, pre = s3_day()
    q = reg["QQQ"]
    base = q[0].open
    reg["QQQ"] = [Bar(b.t, base, base + 0.1, b.low, base, b.volume) if b.t == T(9, 59) else b for b in q]
    reg["QQQ"] = [Bar(b.t, b.open, b.close + 0.5, b.low, b.close + 0.4, b.volume) if b.t == t3 - 1 else b
                  for b in reg["QQQ"]]
    return reg, pre


def test_indicator_day_s1_entry_at_1015_stopped_before_1030_counts_closed():
    reg, pre = s1_day()
    stop = _stop(reg)
    reg = _with_bar(reg, "TQQQ", T(10, 25), 50.3, 50.35, stop - 1.0, 50.3)
    assert _count_ev(reg, pre, IND) == {"entry": "S1", "symbol": "TQQQ", "open_after_1030": False}
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, IND, 2000.0, **QC)
    assert full["trades"][0]["entry_time"] == "10:15" and full["trades"][0]["exit_time"] == "10:25"


def test_indicator_day_s1_and_s2_entries_at_1015_surviving_count_open():
    reg, pre = s1_day()
    assert _count_ev(reg, pre, IND) == {"entry": "S1", "symbol": "TQQQ", "open_after_1030": True}
    reg, pre = s2_day()
    assert _count_ev(reg, pre, IND) == {"entry": "S2", "symbol": "SQQQ", "open_after_1030": True}
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, IND, 2000.0, **QC)
    assert full["trades"][0]["entry_time"] == "10:15"


def test_s3_counts_as_s3_tqqq_on_normal_and_indicator_days():
    reg, pre = s3_day()
    assert _count_ev(reg, pre, NO_EVENTS) == {"entry": "S3", "symbol": "TQQQ", "open_after_1030": True}
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert full["trades"][0]["entry_time"] == "10:00"
    reg, pre = s3_day_at(T(10, 15))
    assert _count_ev(reg, pre, IND) == {"entry": "S3", "symbol": "TQQQ", "open_after_1030": True}
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, IND, 2000.0, **QC)
    assert full["trades"][0]["entry_time"] == "10:15"


def test_unknown_risk_mode_suppresses_s3_but_not_s1_s2():
    reg, pre = s3_day()
    assert _count_ev(reg, pre, NO_EVENTS, external={})["entry"] is None
    reg, pre = s1_day()
    assert _count_ev(reg, pre, NO_EVENTS, external={})["entry"] == "S1"
    reg, pre = s2_day()
    assert _count_ev(reg, pre, NO_EVENTS, external={})["entry"] == "S2"


def _scenarios():
    def stop_bar(build, t, kind):
        def f():
            reg, pre = build()
            stop = _stop(reg) if reg["TQQQ"] else 0
            o = {"intrabar": (50.3, 50.35, stop - 1.0, 50.3), "gap": (stop - 1.0, stop - 1.0, stop - 1.1, stop - 1.0),
                 "at": (stop, stop, stop - 0.1, stop)}[kind]
            return _with_bar(reg, "TQQQ", t, *o), pre
        return f
    out = {"s1": (s1_day, NO_EVENTS), "s2": (s2_day, NO_EVENTS), "s3": (s3_day, NO_EVENTS),
           "s3_ind": (lambda: s3_day_at(T(10, 15)), IND), "s1_ind": (s1_day, IND), "s2_ind": (s2_day, IND),
           "s4": (lambda: s1_day(qqq_vol=1000.0), NO_EVENTS)}
    for t in (T(9, 50), T(10, 20), T(10, 29), T(10, 30), T(10, 31)):
        for kind in ("intrabar", "gap", "at"):
            out[f"s1_{kind}_{t}"] = (stop_bar(s1_day, t, kind), NO_EVENTS)
            out[f"s1ind_{kind}_{t}"] = (stop_bar(s1_day, t, kind), IND)
    return out


@pytest.mark.parametrize("name", sorted(_scenarios()))
def test_open_after_1030_matches_full_replay_exit(name):
    build, events = _scenarios()[name]
    reg, pre = build()
    r = _count_ev(reg, pre, events)
    full, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, events, 2000.0, **QC)
    morning = [tr for tr in full["trades"] if tr["kind"] in ("S1", "S2", "S3")]
    assert (r["entry"] is None) == (not morning)
    if morning:
        tr = morning[0]
        assert (r["entry"], r["symbol"]) == (tr["kind"], tr["symbol"])
        after = tr["exit_t"] > T(10, 30) or (tr["exit_t"] == T(10, 30) and tr["exit_at"] != "open")
        assert r["open_after_1030"] is after


# ---------------------------------------------------------------- script stages (synthetic)


def _script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("v11_precount", ROOT / "scripts" / "daytrade_v11_precount.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _facts_db(path):
    """Synthetic DB matching the fixed segment facts: A from 2023-04-11 (complete to 07-31) and its last
    session 2024-07-24, B's first session 2024-08-05."""
    days = xcal.trading_days(DATA_START, DATA_END) + [date(2024, 7, 24), date(2024, 8, 5)]
    return make_db(path, days=days)


def test_script_mask_then_count(tmp_path, capsys):
    mod = _script()
    db = _facts_db(tmp_path / "h.sqlite")
    vix, vix9d, dix = _csvs(tmp_path)
    common = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--dix-csv", str(dix),
              "--allow-dirty"]
    mpath = tmp_path / "m" / "mask.json"
    assert mod.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 0
    printed = capsys.readouterr().out
    assert "v10_lookback_short" in printed and "counts" not in printed
    sha = __import__("hashlib").sha256(mpath.read_bytes()).hexdigest()
    assert f"mask sha256 {sha}" in printed
    mask = json.loads(mpath.read_text())
    assert mask["segments"]["A"]["external_availability"]["vix"]["non_ok_count"] == 0
    assert mask["external_snapshot_sha256"]["vix"]
    assert mod.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 2  # write-once

    out = tmp_path / "c" / "counts.json"
    count = [*common, "--mask", str(mpath), "--out", str(out)]
    assert mod.main([*count, "--mask-sha256", "0" * 64]) == 2 and not out.exists()
    assert mod.main([*count, "--mask-sha256", sha]) == 0
    doc = json.loads(out.read_text())
    assert doc["counts"]["segments"]["A"]["days"] == mask["segments"]["A"]["scored_count"] > 0
    assert doc["counts"]["segments"]["B"]["days"] == 0
    m = doc["manifest"]
    assert m["mask_sha256"] == sha and set(m["external_csv_sha256"]) == {"vix", "vix9d", "gex"}
    assert m["numpy"] and m["python"] and m["events_sha256"]["history"] and "adjusted" in m["db_row_hash_fields"]
    assert m["git_commit"] and isinstance(m["git_dirty_tracked"], bool)
    assert mod.main([*count, "--mask-sha256", sha]) == 2  # write-once


def test_count_refuses_a_mask_that_does_not_rebuild(tmp_path):
    mod = _script()
    db = _facts_db(tmp_path / "h.sqlite")
    vix, vix9d, _ = _csvs(tmp_path)
    common = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--allow-dirty"]
    mpath = tmp_path / "mask.json"
    assert mod.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 0
    mask = json.loads(mpath.read_text())
    mask["segments"]["A"]["scored"].append("2024-07-24")  # tampered after the fact, re-hashed
    mpath.write_bytes(V.mask_bytes(mask))
    sha = __import__("hashlib").sha256(mpath.read_bytes()).hexdigest()
    out = tmp_path / "counts.json"
    assert mod.main([*common, "--mask", str(mpath), "--mask-sha256", sha, "--out", str(out)]) == 2
    assert not out.exists()


def test_count_refuses_missing_vix_unless_allowed(tmp_path):
    mod = _script()
    db = _facts_db(tmp_path / "h.sqlite")
    _vix, vix9d, _ = _csvs(tmp_path)
    vix = tmp_path / "short_vix.csv"  # ends 2023-07-01: July scored days are stale
    vix.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n06/30/2023,1,1,1,15\n")
    common = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--allow-dirty"]
    mpath = tmp_path / "mask.json"
    assert mod.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 0
    mask = json.loads(mpath.read_text())
    assert mask["segments"]["A"]["external_availability"]["vix"]["non_ok_count"] > 0
    sha = __import__("hashlib").sha256(mpath.read_bytes()).hexdigest()
    out = tmp_path / "counts.json"
    count = [*common, "--mask", str(mpath), "--mask-sha256", sha, "--out", str(out)]
    assert mod.main(count) == 2 and not out.exists()
    assert mod.main([*count, "--allow-missing-vix"]) == 0
    seg = json.loads(out.read_text())["counts"]["segments"]["A"]
    assert seg["external_non_ok"]["vix"] > 0 and seg["risk_mode"]["UNKNOWN_TREATED_AS_HIGH"] > 0


def test_script_refuses_broken_segment_facts_and_bad_modes(tmp_path):
    mod = _script()
    db = make_db(tmp_path / "h.sqlite")  # no 2024-07-24 / 2024-08-05 sessions
    vix, vix9d, _ = _csvs(tmp_path)
    common = ["--db", str(db), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d), "--allow-dirty"]
    mpath = tmp_path / "mask.json"
    assert mod.main([*common, "--mask-only", "--mask-out", str(mpath)]) == 2 and not mpath.exists()
    with pytest.raises(SystemExit):
        mod.main([*common, "--out", str(tmp_path / "c.json")])  # count mode without a fixed mask
    with pytest.raises(SystemExit):
        mod.main([*common, "--mask-only", "--mask-out", str(mpath), "--out", str(tmp_path / "c.json")])


def test_script_refuses_dirty_worktree_without_flag(tmp_path, monkeypatch):
    mod = _script()
    monkeypatch.setattr(mod, "git_state", lambda: {"commit": "x", "dirty_tracked": True, "dirty_files": ["M a"]})
    vix, vix9d, _ = _csvs(tmp_path)
    argv = ["--db", str(tmp_path / "none.sqlite"), "--vix-csv", str(vix), "--vix9d-csv", str(vix9d),
            "--mask-only", "--mask-out", str(tmp_path / "m.json")]
    assert mod.main(argv) == 2 and not (tmp_path / "m.json").exists()
