"""Situation classification S1/S2/S3/S5 (contract §3). Each decision sees only bars before it."""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    before,
    by_t,
    fmt_t,
    vwap_through,
    window,
)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _num(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def opening_signals(
    inputs: dict, qqq: list[Bar], prior: list[PriorSession], decision_t: int
) -> dict:
    """QQQ opening-window values at decision_t, using bars with t < decision_t only."""
    hist = before(qqq, decision_t)
    bt = by_t(hist)
    win = window(hist, R.OPEN, decision_t)
    prev_close = inputs["prev_close"]
    first, last = bt.get(R.OPEN), bt.get(decision_t - 1)
    rng = (max(b.high for b in win) - min(b.low for b in win)) / prev_close if win else None
    vol = sum(b.volume for b in win)
    past = [sum(b.volume for b in window(list(s.bars), R.OPEN, decision_t)) for s in prior[-R.RVOL_LOOKBACK :]]
    past_avg = sum(past) / len(past) if past else 0.0
    return {
        "decision_time": fmt_t(decision_t),
        "opening_window": f"{fmt_t(R.OPEN)}~{fmt_t(decision_t - 1)}",
        "open_0930": first.open if first else None,
        "last_close": last.close if last else None,
        "vwap": vwap_through(hist, decision_t - 1),
        "range_pct": rng,
        "range_min_pct": R.RANGE_ATR_MULT * inputs["atr14_pct"] if inputs["atr14_pct"] else None,
        "rvol": vol / past_avg if past_avg > 0 else None,
        "window_volume": vol,
        "rvol_baseline_avg": past_avg,
        "rvol_baseline_sessions": len(past),
    }


def classify_open(inputs: dict, sig: dict) -> dict:
    """S1/S2 checks from opening signals. Returns {"label": "S1"|"S2"|None, "S1": [...], "S2": [...]}."""
    gap, bull = inputs["gap"], inputs["bull"]
    c, o, v = sig["last_close"], sig["open_0930"], sig["vwap"]
    rvol, rng, rmin = sig["rvol"], sig["range_pct"], sig["range_min_pct"]
    have = None not in (gap, c, o, v)
    rvol_ok = rvol is not None and rvol >= R.RVOL_MIN
    range_ok = rng is not None and rmin is not None and rng >= rmin
    rvol_txt = f"RVOL {_num(rvol)} (기준 ≥ {R.RVOL_MIN})"
    range_txt = f"범위 {_pct(rng)} (기준 ≥ 0.4×ATR14 = {_pct(rmin)})"
    t = sig["decision_time"]
    s1 = [
        _check("gap", have and (gap >= R.GAP_STRONG or (bull and gap >= 0)),
               f"갭 {_pct(gap)} (기준 ≥ +0.30% 또는 BULL·갭 ≥ 0; {'BULL' if bull else 'BEAR'})"),
        _check("close_gt_open", have and c > o, f"{t} 직전 봉 종가 {_num(c)} > 09:30 시가 {_num(o)}"),
        _check("close_gt_vwap", have and c > v, f"직전 봉 종가 {_num(c)} > QQQ VWAP {_num(v)}"),
        _check("rvol", rvol_ok, rvol_txt),
        _check("range", range_ok, range_txt),
    ]
    s2 = [
        _check("gap", have and (gap <= -R.GAP_STRONG or (not bull and gap <= 0)),
               f"갭 {_pct(gap)} (기준 ≤ −0.30% 또는 BEAR·갭 ≤ 0; {'BULL' if bull else 'BEAR'})"),
        _check("close_lt_open", have and c < o, f"{t} 직전 봉 종가 {_num(c)} < 09:30 시가 {_num(o)}"),
        _check("close_lt_vwap", have and c < v, f"직전 봉 종가 {_num(c)} < QQQ VWAP {_num(v)}"),
        _check("rvol", rvol_ok, rvol_txt),
        _check("range", range_ok, range_txt),
    ]
    label = "S1" if all(x["ok"] for x in s1) else "S2" if all(x["ok"] for x in s2) else None
    return {"label": label, "S1": s1, "S2": s2}


def classify_s3(inputs: dict, qqq: list[Bar], t3: int) -> dict:
    """S3 checks at t3 (10:00, or 10:15 on indicator days). Caller ensures S1/S2 did not fire."""
    hist = before(qqq, t3)
    bt = by_t(hist)
    last = bt.get(t3 - 1)
    v = vwap_through(hist, t3 - 1)
    cutoff = t3 - R.S3_LOW_CUTOFF_OFFSET
    early = window(hist, R.OPEN, cutoff)
    late = window(hist, cutoff, t3)
    low_early = min((b.low for b in early), default=None)
    low_late = min((b.low for b in late), default=None)
    gap = inputs["gap"]
    c5 = last.close if last else None
    checks = [
        _check("gap", gap is not None and gap <= R.S3_GAP, f"갭 {_pct(gap)} (기준 ≤ −1.00%)"),
        _check("bull", inputs["bull"], f"BULL(전일 종가 {_num(inputs['prev_close'])} > SMA50 {_num(inputs['sma50'])})"),
        _check("not_high", not inputs["high"], f"위험 모드 {inputs['risk_mode']} (HIGH 아님 필요)"),
        _check("close5m_gt_vwap", None not in (c5, v) and c5 > v,
               f"{fmt_t(t3 - 5)} 5분봉 종가 {_num(c5)} > QQQ VWAP {_num(v)}"),
        _check("low_held", None not in (low_early, low_late) and low_late > low_early,
               f"{fmt_t(cutoff)} 전 저점 {_num(low_early)}, 이후 저점 {_num(low_late)} (이후가 더 높아야 함)"),
    ]
    return {"label": "S3" if all(x["ok"] for x in checks) else None, "checks": checks,
            "decision_time": fmt_t(t3), "session_low_before_cutoff": low_early,
            "low_after_cutoff": low_late, "vwap": v, "close_5m": c5}


def classify_s5(inputs: dict, qqq: list[Bar]) -> dict:
    """S5 at 14:45 from the 14:44 QQQ close vs the prior close."""
    last = by_t(before(qqq, R.S5_DECISION)).get(R.S5_DECISION - 1)
    move = last.close / inputs["prev_close"] - 1 if last else None
    side = None
    if move is not None and move >= R.S5_MOVE:
        side = R.LONG
    elif move is not None and move <= -R.S5_MOVE:
        side = R.SHORT
    return {"label": "S5" if side else None, "symbol": side, "move": move,
            "detail": f"14:44 QQQ 종가 {_num(last.close if last else None)} / 전일 종가 "
                      f"{_num(inputs['prev_close'])} − 1 = {_pct(move)} (기준 |±2.00%|)"}
