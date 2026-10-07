"""§6.2 pre-run count: v1.0 morning entries and their state after the 10:30 open stage, counts only.

Each scored day runs the shadow engine (`ShadowDay`, the Paper-matched minute-by-minute mirror of run_day) the
way `replay_day` does, but the last step is now = 10:30: that step processes complete bars t < 10:30 and the
open of bar 10:30 (gap stop / pending exit at the open). The intrabar and the close of bar 10:30 are never
processed, and the engine is only ever given bars with t <= 10:30. No price, fill, exit price or P&L is output.
"""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar, PriorSession
from src.daytrade_shadow.engine import ShadowDay
from src.daytrade_v11.data import (
    SegmentData,
    day_context,
    day_inputs,
    external_for,
    risk_mode,
)

COUNT_END = R.hm(10, 30)
MORNING = ("S1", "S2", "S3")
DIRECTIONS = (R.LONG, R.SHORT)
RISK_MODES = ("NORMAL", "HIGH", "UNKNOWN_TREATED_AS_HIGH")


class CountInvariantError(RuntimeError):
    """A masked day behaved like an unmasked one (e.g. the engine found incomplete data)."""


def count_day(day: str, regular: dict[str, list[Bar]], qqq_pre: list[Bar], prior: list[PriorSession],
              external: dict, events: dict, equity_start: float, *, prev_times: dict[str, set[int]],
              expected_prev: str, prev_early_close: bool = False) -> dict:
    """{"entry": "S1"|"S2"|"S3"|None, "symbol": ..., "open_after_1030": bool} for one day."""
    eng = ShadowDay(day, prior, external, events, equity_start, prev_times=prev_times,
                    expected_prev=expected_prev, prev_early_close=prev_early_close)
    ordered = {s: sorted((b for b in regular.get(s, []) if b.t <= COUNT_END), key=lambda b: b.t)
               for s in R.SYMBOLS}
    by = {s: {b.t: b for b in ordered[s]} for s in R.SYMBOLS}
    complete: dict[str, list[Bar]] = {s: [] for s in R.SYMBOLS}
    idx = dict.fromkeys(R.SYMBOLS, 0)
    for now in range(R.OPEN, COUNT_END + 1):
        for s in R.SYMBOLS:
            while idx[s] < len(ordered[s]) and ordered[s][idx[s]].t < now:
                complete[s].append(ordered[s][idx[s]])
                idx[s] += 1
        opens = {s: (by[s][now].open if now in by[s] else None) for s in R.SYMBOLS}
        eng.step(now, {s: list(v) for s, v in complete.items()}, opens, qqq_pre)
    if eng.state == "STOPPED" and eng.base.get("situation") != "S0":
        raise CountInvariantError(f"{day}: engine stopped on a masked day: {eng.base['no_trade_reasons']}")
    morning = [(tr, w) for tr, w in eng.positions if tr["kind"] in MORNING]
    if not morning:
        return {"entry": None, "symbol": None, "open_after_1030": False}
    tr, w = morning[0]
    return {"entry": tr["kind"], "symbol": tr["symbol"], "open_after_1030": w.exit is None}


def _empty() -> dict:
    return {"days": 0, "entry_days": 0, "entries": dict.fromkeys(MORNING, 0),
            "entries_by_direction": dict.fromkeys(DIRECTIONS, 0),
            "open_after_1030_days": 0, "open_after_1030": dict.fromkeys(DIRECTIONS, 0),
            "risk_mode": dict.fromkeys(RISK_MODES, 0), "external_non_ok": {"vix": 0, "vix9d": 0}}


def _add(acc: dict, r: dict) -> None:
    acc["days"] += 1
    if r["entry"] is None:
        return
    acc["entry_days"] += 1
    acc["entries"][r["entry"]] += 1
    acc["entries_by_direction"][r["symbol"]] += 1
    if r["open_after_1030"]:
        acc["open_after_1030_days"] += 1
        acc["open_after_1030"][r["symbol"]] += 1


def run_count(mask: dict, datas: list[SegmentData], cal: dict, ext: dict) -> dict:
    """Counts per segment and in total over the mask's scored dates only."""
    out: dict = {"segments": {}, "total": _empty(), "equity_start": R.START_EQUITY,
                 "stage": "through the 10:30 open (gap stop / pending exit at the 10:30 open); "
                          "10:30 intrabar and close excluded"}
    for data in datas:
        seg = mask["segments"][data.segment.name]
        acc = _empty()
        pos = {d: i for i, d in enumerate(data.sessions)}
        for day in seg["scored"]:
            ctx = day_context(cal, day)
            inp = day_inputs(data, pos[day])
            external = external_for(ext, day)
            acc["risk_mode"][risk_mode(external)] += 1
            for key in ("vix", "vix9d"):
                acc["external_non_ok"][key] += external[key]["status"] != "ok"
            r = count_day(day, inp["regular"], inp["qqq_pre"], inp["prior"], external,
                          ctx["events"], R.START_EQUITY, prev_times=inp["prev_times"],
                          expected_prev=ctx["expected_prev"], prev_early_close=ctx["prev_early_close"])
            _add(acc, r)
        out["segments"][data.segment.name] = acc
    tot = out["total"]
    for acc in out["segments"].values():
        for k in ("days", "entry_days", "open_after_1030_days"):
            tot[k] += acc[k]
        for k in ("entries", "entries_by_direction", "open_after_1030", "risk_mode", "external_non_ok"):
            for kk, v in acc[k].items():
                tot[k][kk] += v
    return out
