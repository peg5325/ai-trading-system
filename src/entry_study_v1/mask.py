"""Scored-date mask and data manifest (PREREG §5.1, §8).

The mask depends on dates, bar presence and bar value sanity (positive finite prices, volume >= 0, the 15% gap
filter). It is built before any P&L exists. The same code serves the development mask (performance loads) and
the OOS mask (coverage loads: prices are read for conditions 5 and 6 only, no return is computed).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date as Date
from datetime import timedelta
from pathlib import Path

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar
from src.daytrade_paper.external import events_for
from src.entry_study_v1.bars import ROW_HASH_FIELDS, StudyRefused
from src.entry_study_v1.config import PREREG_PATH
from src.entry_study_v1.data import Segment, SymbolData
from src.history import exchange_calendar as xcal

MASK_SCHEMA = "entry_study_v1_mask/v1"
MANIFEST_SCHEMA = "entry_study_v1_data_manifest/v1"
EVENT_TYPES = frozenset({"FOMC", "CPI", "NFP", "PCE"})
SIGMA_SESSIONS = 14
MAX_REF_GAP = 0.15
FULL_START, FULL_END = R.OPEN, R.OPEN + R.FULL_SESSION_BARS  # bar starts 570..959


class MaskRefused(StudyRefused):
    """Inputs cannot support a fixed mask (calendar coverage or calendar mismatch)."""


def prev_session(day: Date) -> Date:
    d = day - timedelta(days=1)
    while not xcal.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def sigma_dates(day: Date, seg: Segment, n: int = SIGMA_SESSIONS) -> list[Date] | None:
    """The n non-early-close NYSE sessions before `day`, all inside the data segment (oldest first), or None."""
    out: list[Date] = []
    d = prev_session(day)
    while d >= seg.data_start and len(out) < n:
        if not xcal.is_early_close(d):
            out.append(d)
        d = prev_session(d)
    return list(reversed(out)) if len(out) == n else None


def is_full_session(bars: list[Bar]) -> bool:
    """Exactly 390 bars, one per start minute 09:30..15:59 (no duplicates)."""
    return len(bars) == R.FULL_SESSION_BARS and {b.t for b in bars} == set(range(FULL_START, FULL_END))


def bars_valid(bars: list[Bar]) -> bool:
    return all(all(math.isfinite(v) and v > 0 for v in (b.open, b.high, b.low, b.close))
               and math.isfinite(b.volume) and b.volume >= 0 for b in bars)


def pc_bar(prev_bars: list[Bar], prev: Date) -> Bar | None:
    """The single bar starting at regular_close_minute - 1 of the previous session, else None."""
    want = xcal.regular_close_minute(prev) - 1
    hit = [b for b in prev_bars if b.t == want]
    return hit[0] if len(hit) == 1 else None


def day_events(cal: dict, day: Date) -> dict:
    ev = events_for(cal, day.isoformat())
    if not ev["known"]:
        raise MaskRefused(f"{day} is outside the event calendar coverage {cal['coverage']}; refusing")
    if ev["early_close"] != xcal.is_early_close(day):
        raise MaskRefused(f"{day}: early-close flag differs between events yaml and exchange calendar")
    return ev


def _day_reasons(day: Date, seg: Segment, data: dict[str, SymbolData], ref: str, etfs: tuple[str, str]) -> list[str]:
    reasons: list[str] = []
    syms = (ref, *etfs)
    iso = day.isoformat()
    today = {s: data[s].regular.get(iso, []) for s in syms}
    for s in syms:  # condition 2
        if not is_full_session(today[s]):
            ts = [b.t for b in today[s]]
            reasons.append(f"incomplete_session:{s}:{len(ts)} bars, {len(set(ts))} unique")
    prev = prev_session(day)  # condition 3
    pc = None
    if prev < seg.data_start:
        reasons.append(f"prev_session_outside_segment:{prev}")
    else:
        pc = pc_bar(data[ref].regular.get(prev.isoformat(), []), prev)
        if pc is None:
            reasons.append(f"pc_bar_missing:{prev}")
    sig = sigma_dates(day, seg)  # condition 4
    if sig is None:
        reasons.append("sigma_sessions_short")
    if reasons:
        return reasons  # value checks assume complete inputs
    check = [b for s in syms for b in today[s]] + [pc]  # condition 5
    for d in sig:
        check.extend(data[ref].regular.get(d.isoformat(), []))
    if not bars_valid(check):
        reasons.append("bad_value")
        return reasons
    o = next(b for b in today[ref] if b.t == R.OPEN).open  # condition 6
    gap = o / pc.close - 1
    if abs(gap) > MAX_REF_GAP:
        reasons.append(f"ref_gap_gt_15pct:{gap!r}")
    return reasons


def build_segment_mask(seg: Segment, data: dict[str, SymbolData], ref: str, etfs: tuple[str, str],
                       cal: dict, excluded_dates: tuple[Date, ...] = ()) -> dict:
    """Scored dates, excluded dates with reasons, and event days for one pair in one segment."""
    sessions = xcal.trading_days(seg.score_start, seg.score_end)
    for d in sessions:  # refuse before any per-day work if the event calendar does not cover the window
        day_events(cal, d)
    normal = [d for d in sessions if not xcal.is_early_close(d)]
    scored: list[str] = []
    excluded: list[dict] = []
    event_days: dict[str, list[str]] = {}
    for d in sessions:
        iso = d.isoformat()
        ev = day_events(cal, d)
        if d in excluded_dates:
            reasons = ["paper_week_already_seen"]
        elif xcal.is_early_close(d):
            reasons = ["early_close"]
        else:
            reasons = _day_reasons(d, seg, data, ref, etfs)
        if reasons:
            excluded.append({"date": iso, "reasons": reasons})
            continue
        scored.append(iso)
        types = sorted(EVENT_TYPES & set(ev["types"]))
        if types:
            event_days[iso] = types
    return {"start": seg.score_start.isoformat(), "end": seg.score_end.isoformat(),
            "data_start": seg.data_start.isoformat(), "data_end": seg.data_end.isoformat(),
            "normal_sessions": len(normal), "scored_count": len(scored), "excluded_count": len(excluded),
            "scored": scored, "excluded": excluded, "event_days": event_days}


def build_mask(pairs: dict, seg_data: dict[str, dict[str, SymbolData]], segments: tuple[Segment, ...],
               cal: dict, events_sha256: str, excluded_dates: tuple[Date, ...] = ()) -> dict:
    """pairs: {"P1": (ref, long_etf, short_etf)}; seg_data[segment name][symbol] -> SymbolData."""
    out = {}
    for name, (ref, long_etf, short_etf) in pairs.items():
        out[name] = {"ref": ref, "long": long_etf, "short": short_etf, "segments": {
            seg.name: build_segment_mask(seg, seg_data[seg.name], ref, (long_etf, short_etf), cal,
                                         excluded_dates)
            for seg in segments}}
    return {"schema": MASK_SCHEMA, "prereg": PREREG_PATH, "pairs": out,
            "events_calendar_sha256": events_sha256, "events_coverage": dict(cal["coverage"]),
            "rules": {"conditions": ["early_close", "390 unique regular bars for ref and both ETFs",
                                     "previous session in segment with PC bar", "14 normal sessions in segment",
                                     "finite positive prices and volume>=0 (day, PC bar, sigma sessions)",
                                     "ref |O/PC-1|<=15%"],
                      "excluded_dates": [d.isoformat() for d in excluded_dates]}}


def build_manifest(seg_data: dict[str, dict[str, SymbolData]], segments: tuple[Segment, ...],
                   range_override: dict[str, list[str]] | None = None) -> dict:
    rows = {}
    for seg in segments:
        for sym, sd in seg_data[seg.name].items():
            rows.setdefault(sym, {})[seg.name] = {"sha256": sd.row_sha256, "count": sd.row_count}
    ranges = range_override or {s.name: [s.data_start.isoformat(), s.data_end.isoformat()] for s in segments}
    return {"schema": MANIFEST_SCHEMA, "row_hash_fields": list(ROW_HASH_FIELDS), "segments": ranges,
            "rows": dict(sorted(rows.items()))}


def canonical_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_once(path: Path, raw: bytes) -> str:
    """Write-once; returns the SHA-256 of the file bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as f:
        f.write(raw)
    return sha256_bytes(raw)
