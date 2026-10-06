"""Minute-by-minute mirror of `src.daytrade_paper.simulate.run_day` (R1 shadow run, no orders).

At ET minute `now` a live process knows the complete 1m bars that started before `now` and the open of
bar `now` (the in-progress REST bar). `ShadowDay.step` is fed exactly that. It calls the Paper rule
functions unchanged and splits `walk_exit` into an open step and a close step per bar, so replaying a
finished session must reproduce `run_day` (see tests/test_daytrade_shadow.py and `replay`).

Paper code is not modified: the Paper five-session check (contract §7) runs on it.
"""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import (
    Bar,
    PriorSession,
    data_quality,
    decision_time,
    fmt_t,
    preopen_inputs,
    vwap_through,
    window,
)
from src.daytrade_paper.simulate import (
    _close_trade,
    _finish,
    _mark_pnl,
    _open_trade,
    closed_by,
)
from src.daytrade_paper.situation import (
    classify_open,
    classify_s3,
    classify_s5,
    opening_signals,
)


class ExitWatcher:
    """`walk_exit` for one long position, advanced as bars arrive.

    Per bar t, in walk_exit's order: open step (pending or forced exit at the open, stop first), then
    close step (intrabar stop, S3 target, 5m close < VWAP sets a pending exit). Missing bars are skipped.
    """

    def __init__(self, kind: str, entry_t: int, stop: float, prev_close: float,
                 forced_t: int = R.FORCED_EXIT):
        self.kind, self.stop, self.prev_close, self.forced_t = kind, stop, prev_close, forced_t
        self.check_from = {"S1": R.S1S2_EXIT_CHECK_FROM, "S2": R.S1S2_EXIT_CHECK_FROM,
                           "S3": R.S3_EXIT_CHECK_FROM}.get(kind)
        self.t = entry_t  # next bar to process
        self.opened = False  # open step of bar self.t already done
        self.pending: str | None = None
        self.exit: dict | None = None

    def advance(self, now: int, product: dict[int, Bar], qqq: dict[int, Bar], product_bars: list[Bar],
                open_now: float | None) -> None:
        """Process complete bars t < now, then the open of bar `now` when it is known."""
        while self.exit is None and self.t <= self.forced_t:
            t = self.t
            if t < now:
                b = product.get(t)
                if b is not None:
                    if not self.opened:
                        self._open(t, b.open)
                    if self.exit is None:
                        self._close(t, b, qqq.get(t), product_bars)
                self.t, self.opened = t + 1, False
            elif t == now and open_now is not None and not self.opened:
                self._open(t, open_now)
                self.opened = True
                return
            else:
                return

    def _open(self, t: int, open_: float) -> None:
        if self.pending or t == self.forced_t:
            reason = "STOP_GAP" if open_ <= self.stop else (self.pending or "FORCED_1555")
            self.exit = {"t": t, "at": "open", "raw": open_, "reason": reason}
        elif open_ < self.stop:
            # walk_exit finds this in its intrabar branch (low <= stop, open < stop -> STOP_GAP "at open"),
            # but it is already decided by the open; resolving it here keeps closed_by(…, 14:45) identical.
            self.exit = {"t": t, "at": "open", "raw": open_, "reason": "STOP_GAP"}

    def _close(self, t: int, b: Bar, q: Bar | None, product_bars: list[Bar]) -> None:
        if b.low <= self.stop:
            gap = b.open < self.stop
            self.exit = {"t": t, "at": "open" if gap else "intrabar", "raw": min(self.stop, b.open),
                         "reason": "STOP_GAP" if gap else "STOP"}
            return
        if self.kind == "S3" and q is not None and q.high >= self.prev_close:
            self.exit = {"t": t, "at": "close", "raw": b.close, "reason": "S3_TARGET_PREV_CLOSE"}
            return
        if self.check_from is not None and t >= self.check_from + 4 and (t - R.OPEN) % 5 == 4:
            v = vwap_through(product_bars, t)
            if v is not None and b.close < v:
                self.pending = "VWAP_5M"


