"""Read-only 1m bar loading and write-once report/ledger handling."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar, PriorSession
from src.history.exchange_calendar import CalendarRangeError, regular_close_minute
from src.history.oos_guard import TRACK_INTRADAY, blind_oos_window
from src.models import ET

HISTORY_CALENDAR_DAYS = 120


class RunRefused(Exception):
    """The run must not proceed (write-once, ledger chain, data not landed)."""


class SealedDayRefused(RunRefused):
    """The requested day lies in the sealed intraday blind OOS block (2026-01-21..2026-07-21 ET)."""


def _utc_of_et_midnight(d: Date) -> str:
    """ISO UTC string of 00:00 ET on `d`, comparable with the DB's ts_utc text."""
    return datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(timezone.utc).isoformat()


def is_sealed_day(day: str) -> bool:
    """True when `day` lies in the sealed intraday blind OOS block."""
    seal_lo, seal_hi = blind_oos_window(TRACK_INTRADAY)
    return seal_lo <= Date.fromisoformat(day) <= seal_hi


def query_bounds(day: str) -> tuple[str, str]:
    """[lo, hi) ts_utc bounds of `load_bars(day)`, never touching the sealed intraday block (U2, 2026-10-16).

    A day inside the block is refused. For a day after it the history window starts after the block; for a day
    before it the window ends at the block start. Bounds are ET calendar days, so the sealed day's POST bars that
    fall on the next UTC date stay excluded. The block dates themselves are not changed here.

    Consequence: v1.0 needs 50 prior sessions (SMA50). The first day with 50 post-block sessions is 2026-10-01
    (07-22..09-30 = 50 sessions), so days 2026-07-22..2026-09-30 now fail the data check ("QQQ 과거 세션 < 50")
    instead of filling SMA50 with sealed sessions. From 2026-10-01 on, with every post-block session present, the
    v1.0 inputs (last 50/20/15 sessions) are the same as before; only the reported `prior` count is smaller.
    """
    d = Date.fromisoformat(day)
    seal_lo, seal_hi = blind_oos_window(TRACK_INTRADAY)
    if is_sealed_day(day):
        raise SealedDayRefused(f"{day} is inside the sealed intraday blind OOS block {seal_lo}..{seal_hi}; "
                               "the Paper loader does not read it")
    lo_d = d - timedelta(days=HISTORY_CALENDAR_DAYS)
    hi_d = d + timedelta(days=2)
    if d > seal_hi:
        lo_d = max(lo_d, seal_hi + timedelta(days=1))
    else:
        hi_d = min(hi_d, seal_lo)
    return _utc_of_et_midnight(lo_d), _utc_of_et_midnight(hi_d)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _bar(row) -> tuple[str, str, Bar] | None:
    """Toss labels a 1m bar with its END minute (verified 2026-10-05: the daily open equals the open of
    the bar labelled 09:31, and the live bar labelled HH:MM+1 fills during HH:MM). The collector's
    `session` column treats the label as the start, so it is ignored here: the bar starts at label - 1
    and is classed REGULAR when OPEN <= start < that day's close (13:00 on early-close days)."""
    ts, _session, o, h, lo, c, v = row
    t = int(ts[11:13]) * 60 + int(ts[14:16]) - 1
    if t < 0:
        return None
    day = ts[:10]
    close = regular_close_minute(Date.fromisoformat(day))
    if close is None or t >= close:
        session = "POST"
    elif t >= R.OPEN:
        session = "REGULAR"
    else:
        session = "PRE"
    return day, session, Bar(t, o, h, lo, c, v)


