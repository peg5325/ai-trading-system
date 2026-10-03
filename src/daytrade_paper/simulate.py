"""Bar-by-bar virtual fills for one session (contract §3-§4). No orders are ever sent."""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    by_t,
    data_quality,
    fmt_t,
    preopen_inputs,
    vwap_through,
    window,
)
from src.daytrade_paper.situation import (
    classify_open,
    classify_s3,
    classify_s5,
    opening_signals,
)


def buy_fill(price: float) -> float:
    return price * (1 + R.SLIPPAGE)


def sell_fill(price: float) -> float:
    return price * (1 - R.SLIPPAGE)


def sell_net(shares: float, raw_price: float) -> dict:
    fill = sell_fill(raw_price)
    proceeds = shares * fill
    commission = proceeds * R.COMMISSION
    sec = proceeds * R.SEC_FEE
    return {"fill": fill, "proceeds": proceeds, "commission": commission, "sec_fee": sec,
            "net": proceeds - commission - sec}


def size_notional(equity: float, stop_frac: float, high: bool, room: float | None = None) -> float:
    """Notional = equity×0.5% ÷ stop fraction, capped at 35% of equity; halved in HIGH mode.

    room: remaining combined same-direction cap (already HIGH-adjusted) when S5 adds to S1/S2.
    """
    notional = min(equity * R.RISK_PER_TRADE / stop_frac, equity * R.CAP_SINGLE)
    if high:
        notional *= R.HIGH_SIZE_MULT
    if room is not None:
        notional = min(notional, room)
    return max(notional, 0.0)


def walk_exit(
    kind: str,
    bars: list[Bar],
    qqq: list[Bar],
    entry_t: int,
    stop: float,
    prev_close: float,
    forced_t: int = R.FORCED_EXIT,
) -> dict:
    """Find the exit of a long position in `bars` (TQQQ or SQQQ) entered at the open of entry_t.

    Per bar, in order: (1) a pending/forced exit at the open, but if the open is at or below
    the stop it is a stop at the open (stop first); (2) intrabar stop at min(stop, open);
    (3) end-of-bar signals: S3 target at the close, 5m close < VWAP -> exit next bar open.
    """
    bt, qt = by_t(bars), by_t(qqq)
    check_from = {"S1": R.S1S2_EXIT_CHECK_FROM, "S2": R.S1S2_EXIT_CHECK_FROM,
                  "S3": R.S3_EXIT_CHECK_FROM}.get(kind)
    pending: str | None = None
    for t in range(entry_t, forced_t + 1):
        b = bt.get(t)
        if b is None:
            continue
        if pending or t == forced_t:
            if b.open <= stop:
                return {"t": t, "at": "open", "raw": b.open, "reason": "STOP_GAP"}
            return {"t": t, "at": "open", "raw": b.open, "reason": pending or "FORCED_1555"}
        if b.low <= stop:
            gap = b.open < stop
            return {"t": t, "at": "open" if gap else "intrabar", "raw": min(stop, b.open),
                    "reason": "STOP_GAP" if gap else "STOP"}
        if kind == "S3":
            q = qt.get(t)
            if q is not None and q.high >= prev_close:
                return {"t": t, "at": "close", "raw": b.close, "reason": "S3_TARGET_PREV_CLOSE"}
        if check_from is not None and t >= check_from + 4 and (t - R.OPEN) % 5 == 4:
            v = vwap_through(bars, t)
            if v is not None and b.close < v:
                pending = "VWAP_5M"
    raise ValueError("no exit found; forced exit bar missing")


def closed_by(exit_: dict, t: int) -> bool:
    return exit_["t"] < t or (exit_["t"] == t and exit_["at"] == "open")


EXIT_REASON_KO = {
    "STOP": "손절가 도달",
    "STOP_GAP": "손절가 아래로 갭(불리한 시가 체결)",
    "VWAP_5M": "5분봉 종가 < VWAP → 다음 봉 시가 청산",
    "S3_TARGET_PREV_CLOSE": "QQQ 고가가 전일 종가 도달 → 그 봉 종가 청산",
    "FORCED_1555": "15:55 봉 시가 일괄 청산",
}