def live_quality(complete: dict[str, list[Bar]], prior: list[PriorSession], decision_t: int,
                 prev_times: dict[str, set[int]], expected_prev: str, prev_early_close: bool) -> dict:
    """Data checks a live run can make at decision_t.

    History checks are `data_quality`'s own (fed a complete placeholder grid for today); today's check is
    that every bar of the opening window [09:30, decision_t) is present for each symbol.
    """
    grid = {s: [Bar(t, 1.0, 1.0, 1.0, 1.0, 1.0) for t in range(R.OPEN, R.LAST_BAR + 1)] for s in R.SYMBOLS}
    q = data_quality(grid, prior, False, decision_t, prev_times, expected_prev, prev_early_close)
    issues = list(q["issues"])
    for sym in R.SYMBOLS:
        ts = {b.t for b in complete.get(sym, [])}
        missing = [t for t in range(R.OPEN, decision_t) if t not in ts]
        if missing:
            issues.append(f"{sym} 개장 구간 봉 {len(missing)}개 누락(첫 누락 {fmt_t(missing[0])})")
    return {"ok": not issues, "issues": issues, "scope": f"live: history + {fmt_t(R.OPEN)}~{fmt_t(decision_t - 1)}",
            "prior_sessions": len(prior), "expected_prev_session": expected_prev,
            "db_prev_session": q["db_prev_session"]}


def _with_open(bars: list[Bar], t: int, open_: float | None) -> list[Bar]:
    """Complete bars plus an open-only placeholder for bar t (only its open is read at entry)."""
    if open_ is None:
        return bars
    return [*bars, Bar(t, open_, open_, open_, open_, 0.0)]