def load_bars(db_path: Path, day: str) -> dict:
    """Bars for `day` (all three symbols) plus prior QQQ regular sessions, read-only.

    Refuses a day in the sealed intraday block and never reads rows inside it (`query_bounds`)."""
    lo, hi = query_bounds(day)
    con = _connect_ro(db_path)
    try:
        regular: dict[str, list[Bar]] = {s: [] for s in R.SYMBOLS}
        qqq_pre: list[Bar] = []
        prior: dict[str, list[Bar]] = {}
        prior_times: dict[str, dict[str, set[int]]] = {s: {} for s in R.SYMBOLS}
        q = ("SELECT ts_exchange, session, open, high, low, close, volume FROM candle_history "
             "WHERE symbol=? AND interval='1m' AND ts_utc>=? AND ts_utc<? ORDER BY ts_utc")
        for sym in R.SYMBOLS:
            for row in con.execute(q, (sym, lo, hi)):
                if row[0][:10] > day:
                    continue
                try:
                    parsed = _bar(row)
                except CalendarRangeError as exc:
                    raise RunRefused(f"exchange calendar does not cover {row[0][:10]}: {exc}") from exc
                if parsed is None:
                    continue
                bday, session, bar = parsed
                if bday == day and session == "REGULAR":
                    regular[sym].append(bar)
                elif bday == day and session == "PRE" and sym == R.SIGNAL:
                    qqq_pre.append(bar)
                elif bday < day and session == "REGULAR":
                    prior_times[sym].setdefault(bday, set()).add(bar.t)
                    if sym == R.SIGNAL:
                        prior.setdefault(bday, []).append(bar)
    finally:
        con.close()
    sessions = [PriorSession(k, tuple(v)) for k, v in sorted(prior.items())]
    prev = sessions[-1].date if sessions else None
    return {"regular": regular, "qqq_pre": qqq_pre, "prior": sessions, "prev_session": prev,
            "prev_times": {s: prior_times[s].get(prev, set()) for s in R.SYMBOLS}}


def new_ledger() -> dict:
    return {"label": R.LABEL, "rules_version": R.RULES_VERSION, "contract": R.CONTRACT_PATH,
            "start_equity": R.START_EQUITY, "days": []}


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return new_ledger()
    return json.loads(path.read_text(encoding="utf-8"))


def check_chain(ledger: dict, day: str, prev_trading_day: str) -> float:
    """Return the starting equity for `day`, refusing gaps or repeats in the ledger.

    prev_trading_day comes from the NYSE calendar (not the DB), so a session the collector
    missed cannot be skipped silently.
    """
    days = ledger["days"]
    if not days:
        return float(ledger["start_equity"])
    last = days[-1]
    if day <= last["date"]:
        raise RunRefused(f"ledger already has {last['date']} (>= {day}); dates must move forward")
    if prev_trading_day != last["date"]:
        raise RunRefused(
            f"previous NYSE trading day {prev_trading_day} is not the last ledger day {last['date']}; "
            "run the missing day first")
    return float(last["equity_end"])


def ledger_row(result: dict, report_path: str) -> dict:
    return {"date": result["date"], "situation": result["situation"],
            "situations": result["situations"], "trades": len(result["trades"]),
            "equity_start": result["equity_start"], "day_pnl": result["pnl"]["day_pnl"],
            "equity_end": result["pnl"]["equity_end"], "rules_version": result["rules_version"],
            "qqq_open_to_close": result["benchmarks"]["QQQ_open_to_close"],
            "tqqq_open_to_close": result["benchmarks"]["TQQQ_open_to_close"],
            "report": report_path}


def write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError as exc:
        raise RunRefused(f"{path} already exists; outputs are write-once") from exc


def write_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def commit_outputs(report_path: Path, report_text: str, journal_path: Path, journal_text: str,
                   ledger_path: Path, ledger: dict, row: dict) -> None:
    """Write the report, the journal, then the ledger row last. On any failure, delete the
    files this call created (so a rerun is not blocked by write-once) and re-raise."""
    created: list[Path] = []
    try:
        write_once(report_path, report_text)
        created.append(report_path)
        write_once(journal_path, journal_text)
        created.append(journal_path)
        write_ledger(ledger_path, {**ledger, "days": [*ledger["days"], row]})
    except BaseException:
        for p in created:
            p.unlink(missing_ok=True)
        raise
