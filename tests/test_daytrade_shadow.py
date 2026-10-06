"""R1 shadow engine: minute-by-minute replay must reproduce Paper's run_day exactly. Synthetic bars only."""
from __future__ import annotations

import pytest
from test_daytrade_paper import (
    EXT_OK,
    NO_EVENTS,
    PREV,
    PRIOR,
    QC,
    T,
    bars,
    s1_day,
    tqqq_default,
)

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar
from src.daytrade_shadow.engine import ExitWatcher, ShadowDay
from src.daytrade_shadow.replay import diff, paper_vs_shadow, replay_day


def s2_day():
    """QQQ gaps −0.5% and falls through the opening window with 2x volume; SQQQ climbs -> S2."""
    qqq = bars(lambda t: PREV * 0.995 - 0.01 * min(t - R.OPEN, 15), spread=0.5, vol=2000.0)
    sq = bars(lambda t: 20.0 + 0.01 * min(t - R.OPEN, 15), spread=0.01)
    tq = bars(lambda t: 50.0, spread=0.02)
    pre = [Bar(T(9, 0), PREV * 0.995, PREV * 0.995, PREV * 0.995, PREV * 0.995, 10)]
    return {"QQQ": qqq, "TQQQ": tq, "SQQQ": sq}, pre


def s3_day():
    """Gap −1.2%, early low at 09:35 held after 09:50, 09:59 close above VWAP; rallies to the prior close at 11:00."""
    base = PREV * 0.988

    def q(t):
        if t < T(10, 0):
            return base
        return base if t < T(11, 0) else PREV + 0.2

    qqq = []
    for t in range(R.OPEN, R.LAST_BAR + 1):
        p = q(t)
        lo = p - 1.0 if t == T(9, 35) else p - 0.3
        c = p + 0.4 if t == T(9, 59) else p
        qqq.append(Bar(t, p, max(p, c) + 0.1, lo, c, 1000.0))
    tq = bars(lambda t: 50.0, spread=0.1)
    sq = bars(lambda t: 20.0, spread=0.02)
    pre = [Bar(T(9, 0), base, base, base, base, 10)]
    return {"QQQ": qqq, "TQQQ": tq, "SQQQ": sq}, pre


def vwap_exit_day():
    """S1, then TQQQ slips under its session VWAP (still above the stop) -> VWAP_5M exit."""
    return s1_day(tqqq_fn=lambda t: tqqq_default(t) if t < T(10, 30) else 50.12)


def gap_stop_day():
    return s1_day(tqqq_fn=lambda t: tqqq_default(t) if t < T(11, 0) else 49.0)


def halt_day():
    return s1_day(qqq_after=lambda t: PREV * 1.03,
                  tqqq_fn=lambda t: tqqq_default(t) if t < T(10, 0) else 30.0)


def gap_at_s5_day():
    """Morning S1 stop gapped through exactly at the 14:45 open while S5 fires (closed_by at 14:45)."""
    return s1_day(qqq_after=lambda t: PREV * 1.03,
                  tqqq_fn=lambda t: 49.5 if t >= T(14, 45) else tqqq_default(t))


def gap_at_s5_equal_stop_day():
    """Open exactly AT the stop at 14:45 is an intrabar STOP in walk_exit (not closed by the 14:45 open)."""
    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03)
    stop = min(b.low for b in reg["TQQQ"] if R.OPEN <= b.t < R.DECISION)
    reg["TQQQ"] = [b if b.t != T(14, 45) else Bar(b.t, stop, stop, stop - 0.1, stop, b.volume) for b in reg["TQQQ"]]
    return reg, pre


def wide_stop_day():
    return s1_day(tqqq_fn=lambda t: 49.0 if t == T(9, 31) else tqqq_default(t))


