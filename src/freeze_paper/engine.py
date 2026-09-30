"""Deterministic 1-minute step engine for the FREEZE Paper contract.

State is a plain JSON-serializable dict so a run can be checkpointed after any
bar and resumed with identical results.  Each ``step_bar`` rebuilds the existing
components from that state and applies the contract's per-bar order
(``exits.priority_within_bar``):

1. bar open: pending risk-halt/carried liquidations (EXIT) -> TIME_EXIT /
   EOD_FLATTEN -> new entries (only at the 09:35 ET bar)
2. intrabar: STOP_LOSS / TAKE_PROFIT (stop first, gap rules), incl. the entry bar
3. bar close: mark positions at the close, evaluate daily-loss / drawdown on the
   recorded ledger; a breach schedules liquidation at the next bar open

Orders go RiskManager -> OrderManager -> PaperBrokerProvider; fills feed the single
FIFO PnL engine through PortfolioAccount.  Exits use RISK_REDUCING kinds and
never consult AI.  No clock, randomness, network, or file I/O.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from src.backtest.execution import ExecutionModel
from src.exits.generator import ExitContext, ExitSignalGenerator, PositionView
from src.freeze_paper.binding import ContractBinding
from src.freeze_paper.costs import CostModel
from src.freeze_paper.gates import TRADE, GateResult
from src.freeze_paper.sizing import NoTradeSymbol, size_entries
from src.models import ET
from src.order.manager import OrderManager
from src.order.models import (
    ORD_FILLED,
    InMemoryOrderStore,
    InMemoryPositionStore,
    OrderRecord,
)
from src.order.paper_broker import PaperBrokerProvider
from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.premarket.contract import canonical_bytes
from src.premarket.schedule import build_premarket_schedule, iso_z, parse_iso
from src.risk.models import (
    BUY,
    EXIT,
    NEW_ENTRY,
    SELL,
    OrderIntent,
    PortfolioSnapshot,
    Position,
    RiskDecision,
)
from src.risk.risk_manager import RiskManager

STATE_SCHEMA = "freeze-paper-engine-state/v2"      # v2: session.equity_curve
CLIENT_ORDER_PREFIX = "pfl1"
_QTY_EPS = 1e-9

# session statuses
TRADING = "TRADING"
NO_TRADE = "NO_TRADE"
HALTED = "HALTED"
COMPLETE = "COMPLETE"
UNRESOLVED_EXIT = "UNRESOLVED_EXIT"


@dataclass(frozen=True)
class Bar:
    """Minimal completed 1m bar (Candle-compatible attribute names)."""
    open: float
    high: float
    low: float
    close: float


INVALID_BAR = "INVALID_BAR"
BAR_KEY_COLLISION = "BAR_KEY_COLLISION"


def client_order_id(entry_day: str, symbol: str, side: str, attempt: int | None = None) -> str:
    """BUY is single-attempt; SELL ids carry the attempt number of that position's exit."""
    base = f"{CLIENT_ORDER_PREFIX}-{entry_day.replace('-', '')}-{symbol}-{side}"
    return base if attempt is None else f"{base}-{attempt}"


def valid_bar(bar: Any) -> bool:
    """Finite, positive OHLC with low <= min(open, close) and max(open, close) <= high."""
    try:
        o, h, low, c = (float(bar.open), float(bar.high), float(bar.low), float(bar.close))
    except (AttributeError, TypeError, ValueError):
        return False
    if not all(math.isfinite(v) and v > 0 for v in (o, h, low, c)):
        return False
    return low <= min(o, c) and max(o, c) <= h


def new_account_state(binding: ContractBinding) -> dict:
    """Flat Paper account at the contract's initial equity (Paper start)."""
    initial = binding.initial_equity
    return {
        "schema": STATE_SCHEMA,
        "contract_sha256": binding.sha256,
        "initial_equity": initial,
        "cash": initial,
        "fills": [],
        "orders": [],
        "order_positions": {},
        "positions": {},
        "marks": {},
        "peak_equity": initial,
        "prior_close_equity": initial,
        "halted": None,
        "session": None,
        "sessions": [],
    }