def _open_trade(kind: str, sym: str, t: int, bars: list[Bar], stop_raw: float | None,
                stop_frac_fixed: float | None, equity: float, high: bool,
                room: float | None) -> tuple[dict | None, str | None]:
    bar = by_t(bars).get(t)
    if bar is None:
        return None, f"{sym} {fmt_t(t)} 봉 없음"
    fill = buy_fill(bar.open)
    if stop_frac_fixed is not None:
        stop = fill * (1 - stop_frac_fixed)
    else:
        stop = stop_raw
    stop_frac = (fill - stop) / fill
    if stop_frac <= 0:
        return None, f"손절가 {stop:.4f} ≥ 진입가 {fill:.4f} → 진입 안 함"
    if stop_frac > R.MAX_STOP_FRAC + 1e-12:
        return None, f"손절폭 {stop_frac * 100:.2f}% > 1.5% 상한 → 진입 안 함"
    notional = size_notional(equity, stop_frac, high, room)
    if notional <= 0:
        return None, "합산 상한에 여유 없음 → 진입 안 함"
    return {"kind": kind, "symbol": sym, "side": "BUY", "entry_t": t, "entry_raw": bar.open,
            "entry_fill": fill, "stop": stop, "stop_frac": stop_frac, "notional": notional,
            "shares": notional / fill, "buy_commission": notional * R.COMMISSION,
            "sizing_equity": equity, "high_halved": high}, None


def _close_trade(trade: dict, exit_: dict) -> dict:
    s = sell_net(trade["shares"], exit_["raw"])
    pnl = s["net"] - trade["notional"] - trade["buy_commission"]
    return {**trade, "exit_t": exit_["t"], "exit_at": exit_["at"], "exit_raw": exit_["raw"],
            "exit_fill": s["fill"], "exit_reason": exit_["reason"],
            "exit_reason_ko": EXIT_REASON_KO[exit_["reason"]], "proceeds": s["proceeds"],
            "sell_commission": s["commission"], "sec_fee": s["sec_fee"], "pnl": pnl,
            "entry_time": fmt_t(trade["entry_t"]), "exit_time": fmt_t(exit_["t"])}


def _mark_pnl(trade: dict, bars: list[Bar], t: int) -> float:
    """Liquidation value P&L after costs using the close of bar t-1."""
    last = by_t(bars).get(t - 1)
    if last is None:
        return 0.0
    return sell_net(trade["shares"], last.close)["net"] - trade["notional"] - trade["buy_commission"]