SCENARIOS = {
    "S1_forced_or_vwap": (s1_day, NO_EVENTS, EXT_OK),
    "S1_plus_S5_combined_cap": (lambda: s1_day(qqq_after=lambda t: PREV * 1.03), NO_EVENTS, EXT_OK),
    "S1_vwap_exit": (vwap_exit_day, NO_EVENTS, EXT_OK),
    "S1_gap_stop": (gap_stop_day, NO_EVENTS, EXT_OK),
    "S1_gap_stop_at_1445_with_S5": (gap_at_s5_day, NO_EVENTS, EXT_OK),
    "S1_open_at_stop_1445_with_S5": (gap_at_s5_equal_stop_day, NO_EVENTS, EXT_OK),
    "S1_wide_stop_no_entry": (wide_stop_day, NO_EVENTS, EXT_OK),
    "S1_halt_blocks_S5": (halt_day, NO_EVENTS, EXT_OK),
    "S1_unknown_risk_half": (s1_day, NO_EVENTS, {}),
    "S2": (s2_day, NO_EVENTS, EXT_OK),
    "S3_target": (s3_day, NO_EVENTS, EXT_OK),
    "S4": (lambda: s1_day(qqq_vol=1000.0), NO_EVENTS, EXT_OK),
    "S0_fomc": (lambda: s1_day(qqq_after=lambda t: PREV * 1.05), {**NO_EVENTS, "fomc": True, "types": ["FOMC"]},
                EXT_OK),
    "indicator_1015": (s1_day, {**NO_EVENTS, "indicator": True, "types": ["CPI"]}, EXT_OK),
    "S5_short_only": (lambda: s1_day(qqq_vol=1000.0, qqq_after=lambda t: PREV * 0.97), NO_EVENTS, EXT_OK),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_replay_matches_run_day(name):
    build, events, external = SCENARIOS[name]
    reg, pre = build()
    out = paper_vs_shadow("2026-10-05", reg, pre, PRIOR, external, events, 2000.0, **QC)
    assert out["paper_quality_ok"] and out["shadow_quality_ok"]
    assert out["diffs"] == []


def test_scenarios_cover_every_exit_and_situation():
    seen_sit, seen_exit = set(), set()
    for build, events, external in SCENARIOS.values():
        reg, pre = build()
        r, _ = replay_day("2026-10-05", reg, pre, PRIOR, external, events, 2000.0, **QC)
        seen_sit.update(r["situations"])
        seen_exit.update(t["exit_reason"] for t in r["trades"])
    assert {"S0", "S1", "S2", "S3", "S4", "S5"} <= seen_sit
    assert {"STOP_GAP", "VWAP_5M", "S3_TARGET_PREV_CLOSE", "FORCED_1555"} <= seen_exit


def test_decision_does_not_see_the_future():
    """Feeding a bar from after `now` would be look-ahead; the engine decides from what it is given."""
    reg, pre = s1_day()
    eng = ShadowDay("2026-10-05", PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    complete = {s: [b for b in reg[s] if b.t < R.DECISION] for s in R.SYMBOLS}
    opens = {s: next(b.open for b in reg[s] if b.t == R.DECISION) for s in R.SYMBOLS}
    ev = eng.step(R.DECISION, complete, opens, pre)
    assert [e["event"] for e in ev] == ["SITUATION", "ENTRY"]
    assert ev[1]["t"] == "09:45" and eng.open_positions()[0]["symbol"] == R.LONG


def test_missed_decision_minute_stops_instead_of_deciding_late():
    reg, pre = s1_day()
    eng = ShadowDay("2026-10-05", PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    late = R.DECISION + 2
    ev = eng.step(late, {s: [b for b in reg[s] if b.t < late] for s in R.SYMBOLS}, {}, pre)
    assert ev[-1]["event"] == "STOPPED" and eng.positions == []


def test_live_quality_refuses_a_hole_in_the_opening_window():
    reg, pre = s1_day()
    holed = {s: [b for b in reg[s] if b.t < R.DECISION and not (s == "TQQQ" and b.t == T(9, 40))]
             for s in R.SYMBOLS}
    eng = ShadowDay("2026-10-05", PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    ev = eng.step(R.DECISION, holed, {s: 1.0 for s in R.SYMBOLS}, pre)
    assert ev[0]["event"] == "INCOMPLETE_DATA" and eng.base["situations"] == ["INCOMPLETE_DATA"]


def test_exit_watcher_waits_for_a_late_bar_like_walk_exit_skips_a_missing_one():
    w = ExitWatcher("S1", T(9, 45), stop=49.0, prev_close=100.0)
    bt = {t: Bar(t, 50.0, 50.1, 49.9, 50.0, 1.0) for t in range(T(9, 45), T(16, 0)) if t != T(10, 0)}
    w.advance(T(10, 1), bt, bt, list(bt.values()), None)  # 10:00 missing -> skipped like walk_exit
    assert w.exit is None and w.t == T(10, 1)
    w.advance(T(15, 55), bt, bt, list(bt.values()), 50.0)
    assert w.exit == {"t": T(15, 55), "at": "open", "raw": 50.0, "reason": "FORCED_1555"}


def _candle(kst: str, o=1.0, h=1.0, lo=1.0, c=1.0, v=1.0):
    return {"timestamp": kst, "openPrice": str(o), "highPrice": str(h), "lowPrice": str(lo),
            "closePrice": str(c), "volume": str(v)}


def test_toss_bar_matches_the_paper_store_conversion():
    from src.daytrade_paper.store import _bar
    from src.daytrade_shadow.bars import toss_bar

    # the same QQQ bar: REST timestamp in KST vs the collector's ET text (2026-10-02, label 09:31)
    day, t, bar = toss_bar(_candle("2026-10-02T22:31:00.000+09:00", 751.31, 751.49, 750.02, 750.04, 469723))
    pday, session, pbar = _bar(("2026-10-02T09:31:00-04:00", "REGULAR", 751.31, 751.49, 750.02, 750.04, 469723.0))
    assert (day, t, bar) == (pday, pbar.t, pbar) and session == "REGULAR" and t == T(9, 30)
    assert toss_bar(_candle("2026-10-02T22:26:00.000+09:00"))[1] == T(9, 25)


def test_live_bars_split_complete_and_in_progress():
    from src.daytrade_shadow.bars import LiveBars

    lb = LiveBars("2026-10-02", T(16, 0))
    lb.merge("QQQ", [_candle("2026-10-02T22:47:00.000+09:00", o=7.0),   # bar 09:46, in progress at 09:46
                     _candle("2026-10-02T22:46:00.000+09:00", o=6.0),   # bar 09:45, complete
                     _candle("2026-10-02T22:25:00.000+09:00", o=5.0)],  # bar 09:24, pre-market
             T(9, 46))
    assert [b.t for b in lb.complete(T(9, 46))["QQQ"]] == [T(9, 45)]
    assert lb.opens(T(9, 46))["QQQ"] == 7.0 and lb.opens(T(9, 47))["QQQ"] is None
    assert [b.t for b in lb.qqq_pre()] == [T(9, 24)]
    assert lb.missing("QQQ", R.OPEN, T(9, 46))[0] == R.OPEN


def _as_toss(day: str, b: Bar) -> dict:
    """A Paper bar as Toss REST returns it: labelled by its END minute, in KST."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=ZoneInfo("America/New_York"))
    end = (start + timedelta(minutes=b.t + 1)).astimezone(ZoneInfo("Asia/Seoul"))
    return _candle(end.isoformat(), b.open, b.high, b.low, b.close, b.volume)


@pytest.mark.parametrize("name", ["S1_plus_S5_combined_cap", "S3_target", "S1_vwap_exit", "S2"])
def test_live_path_from_toss_candles_matches_run_day(name):
    """Toss-shaped candles -> LiveBars (end-label shift, in-progress split) -> ShadowDay == run_day."""
    from src.daytrade_paper.simulate import run_day
    from src.daytrade_shadow.bars import LiveBars

    build, events, external = SCENARIOS[name]
    reg, pre = build()
    day = "2026-10-05"
    feed = {s: [_as_toss(day, b) for b in (reg[s] + (pre if s == "QQQ" else []))] for s in R.SYMBOLS}
    lb = LiveBars(day, T(16, 0))
    eng = ShadowDay(day, PRIOR, external, events, 2000.0, **QC)
    for now in range(T(9, 25), T(16, 2)):
        for s in R.SYMBOLS:  # a poll at now+4s sees every bar that has started (the newest is in progress)
            lb.merge(s, [c for c, b in zip(feed[s], reg[s] + (pre if s == "QQQ" else [])) if b.t <= now], now)
        if now >= R.OPEN:
            eng.step(now, lb.complete(now), lb.opens(now), lb.qqq_pre())
    shadow = eng.finish(lb.regular())
    paper = run_day(day, reg, pre, PRIOR, external, events, 2000.0, **QC)
    assert diff(paper, shadow) == []


def test_compare_checks_situation_entry_and_size():
    from scripts.run_daytrade_shadow import compare

    reg, pre = s1_day(qqq_after=lambda t: PREV * 1.03)
    paper, _ = replay_day("2026-10-05", reg, pre, PRIOR, EXT_OK, NO_EVENTS, 2000.0, **QC)
    assert compare(paper, paper)["pass"] is True
    other = {**paper, "trades": [{**paper["trades"][0], "notional": paper["trades"][0]["notional"] * 1.05},
                                 paper["trades"][1]]}
    out = compare(other, paper)
    assert out["pass"] is False and out["same_entries"] and not out["size_within_1pct"]
    assert compare({**paper, "situations": ["S4"]}, paper)["same_situations"] is False


def test_diff_reports_paths():
    assert diff({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}]}) == ["/a[1]/b: 2 != 3"]
    assert diff({"data_quality": 1, "x": 1}, {"data_quality": 2, "x": 1}) == []
