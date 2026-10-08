"""v1.3 minute-by-minute day simulator (PREREG_DAYTRADE_V1_3_SIZING §2, §3, §9).

Structure: the shadow engine's open stage / close stage split of `walk_exit` (`src.daytrade_shadow.engine`),
with v1.0's own decision functions (`preopen_inputs`, `opening_signals`, `classify_open`, `classify_s3`,
`classify_s5`, `data_quality`) called unchanged on the full day; each of them reads only bars before its decision
time, exactly as `run_day` calls them. Only sizing (`policy.size`), the risk controls and the stress options differ.

Fixed order inside bar T (§3, both arms):

1. account halt pending from the previous close: cancel pending entries, liquidate everything at the T open
   (STOP_GAP if open <= stop, else ACCOUNT_HALT). Never delayed.
2. open-stage exits, `walk_exit` order and labels (gap stop -> pending / 15:55 forced exit).
3. realized P&L update.
4. pre-entry risk check: realized through the T open + open positions at the T-1 close (`_mark_pnl(., T)`),
   <= -3% x day start equity -> daily lock. Evaluated at the bar of every entry signal and every entry fill
   (they differ only under the delay stress), and at 14:45 always, as v1.0 records its check there.
5. entries, only without lock and halt.
6. intrabar stops.
7. close check (risk-control arms): day P&L with open positions at the T close -> daily lock from T+1; liquidation
   value account (`_mark_pnl` basis) <= segment high-water mark x 0.65 -> account halt (step 1 at T+1).
   The high-water mark and the drawdown D are updated at every bar close in every arm.
8. close-stage signals (S3 target at the close, 5m VWAP pending exit).

With `V10_EXACT`, base costs and no delay this reproduces `run_day` (tests/test_daytrade_v13.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

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
from src.daytrade_paper.simulate import EXIT_REASON_KO as V10_EXIT_REASON_KO
from src.daytrade_paper.situation import (
    classify_open,
    classify_s3,
    classify_s5,
    opening_signals,
)
from src.daytrade_v13.policy import BASE_COSTS, Costs, Policy, finite_positive, size

ACCOUNT_HALT_LEVEL = 0.65  # 1 - 0.35 (Charter §5)
EXIT_REASON_KO = {**V10_EXIT_REASON_KO, "ACCOUNT_HALT": "계좌 −35% 중단 → 다음 봉 시가 전량 청산"}
STOP_REASONS = ("STOP", "STOP_GAP")
# Keys of a v1.0 trade dict (`simulate._open_trade` + `_close_trade`); extra v1.3 keys are reporting only.
V10_TRADE_KEYS = ("kind", "symbol", "side", "entry_t", "entry_raw", "entry_fill", "stop", "stop_frac", "notional",
                  "shares", "buy_commission", "sizing_equity", "high_halved", "exit_t", "exit_at", "exit_raw",
                  "exit_fill", "exit_reason", "exit_reason_ko", "proceeds", "sell_commission", "sec_fee", "pnl",
                  "entry_time", "exit_time")


class InvalidRun(Exception):
    """§3 invalid value / v1.1 §3.1 missing bar: the run stops and is invalid (verdict row 0)."""


@dataclass
class AccountState:
    """One independent account per segment (§9). Carried across mask gaps inside the segment."""

    equity: float = R.START_EQUITY
    hwm: float = R.START_EQUITY  # liquidation-value high-water mark (bar closes)
    halted: bool = False
    halt_event: dict | None = None
    max_dd: float = 0.0


# ---------------------------------------------------------------- fills and costs (v1.0 formulas, costs as input)


def buy_fill(price: float, costs: Costs) -> float:
    return price * (1 + costs.slippage)


def sell_net(shares: float, raw_price: float, costs: Costs) -> dict:
    fill = raw_price * (1 - costs.slippage)
    proceeds = shares * fill
    commission = proceeds * costs.commission
    sec = proceeds * costs.sec_fee
    return {"fill": fill, "proceeds": proceeds, "commission": commission, "sec_fee": sec,
            "net": proceeds - commission - sec}


def mark_pnl(trade: dict, price: float, costs: Costs) -> float:
    """`simulate._mark_pnl` with the mark price given: liquidation-value P&L after costs."""
    v = sell_net(trade["shares"], price, costs)["net"] - trade["notional"] - trade["buy_commission"]
    if not math.isfinite(v):
        raise InvalidRun(f"non-finite valuation of {trade['kind']} {trade['symbol']}")
    return v


def close_trade(trade: dict, exit_: dict, costs: Costs) -> dict:
    """`simulate._close_trade` with costs as input."""
    s = sell_net(trade["shares"], exit_["raw"], costs)
    pnl = s["net"] - trade["notional"] - trade["buy_commission"]
    if not math.isfinite(pnl):
        raise InvalidRun(f"non-finite P&L of {trade['kind']} {trade['symbol']}")
    return {**trade, "exit_t": exit_["t"], "exit_at": exit_["at"], "exit_raw": exit_["raw"],
            "exit_fill": s["fill"], "exit_reason": exit_["reason"],
            "exit_reason_ko": EXIT_REASON_KO[exit_["reason"]], "proceeds": s["proceeds"],
            "sell_commission": s["commission"], "sec_fee": s["sec_fee"], "pnl": pnl,
            "entry_time": fmt_t(trade["entry_t"]), "exit_time": fmt_t(exit_["t"])}


def open_trade(kind: str, sym: str, t: int, bar: Bar, stop_raw: float | None, stop_frac_fixed: float | None,
               equity: float, high: bool, policy: Policy, costs: Costs,
               open_positions: list[dict]) -> tuple[dict | None, str | None]:
    """`simulate._open_trade` with the v1.3 entry conditions (§2 change 1) and sizing (`policy.size`).

    v1.0 order: open <= stop -> no entry; stop distance <= 0 -> no entry; > cap -> no entry (arms with a cap).
    v1.3 adds: finite positive prices (a non-finite/non-positive open or equity invalidates the run, §3) and
    0 < stop < fill.
    """
    if not finite_positive(bar.open) or not finite_positive(equity):
        raise InvalidRun(f"{sym} {fmt_t(t)}: open {bar.open!r} / equity {equity!r} not a finite positive value")
    if stop_raw is not None and bar.open <= stop_raw:
        return None, f"진입 시가가 이미 손절가 아래(시가 {bar.open:.4f} ≤ 손절가 {stop_raw:.4f}) → 진입 안 함"
    fill = buy_fill(bar.open, costs)
    stop = fill * (1 - stop_frac_fixed) if stop_frac_fixed is not None else stop_raw
    if not (isinstance(stop, (int, float)) and math.isfinite(stop)) or not 0 < stop < fill:
        return None, f"손절가 {stop!r}가 0 < 손절가 < 진입가 {fill:.4f} 조건 밖 → 진입 안 함"
    stop_frac = (fill - stop) / fill
    if policy.max_stop_frac is not None and stop_frac > policy.max_stop_frac + 1e-12:
        return None, f"손절폭 {stop_frac * 100:.2f}% > {policy.max_stop_frac * 100:.1f}% 상한 → 진입 안 함"
    sz = size(policy, costs, equity, stop_frac, high, sym, open_positions)
    notional = sz["notional"]
    if notional <= 0:
        return None, f"크기 한도에 여유 없음({', '.join(sz['binding'])}) → 진입 안 함"
    return {"kind": kind, "symbol": sym, "side": "BUY", "entry_t": t, "entry_raw": bar.open,
            "entry_fill": fill, "stop": stop, "stop_frac": stop_frac, "notional": notional,
            "shares": notional / fill, "buy_commission": notional * costs.commission,
            "sizing_equity": equity, "high_halved": high,
            "sizing": {"limits": sz["limits"], "binding": sz["binding"],
                       "single_cap_binds": sz["single_cap_binds"]}}, None


# ---------------------------------------------------------------- one position (ExitWatcher split into steps)


class Position:
    """`walk_exit` for one long position, as the shadow engine's ExitWatcher, split into §3 steps 1/2, 6 and 8.

    `delay` (stress b) moves the fill of a pending signal exit one bar later; stops and the 15:55 forced exit are
    never delayed. A pending exit that is not yet due does not count as pending at the open of the waiting bar.
    """

    def __init__(self, trade: dict, prev_close: float, delay: int = 0, forced_t: int = R.FORCED_EXIT):
        self.trade, self.prev_close, self.delay, self.forced_t = trade, prev_close, delay, forced_t
        self.kind, self.stop = trade["kind"], trade["stop"]
        self.check_from = {"S1": R.S1S2_EXIT_CHECK_FROM, "S2": R.S1S2_EXIT_CHECK_FROM,
                           "S3": R.S3_EXIT_CHECK_FROM}.get(self.kind)
        self.pending: str | None = None
        self.pending_fill_t: int | None = None
        self.exit: dict | None = None

    def liquidate(self, t: int, b: Bar) -> None:
        """Step 1: account-halt liquidation at the open (gap-stop label per v1.0)."""
        reason = "STOP_GAP" if b.open <= self.stop else "ACCOUNT_HALT"
        self.exit = {"t": t, "at": "open", "raw": b.open, "reason": reason}

    def open_step(self, t: int, b: Bar) -> None:
        due = self.pending is not None and t >= self.pending_fill_t
        if due or t == self.forced_t:
            reason = "STOP_GAP" if b.open <= self.stop else (self.pending if due else "FORCED_1555")
            self.exit = {"t": t, "at": "open", "raw": b.open, "reason": reason}
        elif b.open < self.stop:
            # walk_exit's intrabar branch with open < stop (STOP_GAP "at open"), decided by the open (as ExitWatcher)
            self.exit = {"t": t, "at": "open", "raw": b.open, "reason": "STOP_GAP"}

    def intrabar_step(self, t: int, b: Bar) -> None:
        if b.low <= self.stop:
            gap = b.open < self.stop
            self.exit = {"t": t, "at": "open" if gap else "intrabar", "raw": min(self.stop, b.open),
                         "reason": "STOP_GAP" if gap else "STOP"}

    def close_step(self, t: int, b: Bar, q: Bar | None, product_bars: list[Bar]) -> None:
        if self.kind == "S3" and q is not None and q.high >= self.prev_close:
            self.exit = {"t": t, "at": "close", "raw": b.close, "reason": "S3_TARGET_PREV_CLOSE"}
            return
        if self.pending is None and self.check_from is not None and t >= self.check_from + 4 \
                and (t - R.OPEN) % 5 == 4:
            v = vwap_through(product_bars, t)
            if v is not None and b.close < v:
                self.pending, self.pending_fill_t = "VWAP_5M", t + 1 + self.delay


# ---------------------------------------------------------------- the day


def validate_prices(regular: dict[str, list[Bar]], qqq_pre: list[Bar], prior: list[PriorSession]) -> None:
    """§3: prices must be finite positive values; anything else invalidates the run (no invented fill rule)."""
    groups = [(s, regular.get(s, [])) for s in R.SYMBOLS] + [("QQQ PRE", qqq_pre)]
    groups += [(f"QQQ {p.date}", list(p.bars)) for p in prior]
    for name, bars in groups:
        for b in bars:
            if not all(finite_positive(x) for x in (b.open, b.high, b.low, b.close)):
                raise InvalidRun(f"{name} {fmt_t(b.t)}: price not a finite positive value")
            if not (isinstance(b.volume, (int, float)) and math.isfinite(b.volume) and b.volume >= 0):
                raise InvalidRun(f"{name} {fmt_t(b.t)}: volume {b.volume!r} invalid")


def _zero_day(base: dict, account: AccountState) -> dict:
    base["pnl"] = {"day_pnl": 0.0, "equity_end": account.equity, "day_return": 0.0}
    return base


def simulate_day(day: str, regular: dict[str, list[Bar]], qqq_pre: list[Bar], prior: list[PriorSession],
                 external: dict, events: dict, account: AccountState, policy: Policy, costs: Costs = BASE_COSTS,
                 delay: int = 0, *, prev_times: dict[str, set[int]], expected_prev: str,
                 prev_early_close: bool = False) -> dict:
    """Simulate one scored day for `account` (mutated: equity, high-water mark, halt state, drawdown)."""
    equity_start = account.equity
    base = {"date": day, "policy": policy.name, "delay": delay, "costs": vars(costs).copy(),
            "equity_start": equity_start, "trades": [], "situations": [], "no_trade_reasons": [],
            "blocked_entries": [], "lock": None, "check_1445": None, "account_halt": None,
            "halted_at_start": account.halted}
    quality = data_quality(regular, prior, bool(events.get("early_close")), decision_time(events),
                           prev_times, expected_prev, prev_early_close)
    if not quality["ok"]:
        raise InvalidRun(f"{day}: scored day fails v1.0 data quality: {quality['issues']}")
    validate_prices(regular, qqq_pre, prior)
    if account.halted:
        base["situations"] = ["ACCOUNT_HALTED"]
        base["no_trade_reasons"].append("계좌 중단 상태 → 구간 끝까지 신규 진입 없음(수익 0 관측)")
        return _zero_day(base, account)

    qqq = regular[R.SIGNAL]
    inputs = preopen_inputs(day, prior, qqq_pre, qqq, external)
    base["risk_mode"] = inputs["risk_mode"]
    high = inputs["high"]
    if events.get("fomc") or events.get("early_close"):
        base["situations"] = ["S0"]
        base["no_trade_reasons"].append("S0: 하루 종일 진입 없음")
        return _zero_day(base, account)

    # v1.0 decisions, called as run_day calls them (each sees only bars before its decision time).
    decision_t = decision_time(events)
    t3 = R.DECISION_INDICATOR_DAY if events.get("indicator") else R.S3_DECISION
    intents: dict[int, dict] = {}
    op = classify_open(inputs, opening_signals(inputs, qqq, prior, decision_t))
    if op["label"]:
        label = op["label"]
        sym = R.LONG if label == "S1" else R.SHORT
        open_low = min(b.low for b in window(regular[sym], R.OPEN, decision_t))
        intents[decision_t] = {"kind": label, "symbol": sym, "stop_raw": open_low, "stop_frac_fixed": None}
    else:
        s3 = classify_s3(inputs, qqq, t3)
        label = s3["label"] or "S4"
        if s3["label"]:
            sess_low = min(b.low for b in window(regular[R.LONG], R.OPEN, t3))
            intents[t3] = {"kind": "S3", "symbol": R.LONG, "stop_raw": sess_low * (1 - R.S3_STOP_BUFFER),
                           "stop_frac_fixed": None}
    base["situations"] = [label]
    s5 = classify_s5(inputs, qqq)
    if s5["label"]:
        base["situations"].append("S5")
        intents[R.S5_DECISION] = {"kind": "S5", "symbol": s5["symbol"], "stop_raw": None,
                                  "stop_frac_fixed": R.S5_STOP_FRAC}
    check_bars = {R.S5_DECISION} | set(intents) | {t + delay for t in intents}

    bt = {s: {b.t: b for b in regular[s]} for s in R.SYMBOLS}
    positions: list[Position] = []
    closed: dict[int, dict] = {}  # id(position) -> closed trade
    pending_entries: list[tuple[int, int, dict]] = []  # (fill_t, signal_t, intent)
    realized = 0.0
    limit = -R.DAILY_LOSS_HALT * equity_start
    locked: dict | None = None
    liquidate_next = False
    min_day_pnl = 0.0

    def bar(sym: str, t: int) -> Bar:
        b = bt[sym].get(t)
        if b is None:
            raise InvalidRun(f"{day}: {sym} {fmt_t(t)} bar missing on a scored day")
        return b

    def open_pos() -> list[Position]:
        return [p for p in positions if p.exit is None]

    def settle(p: Position) -> float:
        tr = close_trade(p.trade, p.exit, costs)
        closed[id(p)] = tr
        return tr["pnl"]

    def attempt(intent: dict, signal_t: int, t: int) -> None:
        kind, sym = intent["kind"], intent["symbol"]
        if account.halted:
            why = "계좌 중단 상태"
        elif locked is not None:
            why = f"하루 잠금({locked['source']} {locked['time']})"
        else:
            trade, why = open_trade(kind, sym, t, bar(sym, t), intent["stop_raw"], intent["stop_frac_fixed"],
                                    equity_start + realized, high, policy, costs, [p.trade for p in open_pos()])
            if trade is not None:
                trade["signal_t"] = signal_t
                positions.append(Position(trade, inputs["prev_close"], delay))
                return
        base["blocked_entries"].append({"kind": kind, "symbol": sym, "signal_t": signal_t, "fill_t": t, "why": why})
        base["no_trade_reasons"].append(f"{kind} 신호, 그러나 {why}")

    for t in range(R.OPEN, R.FORCED_EXIT + 1):
        # 1. account halt from the previous close
        if liquidate_next:
            for fill_t, signal_t, intent in pending_entries:
                base["blocked_entries"].append({"kind": intent["kind"], "symbol": intent["symbol"],
                                                "signal_t": signal_t, "fill_t": fill_t,
                                                "why": "계좌 중단: 대기 진입 주문 취소"})
            pending_entries = []
            for p in open_pos():
                p.liquidate(t, bar(p.trade["symbol"], t))
                realized += settle(p)
            liquidate_next = False
        # 2. open-stage exits (gap stop -> pending / forced)
        for p in open_pos():
            p.open_step(t, bar(p.trade["symbol"], t))
            if p.exit is not None:
                realized += settle(p)  # 3. realized update
        # 4. pre-entry risk check
        if t in check_bars:
            day_pnl = realized + sum(mark_pnl(p.trade, bar(p.trade["symbol"], t - 1).close, costs)
                                     for p in open_pos())
            reached = day_pnl <= limit
            if t == R.S5_DECISION:
                base["check_1445"] = {"triggered": reached, "day_pnl_at_check": day_pnl, "limit": limit,
                                      "time": fmt_t(t), "s5_signal": bool(s5["label"])}
            if reached and locked is None:
                locked = {"source": "pre_entry", "t": t, "time": fmt_t(t), "effective_t": t, "day_pnl": day_pnl}
            min_day_pnl = min(min_day_pnl, day_pnl)
        # 5. entries
        if delay:
            due = [e for e in pending_entries if e[0] == t]
            pending_entries = [e for e in pending_entries if e[0] != t]
            for fill_t, signal_t, intent in due:
                attempt(intent, signal_t, fill_t)
            if t in intents:
                intent = intents[t]
                if account.halted or locked is not None:
                    attempt(intent, t, t)  # records the block at the signal time
                else:
                    pending_entries.append((t + delay, t, intent))
        elif t in intents:
            attempt(intents[t], t, t)
        # 6. intrabar stops
        for p in open_pos():
            p.intrabar_step(t, bar(p.trade["symbol"], t))
            if p.exit is not None:
                realized += settle(p)
        # 7. close check
        day_pnl = realized + sum(mark_pnl(p.trade, bar(p.trade["symbol"], t).close, costs) for p in open_pos())
        min_day_pnl = min(min_day_pnl, day_pnl)
        value = equity_start + day_pnl
        if not finite_positive(value):
            raise InvalidRun(f"{day} {fmt_t(t)}: liquidation-value account {value!r} not a finite positive value")
        account.hwm = max(account.hwm, value)
        account.max_dd = max(account.max_dd, 1 - value / account.hwm)
        if policy.risk_controls:
            if day_pnl <= limit and locked is None:
                locked = {"source": "close", "t": t, "time": fmt_t(t), "effective_t": t + 1, "day_pnl": day_pnl}
            if not account.halted and value <= account.hwm * ACCOUNT_HALT_LEVEL:
                account.halted = True
                liquidate_next = True
                account.halt_event = {"date": day, "t": t, "time": fmt_t(t), "effective_t": t + 1,
                                      "liquidation_value": value, "hwm": account.hwm}
                base["account_halt"] = account.halt_event
        # 8. close-stage signals
        for p in open_pos():
            sym = p.trade["symbol"]
            p.close_step(t, bar(sym, t), bt[R.SIGNAL].get(t), regular[sym])
            if p.exit is not None:
                realized += settle(p)

    if open_pos() or pending_entries:
        raise InvalidRun(f"{day}: position or pending entry left after 15:55")
    trades = [closed[id(p)] for p in positions]  # entry order, as run_day
    total = sum(tr["pnl"] for tr in trades)
    base["trades"] = trades
    if locked is not None:
        locked["overshoot_at_trigger"] = max(0.0, limit - locked["day_pnl"])
        locked["overshoot_worst"] = max(0.0, limit - min(min_day_pnl, total))
        locked["overshoot_final"] = max(0.0, limit - total)
    base["lock"] = locked
    base["min_day_pnl"] = min_day_pnl
    account.equity = equity_start + total
    base["pnl"] = {"day_pnl": total, "equity_end": account.equity, "day_return": total / equity_start}
    return base