def start_session(state: dict, gate: GateResult, binding: ContractBinding) -> dict:
    """Open one scheduled session. Schedule comes from the FREEZE snapshot; when the
    brief is unusable (NO_TRADE without schedule) the tracked NYSE calendar is used so
    a carried position can still be liquidated."""
    _check_binding(state, binding)
    s = copy.deepcopy(state)
    if s["session"] is not None and not s["session"]["finished"]:
        raise ValueError("previous session is not finished")
    if gate.contract_sha256 != binding.sha256:
        raise ValueError("gate was evaluated under a different contract")
    day = gate.trading_day
    if s["sessions"] and date.fromisoformat(day) <= date.fromisoformat(
            s["sessions"][-1]["trading_day"]):
        raise ValueError("trading day is not after the last recorded session; "
                         "restart only by resuming a checkpoint")
    schedule = gate.schedule or build_premarket_schedule(date.fromisoformat(day)).to_dict()
    close = parse_iso(schedule["market_close_utc"])
    entry = datetime.combine(date.fromisoformat(day), binding.entry_time_et, tzinfo=ET)
    halted_at_start = s["halted"] is not None
    tradable = gate.decision == TRADE and not halted_at_start
    s["session"] = {
        "trading_day": day,
        "market_open_utc": schedule["market_open_utc"],
        "market_close_utc": schedule["market_close_utc"],
        "eod_utc": iso_z(close - timedelta(minutes=binding.eod_minutes_before_close)),
        "entry_utc": iso_z(entry),
        "status": HALTED if halted_at_start else (TRADING if tradable else NO_TRADE),
        "gate": gate.to_dict(),
        "candidates": [c.to_dict() for c in gate.candidates] if tradable else [],
        "entries_done": not tradable,
        "sizing_equity": None,
        "day_start_equity": s["prior_close_equity"],
        "liquidate_all": bool(halted_at_start and s["positions"]),
        "last_minute_utc": None,
        "risk": {},
        "equity_curve": [],       # [minute_utc, recorded marked equity] at every mark
        "events": [],
        "finished": False,
        "end_equity": None,
    }
    rt = _Runtime(s, binding)
    if halted_at_start and gate.decision == TRADE:
        for cand in gate.candidates:        # HALTED sessions stay auditable per candidate
            rt.event(s["session"]["market_open_utc"], "NO_TRADE_SYMBOL",
                     symbol=cand.traded_symbol, rank=cand.rank, reason=HALTED)
    rt.mark(day, s["session"]["market_open_utc"])   # pre-09:35 mark anchors the daily loss
    return rt.dump()


def step_bar(state: dict, minute_utc: datetime | str, bars: Mapping[str, Any],
             binding: ContractBinding) -> dict:
    """Apply one completed REGULAR 1m bar minute. Pure: returns a new state."""
    _check_binding(state, binding)
    sess = state.get("session")
    if sess is None or sess["finished"]:
        raise ValueError("no open session")
    minute = parse_iso(minute_utc) if isinstance(minute_utc, str) else minute_utc
    minute_iso = iso_z(minute)
    open_utc, close_utc = parse_iso(sess["market_open_utc"]), parse_iso(sess["market_close_utc"])
    if not open_utc <= minute < close_utc:
        return copy.deepcopy(state)          # outside the scheduled regular session
    if sess["last_minute_utc"] is not None and minute <= parse_iso(sess["last_minute_utc"]):
        return copy.deepcopy(state)          # already processed (replay-safe)
    rt = _Runtime(copy.deepcopy(state), binding)
    valid: dict[str, Any] = {}
    keys_by_symbol: dict[str, list[str]] = {}
    for key in sorted(bars):
        keys_by_symbol.setdefault(str(key).upper(), []).append(key)
    for sym, keys in sorted(keys_by_symbol.items()):
        if len(keys) > 1:                    # ambiguous input: every colliding bar is invalid
            for _ in keys:
                rt.event(minute_iso, INVALID_BAR, symbol=sym, reason=BAR_KEY_COLLISION)
        elif valid_bar(bars[keys[0]]):
            valid[sym] = bars[keys[0]]
        else:                                # treated exactly as a missing bar
            rt.event(minute_iso, INVALID_BAR, symbol=sym, reason=INVALID_BAR)
    rt.step(minute, minute_iso, valid)
    return rt.dump()