def run_day(
    day: str,
    regular: dict[str, list[Bar]],
    qqq_pre: list[Bar],
    prior: list[PriorSession],
    external: dict,
    events: dict,
    equity_start: float,
) -> dict:
    """Simulate one session. events: {"fomc", "early_close", "indicator", "types", ...}."""
    base = {"date": day, "rules_version": R.RULES_VERSION, "label": R.LABEL,
            "contract": R.CONTRACT_PATH, "events": events, "external": external,
            "equity_start": equity_start, "trades": [], "no_trade_reasons": [],
            "situations": [], "checks": {}, "halt": {"triggered": False}}
    quality = data_quality(regular, prior, bool(events.get("early_close")))
    base["data_quality"] = quality
    if not quality["ok"]:
        base["situation"] = "INCOMPLETE_DATA"
        base["situations"] = ["INCOMPLETE_DATA"]
        base["no_trade_reasons"].append("데이터 불완전: " + "; ".join(quality["issues"]))
        return _finish(base, regular, equity_start, 0.0)

    qqq = regular[R.SIGNAL]
    inputs = preopen_inputs(day, prior, qqq_pre, qqq, external)
    base["inputs"] = inputs
    high = inputs["high"]
    if inputs["risk_mode"] == "UNKNOWN_TREATED_AS_HIGH":
        base["no_trade_reasons"].append("VIX/VIX9D 전일값 일부 없음 → 위험 모드 미확인, HIGH로 간주(크기 절반, S3 불가)")

    if events.get("fomc") or events.get("early_close"):
        why = "FOMC 결정일" if events.get("fomc") else "조기폐장일"
        base["situation"] = "S0"
        base["situations"] = ["S0"]
        base["checks"]["S0"] = why
        base["no_trade_reasons"].append(f"S0({why}): 하루 종일 진입 없음")
        return _finish(base, regular, equity_start, 0.0)

    decision_t = R.DECISION_INDICATOR_DAY if events.get("indicator") else R.DECISION
    t3 = R.DECISION_INDICATOR_DAY if events.get("indicator") else R.S3_DECISION
    sig = opening_signals(inputs, qqq, prior, decision_t)
    base["opening"] = sig
    op = classify_open(inputs, sig)
    base["checks"]["S1"], base["checks"]["S2"] = op["S1"], op["S2"]
    realized = 0.0
    primary = None
    primary_bars: list[Bar] = []
    if op["label"]:
        label = op["label"]
        sym = R.LONG if label == "S1" else R.SHORT
        bars = regular[sym]
        open_low = min(b.low for b in window(bars, R.OPEN, decision_t))
        trade, why = _open_trade(label, sym, decision_t, bars, open_low, None, equity_start, high, None)
        base["checks"][f"{label}_stop"] = f"{sym} 개장 구간 최저가 {open_low:.4f}"
        if trade is None:
            base["no_trade_reasons"].append(f"{label} 조건 충족, 그러나 {why}")
        else:
            primary, primary_bars = trade, bars
    else:
        s3 = classify_s3(inputs, qqq, t3)
        base["checks"]["S3"] = s3
        label = s3["label"] or "S4"
        if s3["label"]:
            bars = regular[R.LONG]
            sess_low = min(b.low for b in window(bars, R.OPEN, t3))
            stop = sess_low * (1 - R.S3_STOP_BUFFER)
            base["checks"]["S3_stop"] = f"TQQQ 세션 저점 {sess_low:.4f} −0.2% = {stop:.4f}"
            trade, why = _open_trade("S3", R.LONG, t3, bars, stop, None, equity_start, high, None)
            if trade is None:
                base["no_trade_reasons"].append(f"S3 조건 충족, 그러나 {why}")
            else:
                primary, primary_bars = trade, bars
        else:
            base["no_trade_reasons"].append(
                f"S4: {fmt_t(decision_t)} S1·S2, {fmt_t(t3)} S3 조건 미충족 → 오전 진입 없음")
    base["situation"] = label
    base["situations"] = [label]

    closed: list[dict] = []
    if primary is not None:
        ex = walk_exit(primary["kind"], primary_bars, qqq, primary["entry_t"], primary["stop"],
                       inputs["prev_close"])
        primary_closed = _close_trade(primary, ex)
        closed.append(primary_closed)

    s5 = classify_s5(inputs, qqq)
    base["checks"]["S5"] = s5
    if s5["label"]:
        base["situations"].append("S5")
        day_pnl = 0.0
        open_same = 0.0
        if closed:
            p = closed[0]
            if closed_by({"t": p["exit_t"], "at": p["exit_at"]}, R.S5_DECISION):
                realized = p["pnl"]
                day_pnl = realized
            else:
                day_pnl = _mark_pnl(primary, primary_bars, R.S5_DECISION)
                if p["symbol"] == s5["symbol"]:
                    open_same = p["notional"]
        limit = -R.DAILY_LOSS_HALT * equity_start
        if day_pnl <= limit:
            base["halt"] = {"triggered": True, "day_pnl_at_check": day_pnl, "limit": limit,
                            "time": fmt_t(R.S5_DECISION)}
            base["no_trade_reasons"].append(
                f"S5 신호 있었으나 일일 손실 {day_pnl:.2f} ≤ 한도 {limit:.2f}(−3%) → 신규 진입 없음")
        else:
            room = None
            if open_same > 0:
                cap = (equity_start + realized) * R.CAP_COMBINED_SAME_DIR
                room = cap * (R.HIGH_SIZE_MULT if high else 1.0) - open_same
            sym = s5["symbol"]
            bars = regular[sym]
            trade, why = _open_trade("S5", sym, R.S5_DECISION, bars, None, R.S5_STOP_FRAC,
                                     equity_start + realized, high, room)
            if trade is None:
                base["no_trade_reasons"].append(f"S5 신호, 그러나 {why}")
            else:
                ex = walk_exit("S5", bars, qqq, R.S5_DECISION, trade["stop"], inputs["prev_close"])
                closed.append(_close_trade(trade, ex))
    else:
        base["no_trade_reasons"].append(f"S5 미발동: {s5['detail']}")

    base["trades"] = closed
    return _finish(base, regular, equity_start, sum(t["pnl"] for t in closed))


def _ret(bars: list[Bar]) -> float | None:
    if not bars:
        return None
    return bars[-1].close / bars[0].open - 1


def _finish(base: dict, regular: dict[str, list[Bar]], equity_start: float, pnl: float) -> dict:
    qqq_ret, tqqq_ret = _ret(regular.get(R.SIGNAL, [])), _ret(regular.get(R.LONG, []))
    base["pnl"] = {"day_pnl": pnl, "equity_end": equity_start + pnl,
                   "day_return": pnl / equity_start if equity_start else None,
                   "costs": sum(t["buy_commission"] + t["sell_commission"] + t["sec_fee"]
                                for t in base["trades"])}
    base["benchmarks"] = {
        "QQQ_open_to_close": qqq_ret,
        "TQQQ_open_to_close": tqqq_ret,
        "QQQ_hold_pnl": equity_start * qqq_ret if qqq_ret is not None else None,
        "TQQQ_hold_pnl": equity_start * tqqq_ret if tqqq_ret is not None else None,
        "note": "1배(QQQ)·TQQQ 시가→종가 전액 보유, 비용 미차감",
    }
    return base
