"""U2 (2026-10-16): the Paper loader refuses the sealed intraday block and never reads rows in it.

Only tmp databases with fake sentinel rows are used; no real sealed data is opened.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from src.daytrade_paper import rules as R
from src.daytrade_paper import store
from src.daytrade_paper.store import SealedDayRefused, query_bounds
from src.history import exchange_calendar as xcal
from src.history.oos_guard import TRACK_INTRADAY, blind_oos_window
from src.models import ET

SENTINEL = 999.0  # price of every fake row placed inside the sealed block
SEAL_LO, SEAL_HI = blind_oos_window(TRACK_INTRADAY)


def _rows(day: date, price: float, labels):
    out = []
    for lab in labels:
        hh, mm = divmod(lab, 60)
        et = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)
        for sym in R.SYMBOLS:
            out.append((sym, "1m", et.astimezone(timezone.utc).isoformat(), et.isoformat(), "IGNORED",
                        price, price, price, price, 1.0))
    return out


def make_db(path, days_before_seal=(), sealed_days=(), days_after_seal=()):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE candle_history (symbol TEXT, interval TEXT, ts_utc TEXT, "
                "ts_exchange TEXT, session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL)")
    regular = list(range(R.OPEN + 1, R.hm(16, 0) + 1))  # END-minute labels 09:31..16:00
    rows = []
    for d in days_before_seal:
        rows += _rows(d, 100.0, regular)
    for d in sealed_days:  # REGULAR bars and a late POST bar that lands on the next UTC date
        rows += _rows(d, SENTINEL, regular + [R.hm(20, 0)])
    for d in days_after_seal:
        rows += _rows(d, 100.0, regular)
    con.executemany("INSERT INTO candle_history VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


class Recorder:
    """Wraps the read-only connection to record every query's parameters."""

    def __init__(self):
        self.params: list[tuple] = []
        self.opened = 0

    def connect(self, real):
        rec = self

        class Con:
            def __init__(self, path):
                self.c = real(path)
                rec.opened += 1

            def execute(self, sql, params=()):
                rec.params.append(tuple(params))
                return self.c.execute(sql, params)

            def close(self):
                self.c.close()

        return Con


def _utc(d: date) -> str:
    return datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(timezone.utc).isoformat()


@pytest.mark.parametrize("day", [SEAL_LO, date(2026, 3, 2), SEAL_HI])
def test_sealed_days_are_refused_before_any_read(day, tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(store, "_connect_ro", rec.connect(store._connect_ro))
    with pytest.raises(SealedDayRefused):
        store.load_bars(make_db(tmp_path / "h.sqlite"), day.isoformat())
    assert rec.opened == 0 and rec.params == []


def test_bounds_around_the_block_never_overlap_it():
    seal_lo_utc, seal_hi_utc = _utc(SEAL_LO), _utc(SEAL_HI + timedelta(days=1))
    for d in (SEAL_LO - timedelta(days=1), date(2025, 12, 1)):
        lo, hi = query_bounds(d.isoformat())
        assert hi <= seal_lo_utc
    for d in (SEAL_HI + timedelta(days=1), date(2026, 10, 5), date(2026, 11, 20)):
        lo, hi = query_bounds(d.isoformat())
        assert lo >= seal_hi_utc
    # far from the block the 120-day window is unchanged
    lo, hi = query_bounds("2026-12-01")
    assert (lo, hi) == (_utc(date(2026, 12, 1) - timedelta(days=120)), _utc(date(2026, 12, 3)))


def test_day_after_the_block_reads_no_sealed_row(tmp_path, monkeypatch):
    sealed = [SEAL_HI - timedelta(days=1), SEAL_HI]  # 07-20, 07-21
    after = [d for d in xcal.trading_days(SEAL_HI + timedelta(days=1), date(2026, 7, 28))]
    db = make_db(tmp_path / "h.sqlite", sealed_days=sealed, days_after_seal=after)
    rec = Recorder()
    monkeypatch.setattr(store, "_connect_ro", rec.connect(store._connect_ro))
    day = after[-1].isoformat()
    out = store.load_bars(db, day)
    assert all(p[1] >= _utc(SEAL_HI + timedelta(days=1)) for p in rec.params)
    assert [s.date for s in out["prior"]] == [d.isoformat() for d in after[:-1]]
    prices = {b.open for s in out["prior"] for b in s.bars} | {b.open for bs in out["regular"].values() for b in bs}
    assert SENTINEL not in prices


def test_day_before_the_block_stops_at_its_start(tmp_path, monkeypatch):
    before = [d for d in xcal.trading_days(date(2026, 1, 12), SEAL_LO - timedelta(days=1))]
    db = make_db(tmp_path / "h.sqlite", days_before_seal=before, sealed_days=[SEAL_LO])
    rec = Recorder()
    monkeypatch.setattr(store, "_connect_ro", rec.connect(store._connect_ro))
    out = store.load_bars(db, before[-1].isoformat())
    assert all(p[2] <= _utc(SEAL_LO) for p in rec.params)
    assert SENTINEL not in {b.open for bs in out["regular"].values() for b in bs}


def _script(name):
    path = store.Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("extra", [["--dry-run-dir"], ["--backfill-check"], ["--accept-incomplete"], []])
def test_paper_cli_paths_cannot_bypass_the_refusal(extra, tmp_path, capsys):
    """A fake events file covering the sealed date makes the calendar check pass, so the loader is reached on
    every path (the live calendar refuses these dates earlier; that is not what is tested here)."""
    db = make_db(tmp_path / "h.sqlite", sealed_days=[date(2026, 3, 2)])
    events = tmp_path / "events.yaml"
    events.write_text('version: test\ncoverage: {start: "2026-01-01", end: "2026-12-31"}\nholidays: []\n'
                      "events: []\n", encoding="utf-8")
    script = _script("run_daytrade_paper")
    script.ROOT = tmp_path / "repo"
    args = ["--date", "2026-03-02", "--db", str(db), "--out-root", str(tmp_path / "out"), "--events", str(events)]
    if extra == ["--dry-run-dir"]:
        args += ["--dry-run-dir", str(tmp_path / "dry")]
    else:
        args += extra
    rc = script.main(args)
    err = capsys.readouterr().err
    assert rc == 2 and "sealed intraday blind OOS" in err
    assert not (tmp_path / "out").exists() and not (tmp_path / "dry").exists() and not (tmp_path / "repo").exists()