def finish_session(state: dict, binding: ContractBinding) -> dict:
    """Close the session after the last regular bar."""
    _check_binding(state, binding)
    s = copy.deepcopy(state)
    sess = s["session"]
    if sess is None or sess["finished"]:
        raise ValueError("no open session")
    rt = _Runtime(s, binding)
    if not sess["entries_done"]:
        rt.skip_all_candidates(sess["market_close_utc"], "MISSING_ENTRY_BAR")
    if s["positions"]:
        sess["status"] = UNRESOLVED_EXIT
        if s["halted"] is None:
            s["halted"] = {"reason": UNRESOLVED_EXIT, "trading_day": sess["trading_day"],
                           "minute_utc": sess["market_close_utc"]}
        for sym in sorted(s["positions"]):
            sess["events"].append({"minute_utc": sess["market_close_utc"],
                                   "type": UNRESOLVED_EXIT, "symbol": sym})
    elif sess["status"] == TRADING:
        sess["status"] = COMPLETE
    end_equity = rt.account.equity(s["marks"])
    sess["end_equity"] = end_equity
    sess["finished"] = True
    s["prior_close_equity"] = end_equity
    s["sessions"].append({"trading_day": sess["trading_day"], "status": sess["status"],
                          "end_equity": end_equity,
                          "halted": copy.deepcopy(s["halted"])})
    return s


def run_session(state: dict, gate: GateResult,
                bars_by_minute: Mapping[Any, Mapping[str, Any]],
                binding: ContractBinding) -> dict:
    """start -> every minute in time order -> finish."""
    s = start_session(state, gate, binding)
    for minute in sorted(bars_by_minute, key=_minute_key):
        s = step_bar(s, minute, bars_by_minute[minute], binding)
    return finish_session(s, binding)


def state_bytes(state: dict) -> bytes:
    return canonical_bytes(state)


def _minute_key(value: Any) -> datetime:
    return parse_iso(value) if isinstance(value, str) else value


def _check_binding(state: Mapping, binding: ContractBinding) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise ValueError("engine state schema mismatch")
    if state.get("contract_sha256") != binding.sha256:
        raise ValueError("engine state was created under a different contract")


