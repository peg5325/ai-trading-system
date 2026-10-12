"""v1.1 historical inputs and the scored-date mask (PREREG v1.1 §2, §6.1).

Prices are read only through `src.history.oos_guard.research_load(purpose="performance")`, so any range
touching the sealed intraday blind OOS block raises `OOSLockError`. Bars are built with the Paper's own
`store._bar` (Toss END-minute label -> bar start, session by the exchange calendar), and each scored day gets
the same per-day structure `store.load_bars` returns, restricted to one continuous segment: lookback sessions
are the exchange-calendar sessions before the day, never older rows standing in for a missing one.

The mask depends only on dates, bar presence and the event calendar. No price, return or P&L is computed here.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    data_quality,
    decision_time,
    fmt_t,
    high_mode,
)
from src.daytrade_paper.external import (
    events_for,
    parse_cboe,
    parse_dix,
    prev_trading_day,
    prior_value,
)
from src.daytrade_paper.store import _bar
from src.history import exchange_calendar as xcal
from src.history.oos_guard import TRACK_INTRADAY, overlaps_blind_oos, research_load
from src.models import ET, Candle

MASK_SCHEMA = "daytrade_v11_mask/v1"
PREREG_PATH = "docs/research/PREREG_DAYTRADE_V1_1_INTRADAY_REVERSAL.md"

# v1.0 lookback: SMA50 (covers ATR14's 15 sessions and RVOL's 20).
V10_LOOKBACK = max(R.SMA_DAYS, R.ATR_DAYS + 1, R.RVOL_LOOKBACK)
# v1.1 sigma(t) (§2): the 14 prior non-early-close sessions must lie in the same segment. Minute gaps are a
# runtime matter (sigma(t) undefined -> that decision time is skipped) and only reported in the mask as a
# diagnostic over the minutes S6 reads (decisions t = T-1 for T in 10:30..14:30, signal exit up to 15:54).
SIGMA_SESSIONS = 14
SIGMA_FIRST_T = R.hm(10, 29)
SIGMA_LAST_T = R.hm(15, 54)

# Fields of each DB row covered by the per-segment, per-symbol content hash (one line per row, ts_utc order).
ROW_HASH_FIELDS = ("ts_utc", "ts_exchange", "session", "open", "high", "low", "close", "volume", "market",
                   "adjusted")

# Date-only segment end facts (§6.1, user decision 2026-10-07): first/last session with QQQ regular bars.
SEGMENT_FACTS = {"A": ("2023-04-11", "2024-07-24"), "B": ("2024-08-05", None)}


class MaskRefused(Exception):
    """Inputs cannot support a fixed mask (calendar coverage, calendar mismatch, segment boundary facts)."""


@dataclass(frozen=True)
class Segment:
    name: str
    start: Date  # first ET session date, inclusive
    end: Date  # last ET session date, inclusive

    def bounds(self) -> tuple[datetime, datetime]:
        """[first day 00:00 ET, day after last day 00:00 ET)."""
        lo = datetime(self.start.year, self.start.month, self.start.day, tzinfo=ET)
        hi_d = self.end + timedelta(days=1)
        return lo, datetime(hi_d.year, hi_d.month, hi_d.day, tzinfo=ET)


SEGMENTS = (
    Segment("A", Date(2023, 4, 11), Date(2024, 7, 24)),
    Segment("B", Date(2024, 8, 5), Date(2026, 1, 20)),
)
for _seg in SEGMENTS:
    if overlaps_blind_oos(*_seg.bounds(), TRACK_INTRADAY):
        raise RuntimeError(f"segment {_seg.name} overlaps the sealed intraday blind OOS block")

# Date-only boundary facts re-checked at mask time (§6.1): no regular bars before A or between A and B.
BOUNDARY_WINDOWS = {
    "before_A": (None, SEGMENTS[0].start),
    "gap_A_B": (SEGMENTS[0].end + timedelta(days=1), SEGMENTS[1].start),
}


def _et_midnight(d: Date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def candle_bar(c: Candle) -> tuple[str, str, Bar] | None:
    """`store._bar` applied to a Candle. The DB stores ts_exchange as `Candle.ts_exchange.isoformat()`
    (src/history/store.py), so the row tuple is the one `load_bars` reads. Off-grid times are refused."""
    if c.ts_exchange.second or c.ts_exchange.microsecond:
        raise MaskRefused(f"{c.symbol} bar at {c.ts_exchange.isoformat()} is not on the minute grid")
    return _bar((c.ts_exchange.isoformat(), c.session, c.open, c.high, c.low, c.close, c.volume))


@dataclass
class SegmentData:
    segment: Segment
    sessions: list[str]  # exchange-calendar trading days in the segment, ascending
    regular: dict[str, dict[str, list[Bar]]] = field(default_factory=dict)  # day -> symbol -> REGULAR bars
    qqq_pre: dict[str, list[Bar]] = field(default_factory=dict)  # day -> QQQ PRE bars
    row_sha256: dict[str, str] = field(default_factory=dict)  # symbol -> hash of the rows read
    row_counts: dict[str, int] = field(default_factory=dict)


def _month_chunks(seg: Segment):
    lo = seg.start
    while lo <= seg.end:
        nxt = Date(lo.year + (lo.month == 12), lo.month % 12 + 1, 1)
        hi = min(nxt, seg.end + timedelta(days=1))
        yield _et_midnight(lo), _et_midnight(hi)
        lo = hi


def load_segment(db: Path | str, seg: Segment) -> SegmentData:
    """QQQ/TQQQ/SQQQ 1m bars of one segment, in month chunks, all via research_load(purpose="performance")."""
    data = SegmentData(seg, [d.isoformat() for d in xcal.trading_days(seg.start, seg.end)])
    first, last = seg.start.isoformat(), seg.end.isoformat()
    for sym in R.SYMBOLS:
        h = hashlib.sha256()
        n = 0
        for lo, hi in _month_chunks(seg):
            for c in research_load(str(db), sym, "1m", start=lo, end=hi, purpose="performance",
                                 track=TRACK_INTRADAY):
                n += 1
                h.update(f"{c.ts_utc.isoformat()}|{c.ts_exchange.isoformat()}|{c.session}|{c.open!r}|"
                         f"{c.high!r}|{c.low!r}|{c.close!r}|{c.volume!r}|{c.market}|{int(c.adjusted)}\n".encode())
                parsed = candle_bar(c)
                if parsed is None:
                    continue
                day, session, bar = parsed
                if not first <= day <= last:
                    raise MaskRefused(f"{sym} bar dated {day} read inside segment {seg.name} chunk [{lo}, {hi})")
                if session == "REGULAR":
                    data.regular.setdefault(day, {}).setdefault(sym, []).append(bar)
                elif session == "PRE" and sym == R.SIGNAL:
                    data.qqq_pre.setdefault(day, []).append(bar)
        data.row_sha256[sym] = h.hexdigest()
        data.row_counts[sym] = n
    return data


def boundary_check(db: Path | str) -> dict:
    """Count REGULAR bars (dates only, no prices) before segment A and in the A-B gap; refuse if any."""
    out: dict = {}
    for name, (lo_d, hi_d) in BOUNDARY_WINDOWS.items():
        lo = _et_midnight(lo_d) if lo_d else None
        counts: dict[str, dict] = {}
        for sym in R.SYMBOLS:
            regular_dates: set[str] = set()
            outside_calendar = 0
            for c in research_load(str(db), sym, "1m", start=lo, end=_et_midnight(hi_d), purpose="performance",
                                     track=TRACK_INTRADAY):
                if c.ts_exchange.date() < xcal.CALENDAR_START:
                    outside_calendar += 1
                    continue
                parsed = candle_bar(c)
                if parsed is not None and parsed[1] == "REGULAR":
                    regular_dates.add(parsed[0])
            counts[sym] = {"regular_session_dates": sorted(regular_dates),
                           "rows_before_calendar_start": outside_calendar}
        out[name] = {"start": lo_d.isoformat() if lo_d else None, "end_exclusive": hi_d.isoformat(),
                     "symbols": counts}
        # Segment boundaries are QQQ facts (the signal and every lookback are QQQ; TQQQ/SQQQ are read only on
        # scored days, and their previous-session final bar lies inside the segment). Both windows are therefore
        # judged on QQQ alone; ETF sessions there (real DB: TQQQ/SQQQ 2023-01-03..2023-04-10 and 2024-07-25,
        # days with no QQQ data) are recorded only, never read as inputs.
        judged = (R.SIGNAL,)
        out[name]["judged_symbols"] = list(judged)
        bad = {s: counts[s] for s in judged
               if counts[s]["regular_session_dates"] or counts[s]["rows_before_calendar_start"]}
        if bad:
            raise MaskRefused(f"segment boundary fact broken in {name}: {bad}")
    return out


# ---------------------------------------------------------------- events and calendar


def day_events(cal: dict, day: str) -> dict:
    ev = events_for(cal, day)
    if not ev["known"]:
        raise MaskRefused(f"{day} is outside the event calendar coverage {cal['coverage']}; refusing")
    return ev


def _xcal_prev(day: str) -> str:
    d = Date.fromisoformat(day) - timedelta(days=1)
    while not xcal.is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def day_context(cal: dict, day: str) -> dict:
    """Events, decision time and the previous session for `day`, as the Paper runner derives them
    (events yaml), cross-checked against the exchange calendar."""
    ev = day_events(cal, day)
    prev = prev_trading_day(cal, day)
    if prev != _xcal_prev(day):
        raise MaskRefused(f"{day}: previous session {prev} (events) != {_xcal_prev(day)} (exchange calendar)")
    prev_early = day_events(cal, prev)["early_close"]
    for d, flag in ((day, ev["early_close"]), (prev, prev_early)):
        if flag != xcal.is_early_close(Date.fromisoformat(d)):
            raise MaskRefused(f"{d}: early-close flag differs between events yaml and exchange calendar")
    return {"events": ev, "decision_t": decision_time(ev), "expected_prev": prev, "prev_early_close": prev_early}


# ---------------------------------------------------------------- lookbacks


def sigma_sessions(sessions: list[str], i: int, n: int = SIGMA_SESSIONS) -> list[str] | None:
    """The n non-early-close sessions before sessions[i] inside the segment (oldest first), or None."""
    out: list[str] = []
    j = i - 1
    while j >= 0 and len(out) < n:
        if not xcal.is_early_close(Date.fromisoformat(sessions[j])):
            out.append(sessions[j])
        j -= 1
    return list(reversed(out)) if len(out) == n else None


def _qqq(data: SegmentData, day: str) -> list[Bar]:
    return data.regular.get(day, {}).get(R.SIGNAL, [])


def lookback_reasons(data: SegmentData, i: int, s0: bool = False) -> list[tuple[str, str]]:
    """(kind, text) reasons why sessions[i] lacks in-segment lookbacks. S0 days get no sigma condition."""
    reasons: list[tuple[str, str]] = []
    day = data.sessions[i]
    if i < V10_LOOKBACK:
        reasons.append(("v10_lookback_short",
                        f"v1.0 lookback: {i} in-segment sessions before {day} < {V10_LOOKBACK} (SMA50)"))
    else:
        missing = [d for d in data.sessions[i - V10_LOOKBACK:i] if not _qqq(data, d)]
        if missing:
            reasons.append(("v10_lookback_missing_session",
                            "v1.0 lookback sessions without QQQ regular bars (not substituted): " + ", ".join(missing)))
    if s0:
        return reasons
    sig = sigma_sessions(data.sessions, i)
    if sig is None:
        reasons.append(("sigma_lookback_short",
                        f"sigma lookback: fewer than {SIGMA_SESSIONS} non-early-close in-segment sessions before {day}"))
    else:
        missing = [d for d in sig if not _qqq(data, d)]
        if missing:
            reasons.append(("sigma_lookback_missing_session",
                            "sigma lookback sessions without QQQ regular bars: " + ", ".join(missing)))
    return reasons


def sigma_undefined_minutes(data: SegmentData, i: int) -> list[str] | None:
    """Diagnostic only: minutes t in 10:29..15:54 where sigma(t) would be undefined for sessions[i] (some of the
    14 sessions lacks bar t or its 09:30 bar). None when the 14 sessions are not available."""
    sig = sigma_sessions(data.sessions, i)
    if sig is None:
        return None
    have = [{b.t for b in _qqq(data, d)} for d in sig]
    return [fmt_t(t) for t in range(SIGMA_FIRST_T, SIGMA_LAST_T + 1)
            if any(R.OPEN not in h or t not in h for h in have)]


def day_inputs(data: SegmentData, i: int) -> dict:
    """Same keys as `store.load_bars` for sessions[i], prior = the V10_LOOKBACK calendar sessions before it.

    Call only when lookback_reasons(data, i) is empty.
    """
    day = data.sessions[i]
    prev_days = data.sessions[i - V10_LOOKBACK:i]
    prior = [PriorSession(d, tuple(_qqq(data, d))) for d in prev_days]
    prev = prev_days[-1]
    return {"regular": {s: list(data.regular.get(day, {}).get(s, [])) for s in R.SYMBOLS},
            "qqq_pre": list(data.qqq_pre.get(day, [])),
            "prior": prior, "prev_session": prev,
            "prev_times": {s: {b.t for b in data.regular.get(prev, {}).get(s, [])} for s in R.SYMBOLS}}


# ---------------------------------------------------------------- mask


def build_mask(datas: list[SegmentData], cal: dict, sources: dict, ext: dict | None = None) -> dict:
    """Scored dates per segment and every excluded date with its reasons. S0 days stay in (v1.0 decides S0).

    ext (optional): external snapshots; only their hashes and per-day VIX/VIX9D availability (session dates vs
    CSV dates) are recorded. They never change which days are scored."""
    for data in datas:  # refuse before any per-day work if a segment day lies outside the event calendar
        for day in data.sessions:
            day_events(cal, day)
    segments: dict[str, dict] = {}
    for data in datas:
        scored: list[str] = []
        excluded: list[dict] = []
        sigma_diag: dict[str, list[str] | None] = {}
        for i, day in enumerate(data.sessions):
            ctx = day_context(cal, day)
            s0 = bool(ctx["events"]["fomc"] or ctx["events"]["early_close"])
            reasons = lookback_reasons(data, i, s0)
            if not reasons:
                inp = day_inputs(data, i)
                q = data_quality(inp["regular"], inp["prior"], ctx["events"]["early_close"], ctx["decision_t"],
                                 inp["prev_times"], ctx["expected_prev"], ctx["prev_early_close"])
                reasons = [("data_quality", f"data_quality: {x}") for x in q["issues"]]
            if reasons:
                excluded.append({"date": day, "kinds": sorted({k for k, _ in reasons}),
                                 "reasons": [r for _, r in reasons]})
            else:
                scored.append(day)
                sigma_diag[day] = sigma_undefined_minutes(data, i)
        with_data = [d for d in data.sessions if _qqq(data, d)]
        segments[data.segment.name] = {
            "start": data.segment.start.isoformat(), "end": data.segment.end.isoformat(),
            "calendar_sessions": len(data.sessions),
            "first_session_with_qqq_bars": with_data[0] if with_data else None,
            "last_session_with_qqq_bars": with_data[-1] if with_data else None,
            "scored_count": len(scored), "excluded_count": len(excluded),
            "scored": scored, "excluded": excluded,
            "excluded_by_kind": _by_kind(excluded),
            "sigma_undefined_minutes_diagnostic": sigma_diag,
            "external_availability": external_availability(ext, scored) if ext is not None else None,
            "db_rows_sha256": dict(data.row_sha256), "db_row_counts": dict(data.row_counts),
        }
    return {"schema": MASK_SCHEMA, "prereg": PREREG_PATH,
            "rules": {"v10_lookback_sessions": V10_LOOKBACK, "sigma_sessions": SIGMA_SESSIONS,
                      "sigma_condition": "14 prior non-early-close sessions inside the segment, each with QQQ "
                                         "regular bars; not applied to S0 days; minute gaps are not a mask "
                                         "condition (runtime: sigma(t) undefined -> that T is skipped)",
                      "sigma_diagnostic_minutes": f"{fmt_t(SIGMA_FIRST_T)}~{fmt_t(SIGMA_LAST_T)} (+ 09:30 Open)",
                      "db_row_hash_fields": list(ROW_HASH_FIELDS),
                      "sigma_skips_early_close": True, "s0_days_kept": True,
                      "data_quality": "src.daytrade_paper.core.data_quality (full session, decision time per events)"},
            "segments": segments, "sources": sources,
            "external_snapshot_sha256": dict(ext["sha256"]) if ext is not None else None}


def _by_kind(excluded: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in excluded:
        for k in e["kinds"]:
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def check_segment_facts(mask: dict) -> None:
    """Refuse unless the first/last sessions with QQQ bars match the fixed segment facts (dates only)."""
    for name, (first, last) in SEGMENT_FACTS.items():
        seg = mask["segments"][name]
        got = (seg["first_session_with_qqq_bars"], seg["last_session_with_qqq_bars"])
        if got[0] != first or (last is not None and got[1] != last):
            raise MaskRefused(f"segment {name}: sessions with QQQ bars run {got[0]}..{got[1]}, "
                              f"expected first {first}" + (f", last {last}" if last else ""))


def mask_bytes(mask: dict) -> bytes:
    return (json.dumps(mask, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def write_mask(path: Path, mask: dict) -> str:
    """Write-once; returns the SHA-256 of the file bytes."""
    raw = mask_bytes(mask)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(raw)
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- external prior-day values


def load_external(vix_csv: Path, vix9d_csv: Path, dix_csv: Path | None) -> dict:
    """Parsed snapshot rows and their SHA-256 (no download)."""
    out: dict = {"rows": {}, "sha256": {}, "files": {}}
    for key, path, parse in (("vix", vix_csv, parse_cboe), ("vix9d", vix9d_csv, parse_cboe),
                             ("gex", dix_csv, parse_dix)):
        if path is None:
            out["rows"][key] = None
            continue
        raw = Path(path).read_bytes()
        out["rows"][key] = parse(raw.decode("utf-8", errors="replace"))
        out["sha256"][key] = hashlib.sha256(raw).hexdigest()
        out["files"][key] = str(path)
    return out


def external_availability(ext: dict, days: list[str]) -> dict:
    """VIX/VIX9D prior-day status per day from CSV dates only (values are not reported)."""
    out = {}
    for key in ("vix", "vix9d"):
        status = {d: prior_value(ext["rows"][key], d)["status"] for d in days}
        bad = {d: st for d, st in status.items() if st != "ok"}
        out[key] = {"non_ok_count": len(bad), "non_ok": bad}
    return out


def risk_mode(external: dict) -> str:
    return high_mode(external["vix"]["value"], external["vix9d"]["value"])[0]


def external_for(ext: dict, day: str) -> dict:
    res = {}
    for key, rows in ext["rows"].items():
        if rows is None:
            res[key] = {"value": None, "date": None, "status": "missing: no snapshot given"}
        else:
            res[key] = {**prior_value(rows, day), "sha256": ext["sha256"][key]}
    return res
