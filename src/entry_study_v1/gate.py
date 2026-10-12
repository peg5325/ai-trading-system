"""Data-quality gate G1..G4 (PREREG §5.2). Runs before any P&L; the same thresholds for all three pairs."""
from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from itertools import pairwise

import numpy as np

from src.daytrade_paper import rules as R
from src.entry_study_v1.bars import candle_bar
from src.entry_study_v1.data import Segment, SymbolData, et_midnight
from src.entry_study_v1.mask import is_full_session
from src.history import exchange_calendar as xcal
from src.history.oos_guard import research_load

GATE_SCHEMA = "entry_study_v1_gate/v1"
G1_MIN = 0.95
LABELS = list(range(R.OPEN + 1, R.OPEN + R.FULL_SESSION_BARS + 1))  # DB labels 09:31..16:00


def normal_sessions(seg: Segment, skip: tuple[Date, ...] = ()) -> list[Date]:
    """Non-early-close NYSE sessions of the scoring window, minus `skip` (OOS: the Paper week)."""
    return [d for d in xcal.trading_days(seg.score_start, seg.score_end)
            if not xcal.is_early_close(d) and d not in skip]


def complete_days(seg: Segment, data: dict[str, SymbolData], syms: tuple[str, ...],
                  skip: tuple[Date, ...] = ()) -> list[Date]:
    """Normal sessions in the scoring window where every symbol has the full 390-bar session (§5.1 condition 2)."""
    return [d for d in normal_sessions(seg, skip)
            if all(is_full_session(data[s].regular.get(d.isoformat(), [])) for s in syms)]


def g1(seg: Segment, data: dict[str, SymbolData], syms: tuple[str, ...], skip: tuple[Date, ...] = ()) -> dict:
    normal = normal_sessions(seg, skip)
    ok = complete_days(seg, data, syms, skip)
    ratio = len(ok) / len(normal) if normal else 0.0
    return {"complete_sessions": len(ok), "normal_sessions": len(normal), "ratio": ratio, "pass": ratio >= G1_MIN}


def g3(seg: Segment, data: dict[str, SymbolData], syms: tuple[str, ...], skip: tuple[Date, ...] = ()) -> dict:
    """On complete days, the DB labels of the regular bars are exactly 09:31..16:00."""
    bad = []
    for d in complete_days(seg, data, syms, skip):
        for s in syms:
            if sorted(data[s].labels.get(d.isoformat(), [])) != LABELS:
                bad.append(f"{d.isoformat()}:{s}")
    return {"violations": len(bad), "violation_list": bad[:50], "pass": not bad}


def _changes(bars) -> dict[int, float]:
    """Close-to-close change per start minute within one day."""
    out = {}
    for prev, cur in pairwise(bars):
        out[cur.t] = cur.close / prev.close - 1
    return out


def g4(segs: tuple[Segment, ...], seg_data: dict[str, dict[str, SymbolData]], ref: str, etf: str,
       inverse: bool, skip: tuple[Date, ...] = ()) -> dict:
    """Correlation of 1-minute close changes of ETF(t) with ref(t + lag), lags -1, 0, +1, pooled over complete
    days (price data: development performance loads). Pass: lag 0 is the largest (absolute value for inverse)."""
    pooled = {lag: ([], []) for lag in (-1, 0, 1)}
    for seg in segs:
        data = seg_data[seg.name]
        for d in complete_days(seg, data, (ref, etf), skip):
            ce, cr = _changes(data[etf].regular[d.isoformat()]), _changes(data[ref].regular[d.isoformat()])
            for lag, (xs, ys) in pooled.items():
                for t, v in ce.items():
                    if t + lag in cr:
                        xs.append(v)
                        ys.append(cr[t + lag])
    corr = {}
    for lag, (xs, ys) in pooled.items():
        corr[lag] = float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 2 else None
    if any(v is None for v in corr.values()):
        return {"corr": {str(k): v for k, v in corr.items()}, "pass": False}
    score = {lag: (abs(v) if inverse else v) for lag, v in corr.items()}
    return {"corr": {str(k): v for k, v in corr.items()}, "pass": score[0] > score[-1] and score[0] > score[1]}


def boundary_dates(db, symbols: tuple[str, ...], segments: tuple[Segment, ...]) -> dict:
    """G2 (report only): regular-session dates, no prices, before the first segment and in gaps between segments."""
    windows = {f"before_{segments[0].name}": (xcal.CALENDAR_START, segments[0].data_start)}
    for a, b in pairwise(segments):
        windows[f"gap_{a.name}_{b.name}"] = (a.data_end + timedelta(days=1), b.data_start)
    out: dict = {}
    for name, (lo, hi) in windows.items():
        per = {}
        for sym in symbols:
            dates: set[str] = set()
            for c in research_load(str(db), sym, "1m", start=et_midnight(lo), end=et_midnight(hi),
                                   purpose="coverage"):
                parsed = candle_bar(c)
                if parsed is not None and parsed[1] == "REGULAR":
                    dates.add(parsed[0])
            per[sym] = sorted(dates)
        out[name] = {"start": lo.isoformat(), "end_exclusive": hi.isoformat(), "regular_dates": per}
    return out


def pair_gate(pair: str, ref: str, long_etf: str, short_etf: str, segs: tuple[Segment, ...],
              cov: dict[str, dict[str, SymbolData]], perf: dict[str, dict[str, SymbolData]],
              skip: tuple[Date, ...] = ()) -> dict:
    """cov / perf: loaded data (coverage / performance purpose) by segment and symbol. G4 uses perf."""
    syms = (ref, long_etf, short_etf)
    res = {"g1": {s.name: g1(s, cov[s.name], syms, skip) for s in segs},
           "g3": {s.name: g3(s, cov[s.name], syms, skip) for s in segs},
           "g4": {long_etf: g4(segs, perf, ref, long_etf, False, skip),
                  short_etf: g4(segs, perf, ref, short_etf, True, skip)}}
    ok = (all(v["pass"] for v in res["g1"].values()) and all(v["pass"] for v in res["g3"].values())
          and all(v["pass"] for v in res["g4"].values()))
    res["status"] = "PASS" if ok else "DATA_GATE_FAIL"
    return res