class _Runtime:
    """Existing components rebuilt from a serialized state for one step."""

    def __init__(self, state: dict, binding: ContractBinding):
        self.s = state
        self.b = binding
        self.cost = CostModel.from_binding(binding)
        self.exits = ExitSignalGenerator(binding.exit_config)
        self.risk = RiskManager(binding.risk_config)
        self.account = PortfolioAccount.from_fills(
            state["initial_equity"], [fill_event(f) for f in state["fills"]])
        self.account.restore_peak(state["peak_equity"])
        self.orders = InMemoryOrderStore()
        for rec in state["orders"]:
            self.orders.save(OrderRecord(**rec))
        self.order_positions = InMemoryPositionStore()
        for sym, pos in state["order_positions"].items():
            self.order_positions.apply_fill(sym, BUY, pos["quantity"], pos["avg_price"], 0.0)
        broker = PaperBrokerProvider(
            ExecutionModel(slippage_bps=binding.slippage_bps, fee_bps=binding.commission_bps),
            fee_model=self.cost.fee)
        self.om = OrderManager(broker, self.orders, self.order_positions)

    @property
    def sess(self) -> dict:
        return self.s["session"]

    def dump(self) -> dict:
        self.s["cash"] = self.account.cash
        self.s["orders"] = [rec.to_dict() for rec in self.orders.all()]
        self.s["order_positions"] = self.order_positions.all()
        return self.s

    def event(self, minute_iso: str, event_type: str, **fields: Any) -> None:
        self.sess["events"].append({"minute_utc": minute_iso, "type": event_type, **fields})

    # ---- per-bar order ----
    def step(self, minute: datetime, minute_iso: str, bars: Mapping[str, Any]) -> None:
        sess = self.sess
        eod = minute >= parse_iso(sess["eod_utc"])
        self.exit_attempted: set[str] = set()     # at most one SELL attempt per symbol per bar
        # 1) bar open: pending liquidations -> TIME_EXIT / EOD_FLATTEN -> entries
        if sess["liquidate_all"]:
            for sym in sorted(self.s["positions"]):
                if sym in bars:
                    self.exit(sym, EXIT, float(bars[sym].open), minute, minute_iso)
        for sym in sorted(self.s["positions"]):
            pending = self.s["positions"][sym].get("pending_exit")
            if pending is not None and sym in bars:   # unconditional retry of a failed exit
                self.exit(sym, pending, float(bars[sym].open), minute, minute_iso)
        ctx = ExitContext(is_eod_window=eod, now_time=minute)
        for sym in sorted(self.s["positions"]):
            if sym not in bars:
                continue
            decision = self.exits.evaluate_bar_open(
                self._view(sym), ctx, bar_open=float(bars[sym].open))
            if decision is not None:
                self.exit(sym, decision.reason, decision.reference_price, minute, minute_iso)
        if not sess["entries_done"]:
            entry = parse_iso(sess["entry_utc"])
            if minute == entry:
                self.enter(minute, minute_iso, bars, eod)
            elif minute > entry:
                self.skip_all_candidates(minute_iso, "MISSING_ENTRY_BAR")
        # 2) intrabar stop/target (entry bar included)
        for sym in sorted(self.s["positions"]):
            if sym not in bars:
                continue
            decision = self.exits.evaluate_intrabar(self._view(sym), bars[sym])
            if decision is not None:
                self.exit(sym, decision.reason, decision.reference_price, minute, minute_iso)
        # 3) bar close: mark and risk check on the recorded ledger
        for sym in self.s["positions"]:
            if sym in bars:
                self.s["marks"][sym] = float(bars[sym].close)
        self.mark(sess["trading_day"], minute_iso)
        self.check_risk(minute_iso)
        sess["last_minute_utc"] = minute_iso

    def mark(self, day: str, minute_iso: str) -> None:
        marks = {sym: self.s["marks"][sym] for sym in self.s["positions"] if sym in self.s["marks"]}
        equity = self.account.mark_equity(marks, day_key=day,
                                          day_start_equity=self.sess["day_start_equity"])
        self.s["peak_equity"] = self.account.peak_equity
        self.sess["risk"] = {"equity": equity,
                             "daily_return": self.account.last_daily_return_pct,
                             "drawdown": self.account.last_drawdown_pct}
        self.sess["equity_curve"].append([minute_iso, equity])

    def check_risk(self, minute_iso: str) -> None:
        risk = self.sess["risk"]
        cfg = self.b.risk_config
        reason = None
        if risk["daily_return"] <= -cfg.daily_loss_limit_pct:
            reason = "DAILY_LOSS_LIMIT"
        elif -risk["drawdown"] >= cfg.max_drawdown_pct:
            reason = "MAX_DRAWDOWN"
        if reason is None:
            return
        if self.s["halted"] is None:
            self.s["halted"] = {"reason": reason, "trading_day": self.sess["trading_day"],
                                "minute_utc": minute_iso}
            self.event(minute_iso, "RISK_HALT", reason=reason, equity=risk["equity"])
        self.sess["status"] = HALTED
        if not self.sess["entries_done"]:
            self.skip_all_candidates(minute_iso, "HALTED")
        if self.s["positions"]:
            self.sess["liquidate_all"] = True

    # ---- entries ----
    def skip_all_candidates(self, minute_iso: str, reason: str) -> None:
        for cand in self.sess["candidates"]:
            self.event(minute_iso, "NO_TRADE_SYMBOL", symbol=cand["traded_symbol"],
                       rank=cand["rank"], reason=reason)
        self.sess["entries_done"] = True

    def enter(self, minute: datetime, minute_iso: str, bars: Mapping[str, Any],
              eod: bool) -> None:
        sess = self.sess
        sess["entries_done"] = True
        by_symbol = {c["traded_symbol"]: c for c in sess["candidates"]}
        eligible: list[tuple[str, int, float]] = []
        skipped: list[NoTradeSymbol] = []
        for cand in sess["candidates"]:
            sym = cand["traded_symbol"]
            if sym in bars:
                eligible.append((sym, cand["rank"], float(bars[sym].open)))
            else:
                skipped.append(NoTradeSymbol(sym, cand["rank"], "MISSING_ENTRY_BAR"))
        equity = self.account.equity(self.s["marks"])
        sess["sizing_equity"] = equity
        sized, room_skipped = size_entries(equity, eligible, self.b)
        for item in sorted(skipped + room_skipped, key=lambda x: x.rank):
            self.event(minute_iso, "NO_TRADE_SYMBOL", **item.to_dict())
        risk_positions: dict[str, Position] = {}
        lev = self.b.risk_leverage_factor
        for entry in sized:
            intent = OrderIntent(entry.symbol, NEW_ENTRY, BUY, float(entry.quantity),
                                 entry.reference_open, leverage_factor=lev)
            snapshot = PortfolioSnapshot(
                account_value=equity, positions=dict(risk_positions),
                daily_pnl_pct=sess["risk"]["daily_return"],
                drawdown_pct=sess["risk"]["drawdown"],
                session="REGULAR", is_eod_flatten_window=eod, cash=self.account.cash)
            decision = self.risk.evaluate(intent, snapshot)
            if decision.allowed and decision.adjusted_quantity < entry.quantity - _QTY_EPS:
                decision = RiskDecision(False, ["REJECT_ENTRY_QUANTITY_REDUCED"]
                                        + decision.reason_codes, 0.0, decision.risk_flags)
            cand = by_symbol[entry.symbol]
            record = self.submit(intent, decision,
                                 client_order_id(sess["trading_day"], entry.symbol, BUY),
                                 minute, minute_iso, cand["leg"])
            if record is None or record.status != ORD_FILLED:
                self.event(minute_iso, "REJECT_ENTRY", symbol=entry.symbol, rank=entry.rank,
                           reason_codes=list(decision.reason_codes))
                continue
            self.s["positions"][entry.symbol] = {
                "quantity": record.filled_quantity,
                "entry_fill_price": record.avg_fill_price,
                "reference_open": entry.reference_open,
                "entry_bar_utc": minute_iso,
                "entry_day": sess["trading_day"],
                "leg": cand["leg"],
                "rank": entry.rank,
                "signal_symbol": cand["signal_symbol"],
                "sell_attempt": 1,
            }
            self.s["marks"][entry.symbol] = entry.reference_open
            risk_positions[entry.symbol] = Position(
                entry.symbol, record.filled_quantity, record.avg_fill_price,
                entry.reference_open, leverage_factor=lev)

    # ---- exits ----
    def _view(self, sym: str) -> PositionView:
        pos = self.s["positions"][sym]
        return PositionView(
            symbol=sym, quantity=pos["quantity"], entry_price=pos["entry_fill_price"],
            current_price=self.s["marks"].get(sym, pos["entry_fill_price"]),
            leverage_factor=self.b.risk_leverage_factor,
            entry_time=parse_iso(pos["entry_bar_utc"]))

    def exit(self, sym: str, kind: str, reference_price: float, minute: datetime,
             minute_iso: str) -> None:
        if sym in self.exit_attempted:
            return                            # a rejected/partial exit retries at the next bar
        self.exit_attempted.add(sym)
        pos = self.s["positions"][sym]
        lev = self.b.risk_leverage_factor
        intent = OrderIntent(sym, kind, SELL, pos["quantity"], reference_price,
                             leverage_factor=lev, risk_flags=[kind])
        snapshot = PortfolioSnapshot(
            account_value=self.account.equity(self.s["marks"]),
            positions={sym: Position(sym, pos["quantity"], pos["entry_fill_price"],
                                     self.s["marks"].get(sym, reference_price),
                                     leverage_factor=lev)},
            kill_switch=self.s["halted"] is not None, session="REGULAR")
        decision = self.risk.evaluate(intent, snapshot)
        coid = client_order_id(pos["entry_day"], sym, SELL, pos["sell_attempt"])
        stored = self.orders.get(coid)
        if stored is not None and stored.status == ORD_FILLED:
            # A filled order the position book does not reflect: never resubmit or refill;
            # the position stays open and ends as UNRESOLVED_EXIT (fail closed).
            if not any(e["type"] == "LEDGER_INCONSISTENT" and e.get("client_order_id") == coid
                       for e in self.sess["events"]):   # once per position per session
                self.event(minute_iso, "LEDGER_INCONSISTENT", symbol=sym, kind=kind,
                           client_order_id=coid)
            return
        if stored is not None:
            pos["sell_attempt"] += 1          # stale rejected attempt: move to the next id
            coid = client_order_id(pos["entry_day"], sym, SELL, pos["sell_attempt"])
        record = self.submit(intent, decision, coid, minute, minute_iso, pos["leg"])
        if record is None or record.status != ORD_FILLED or record.filled_quantity <= 0:
            pos["sell_attempt"] += 1          # retry at the next available bar with a new id
            pos["pending_exit"] = kind
            self.event(minute_iso, "EXIT_NOT_FILLED", symbol=sym, kind=kind,
                       client_order_id=coid)
            return
        held = pos["quantity"]
        if abs(record.filled_quantity - held) > _QTY_EPS:
            self.event(minute_iso, "EXIT_FILL_QUANTITY_MISMATCH", symbol=sym, kind=kind,
                       client_order_id=coid, held=held, filled=record.filled_quantity)
        remainder = held - record.filled_quantity
        if remainder > _QTY_EPS:
            pos["quantity"] = remainder
            pos["sell_attempt"] += 1
            pos["pending_exit"] = kind
            return
        del self.s["positions"][sym]
        self.s["marks"].pop(sym, None)

    # ---- order path ----
    def submit(self, intent: OrderIntent, decision: RiskDecision, coid: str,
               minute: datetime, minute_iso: str, leg: str) -> OrderRecord | None:
        """OrderManager.submit with the deterministic id; returns None for a duplicate id
        (the stored order is kept and no fill is applied again)."""
        if self.orders.get(coid) is not None:
            self.event(minute_iso, "DUPLICATE_CLIENT_ORDER_ID", client_order_id=coid)
            return None
        record = self.om.submit(intent, decision, client_order_id=coid,
                                ref_price=intent.price)
        if record.status == ORD_FILLED and record.filled_quantity > 0:
            parts = self.cost.fee_parts(record.side,
                                        record.filled_quantity * record.avg_fill_price)
            fill = {
                "client_order_id": coid, "symbol": record.symbol, "side": record.side,
                "quantity": record.filled_quantity, "price": record.avg_fill_price,
                "fee": record.fee, "commission": parts["commission"],
                "sec_fee": parts["sec_fee"], "ref_price": intent.price,
                "timestamp": minute_iso, "kind": intent.kind, "leg": leg,
                "trading_day": self.sess["trading_day"],
            }
            self.s["fills"].append(fill)
            self.account.apply_fill(fill_event(fill))
            self.event(minute_iso, "FILL", client_order_id=coid, symbol=record.symbol,
                       side=record.side, kind=intent.kind, quantity=record.filled_quantity,
                       price=record.avg_fill_price)
        return record


def fill_event(fill: Mapping) -> FillEvent:
    return FillEvent(
        symbol=fill["symbol"], side=fill["side"], quantity=fill["quantity"],
        price=fill["price"], fee=fill["fee"], ref_price=fill["ref_price"],
        timestamp=parse_iso(fill["timestamp"]), reason=(fill["kind"],))