class ShadowDay:
    """One session of run_day, decided at the same minutes from what is known at each minute."""

    def __init__(self, day: str, prior: list[PriorSession], external: dict, events: dict,
                 equity_start: float, *, prev_times: dict[str, set[int]], expected_prev: str,
                 prev_early_close: bool = False):
        self.day, self.prior, self.external, self.events = day, prior, external, events
        self.equity_start = equity_start
        self.prev_times, self.expected_prev, self.prev_early_close = prev_times, expected_prev, prev_early_close
        self.decision_t = decision_time(events)
        self.t3 = R.DECISION_INDICATOR_DAY if events.get("indicator") else R.S3_DECISION
        self.base = {"date": day, "rules_version": R.RULES_VERSION, "label": R.LABEL,
                     "contract": R.CONTRACT_PATH, "events": events, "external": external,
                     "equity_start": equity_start, "trades": [], "no_trade_reasons": [],
                     "situations": [], "checks": {}, "halt": {"triggered": False}}
        self.state = "WAIT"  # WAIT -> OPEN_DONE -> (S3_DONE) -> S5_DONE | STOPPED
        self.inputs: dict | None = None
        self.label: str | None = None
        self.positions: list[tuple[dict, ExitWatcher]] = []  # (trade, watcher), primary first
        self.realized = 0.0
        self.last_now: int | None = None

    # ---- feed ----
    def step(self, now: int, complete: dict[str, list[Bar]], opens: dict[str, float | None],
             qqq_pre: list[Bar]) -> list[dict]:
        """Advance to ET minute `now`. complete: bars with t < now per symbol; opens: open of bar `now`."""
        if self.last_now is not None and now <= self.last_now:
            return []
        self.last_now = now
        events: list[dict] = []
        self._advance_exits(now, complete, opens, events)
        if self.state == "WAIT" and now >= self.decision_t:
            if now > self.decision_t:
                self._stop(f"판단 시각 {fmt_t(self.decision_t)}을 놓침(첫 처리 {fmt_t(now)})", events)
            else:
                self._decide_open(now, complete, opens, qqq_pre, events)
        if self.state == "OPEN_DONE" and self.label is None and now >= self.t3:
            if now > self.t3:
                self._stop(f"S3 판단 시각 {fmt_t(self.t3)}을 놓침", events)
            else:
                self._decide_s3(now, complete, opens, events)
        if self.state in ("OPEN_DONE", "S3_DONE") and now >= R.S5_DECISION:
            if now > R.S5_DECISION:
                self._stop(f"S5 판단 시각 {fmt_t(R.S5_DECISION)}을 놓침", events)
            else:
                self._decide_s5(now, complete, opens, events)
        if self.state in ("OPEN_DONE", "S3_DONE", "S5_DONE"):
            self._advance_exits(now, complete, opens, events)  # new positions start this minute
        return events

    def open_positions(self) -> list[dict]:
        return [tr for tr, w in self.positions if w.exit is None]

    def finish(self, regular: dict[str, list[Bar]]) -> dict:
        """Close the books after the session (full bars give the same benchmarks as Paper)."""
        unresolved = [tr["kind"] for tr, w in self.positions if w.exit is None]
        if unresolved:
            raise ValueError(f"no exit found for {unresolved}; forced exit bar missing")
        closed = [_close_trade(tr, w.exit) for tr, w in self.positions]
        self.base["trades"] = closed
        total = sum(t["pnl"] for t in closed)
        if "limit" in self.base["halt"]:
            self.base["halt"]["reached_by_close"] = total <= self.base["halt"]["limit"]
        return _finish(self.base, regular, self.equity_start, total)

    # ---- phases (same order and wording as run_day) ----
    def _decide_open(self, now, complete, opens, qqq_pre, events) -> None:
        base = self.base
        quality = live_quality(complete, self.prior, self.decision_t, self.prev_times, self.expected_prev,
                               self.prev_early_close)
        base["data_quality"] = quality
        if not quality["ok"]:
            base["situation"] = "INCOMPLETE_DATA"
            base["situations"] = ["INCOMPLETE_DATA"]
            base["no_trade_reasons"].append("데이터 불완전: " + "; ".join(quality["issues"]))
            self.state = "STOPPED"
            events.append({"t": fmt_t(now), "event": "INCOMPLETE_DATA", "issues": quality["issues"]})
            return
        qqq = complete[R.SIGNAL]
        inputs = preopen_inputs(self.day, self.prior, qqq_pre, qqq, self.external)
        self.inputs = inputs
        base["inputs"] = inputs
        if inputs["risk_mode"] == "UNKNOWN_TREATED_AS_HIGH":
            base["no_trade_reasons"].append("VIX/VIX9D 전일값 일부 없음 → 위험 모드 미확인, HIGH로 간주(크기 절반, S3 불가)")
        if self.events.get("fomc") or self.events.get("early_close"):
            why = "FOMC 결정일" if self.events.get("fomc") else "조기폐장일"
            base["situation"] = "S0"
            base["situations"] = ["S0"]
            base["checks"]["S0"] = why
            base["no_trade_reasons"].append(f"S0({why}): 하루 종일 진입 없음")
            self.state = "STOPPED"
            events.append({"t": fmt_t(now), "event": "SITUATION", "situation": "S0", "why": why})
            return
        sig = opening_signals(inputs, qqq, self.prior, self.decision_t)
        base["opening"] = sig
        op = classify_open(inputs, sig)
        base["checks"]["S1"], base["checks"]["S2"] = op["S1"], op["S2"]
        self.state = "OPEN_DONE"
        if op["label"]:
            label = op["label"]
            self.label = label
            sym = R.LONG if label == "S1" else R.SHORT
            bars = complete[sym]
            open_low = min(b.low for b in window(bars, R.OPEN, self.decision_t))
            trade, why = _open_trade(label, sym, self.decision_t, _with_open(bars, now, opens.get(sym)),
                                     open_low, None, self.equity_start, inputs["high"], None)
            base["checks"][f"{label}_stop"] = f"{sym} 개장 구간 최저가 {open_low:.4f}"
            base["situation"] = label
            base["situations"] = [label]
            events.append({"t": fmt_t(now), "event": "SITUATION", "situation": label, "rvol": sig["rvol"]})
            if trade is None:
                base["no_trade_reasons"].append(f"{label} 조건 충족, 그러나 {why}")
                events.append({"t": fmt_t(now), "event": "NO_ENTRY", "why": why})
            else:
                self._enter(trade, inputs, events)

    def _decide_s3(self, now, complete, opens, events) -> None:
        base, inputs = self.base, self.inputs
        qqq = complete[R.SIGNAL]
        s3 = classify_s3(inputs, qqq, self.t3)
        base["checks"]["S3"] = s3
        label = s3["label"] or "S4"
        self.label = label
        self.state = "S3_DONE"
        base["situation"] = label
        base["situations"] = [label]
        events.append({"t": fmt_t(now), "event": "SITUATION", "situation": label})
        if s3["label"]:
            bars = complete[R.LONG]
            sess_low = min(b.low for b in window(bars, R.OPEN, self.t3))
            stop = sess_low * (1 - R.S3_STOP_BUFFER)
            base["checks"]["S3_stop"] = f"TQQQ 세션 저점 {sess_low:.4f} −0.2% = {stop:.4f}"
            trade, why = _open_trade("S3", R.LONG, self.t3, _with_open(bars, now, opens.get(R.LONG)), stop, None,
                                     self.equity_start, inputs["high"], None)
            if trade is None:
                base["no_trade_reasons"].append(f"S3 조건 충족, 그러나 {why}")
                events.append({"t": fmt_t(now), "event": "NO_ENTRY", "why": why})
            else:
                self._enter(trade, inputs, events)
        else:
            base["no_trade_reasons"].append(
                f"S4: {fmt_t(self.decision_t)} S1·S2, {fmt_t(self.t3)} S3 조건 미충족 → 오전 진입 없음")

    def _decide_s5(self, now, complete, opens, events) -> None:
        base, inputs = self.base, self.inputs
        qqq = complete[R.SIGNAL]
        s5 = classify_s5(inputs, qqq)
        base["checks"]["S5"] = s5
        self.state = "S5_DONE"
        day_pnl = 0.0
        open_same = 0.0
        realized = 0.0
        if self.positions:
            primary, w = self.positions[0]
            if w.exit is not None and closed_by(w.exit, R.S5_DECISION):
                realized = _close_trade(primary, w.exit)["pnl"]
                day_pnl = realized
            else:
                day_pnl = _mark_pnl(primary, complete[primary["symbol"]], R.S5_DECISION)
                if primary["symbol"] == s5["symbol"]:
                    open_same = primary["notional"]
        self.realized = realized
        limit = -R.DAILY_LOSS_HALT * self.equity_start
        reached = day_pnl <= limit
        base["halt"] = {"triggered": reached, "day_pnl_at_check": day_pnl, "limit": limit,
                        "time": fmt_t(R.S5_DECISION), "s5_signal": bool(s5["label"])}
        events.append({"t": fmt_t(now), "event": "S5_CHECK", "s5": s5["label"], "move": s5["move"],
                       "day_pnl": day_pnl, "halt": reached})
        if s5["label"]:
            base["situations"].append("S5")
            if reached:
                base["no_trade_reasons"].append(
                    f"S5 신호 있었으나 일일 손실 {day_pnl:.2f} ≤ 한도 {limit:.2f}(−3%) → 신규 진입 없음")
            else:
                room = None
                if open_same > 0:
                    cap = (self.equity_start + realized) * R.CAP_COMBINED_SAME_DIR
                    room = cap * (R.HIGH_SIZE_MULT if inputs["high"] else 1.0) - open_same
                sym = s5["symbol"]
                trade, why = _open_trade("S5", sym, R.S5_DECISION, _with_open(complete[sym], now, opens.get(sym)),
                                         None, R.S5_STOP_FRAC, self.equity_start + realized, inputs["high"], room)
                if trade is None:
                    base["no_trade_reasons"].append(f"S5 신호, 그러나 {why}")
                    events.append({"t": fmt_t(now), "event": "NO_ENTRY", "why": why})
                else:
                    self._enter(trade, inputs, events)
        else:
            base["no_trade_reasons"].append(f"S5 미발동: {s5['detail']}")
            if reached:
                base["no_trade_reasons"].append(
                    f"14:45 일일 손실 {day_pnl:.2f} ≤ 한도 {limit:.2f}(−3%) 도달 → 이후 신규 진입 없음")

    # ---- helpers ----
    def _enter(self, trade: dict, inputs: dict, events: list[dict]) -> None:
        w = ExitWatcher(trade["kind"], trade["entry_t"], trade["stop"], inputs["prev_close"])
        self.positions.append((trade, w))
        events.append({"t": fmt_t(trade["entry_t"]), "event": "ENTRY", "kind": trade["kind"],
                       "symbol": trade["symbol"], "entry_fill": trade["entry_fill"], "stop": trade["stop"],
                       "notional": trade["notional"], "shares": trade["shares"]})

    def _advance_exits(self, now, complete, opens, events) -> None:
        for trade, w in self.positions:
            if w.exit is not None:
                continue
            sym = trade["symbol"]
            product = complete.get(sym, [])
            w.advance(now, {b.t: b for b in product}, {b.t: b for b in complete.get(R.SIGNAL, [])},
                      product, opens.get(sym))
            if w.exit is not None:
                events.append({"t": fmt_t(now), "event": "EXIT", "kind": trade["kind"], "symbol": sym,
                               "exit_t": fmt_t(w.exit["t"]), "reason": w.exit["reason"], "raw": w.exit["raw"]})

    def _stop(self, why: str, events: list[dict]) -> None:
        self.base["no_trade_reasons"].append(f"그림자 운용 중단: {why}")
        self.state = "STOPPED"
        events.append({"event": "STOPPED", "why": why})
