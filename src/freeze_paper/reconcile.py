"""Daily reconciliation of the Paper engine state (read-only).

Compares three position books with the existing ReconciliationService — the
OrderManager position store, the PortfolioAccount (FIFO) rebuilt from fills, and
a plain replay of the fills — plus the engine's own position records.  Also
checks order<->fill pairing, the cash identity, end-of-session flatness,
client_order_id uniqueness and, when given, the restart rebuild.
Any mismatch sets ``ok=False``; nothing is repaired here.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from src.freeze_paper.engine import UNRESOLVED_EXIT, fill_event, state_bytes
from src.order.models import ORD_FILLED
from src.portfolio.account import PortfolioAccount
from src.reconcile.models import PositionView
from src.reconcile.service import ReconcileConfig, ReconciliationService
from src.risk.models import BUY

CASH_TOLERANCE = 1e-6
FILL_TOLERANCE = 1e-9


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    mismatches: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "mismatches": [dict(m) for m in self.mismatches]}


class _Book:
    """Adapter exposing a {symbol: {quantity, avg_price}} book to ReconciliationService."""

    def __init__(self, book: Mapping[str, Mapping]):
        self._views = [PositionView(sym, float(p["quantity"]), float(p["avg_price"]))
                       for sym, p in sorted(book.items())]

    def get_positions(self) -> list[PositionView]:
        return list(self._views)

    def get_open_orders(self) -> list:
        return []


def reconcile_state(state: Mapping, *, rebuilt_state: Mapping | None = None) -> ReconcileResult:
    mismatches: list[dict] = []
    fills = list(state["fills"])
    orders = list(state["orders"])

    account = PortfolioAccount.from_fills(state["initial_equity"],
                                          [fill_event(f) for f in fills])
    books = {
        "order_manager": dict(state["order_positions"]),
        "portfolio_account": account.positions(),
        "fill_replay": _replay(fills),
        "engine": {sym: {"quantity": p["quantity"], "avg_price": p["entry_fill_price"]}
                   for sym, p in state["positions"].items()},
    }
    for left, right in (("order_manager", "portfolio_account"),
                        ("fill_replay", "portfolio_account"),
                        ("engine", "portfolio_account")):
        report = ReconciliationService(
            _Book(books[right]), _Book(books[left]),
            config=ReconcileConfig(compare_market_value=False, compare_orders=False),
        ).reconcile()
        for m in report.mismatches:
            mismatches.append({"check": f"POSITIONS_{left.upper()}_VS_{right.upper()}",
                               **m.to_dict()})

    mismatches += _order_fill_pairing(orders, fills)
    for f in fills:
        if abs(float(f["fee"]) - (float(f["commission"]) + float(f["sec_fee"]))) > FILL_TOLERANCE:
            mismatches.append({"check": "FILL_FEE_PARTS", "client_order_id": f["client_order_id"],
                               "fee": f["fee"], "commission": f["commission"],
                               "sec_fee": f["sec_fee"]})

    identity = state["initial_equity"] + sum(
        (f["quantity"] * f["price"]) * (1 if f["side"] != BUY else -1) - f["fee"] for f in fills)
    for label, value in (("state_cash", state["cash"]), ("account_cash", account.cash)):
        if abs(identity - value) > CASH_TOLERANCE:
            mismatches.append({"check": "CASH_IDENTITY", "source": label,
                               "expected": identity, "actual": value})

    session = state.get("session")
    if (session is not None and session.get("finished") and state["positions"]
            and session.get("status") != UNRESOLVED_EXIT):
        mismatches.append({"check": "NOT_FLAT_AT_SESSION_END",
                           "symbols": sorted(state["positions"])})

    for label, ids in (("orders", [o["client_order_id"] for o in orders]),
                       ("fills", [f["client_order_id"] for f in fills])):
        dupes = sorted(k for k, n in Counter(ids).items() if n > 1)
        if dupes:
            mismatches.append({"check": "DUPLICATE_CLIENT_ORDER_ID", "source": label,
                               "client_order_ids": dupes})

    if rebuilt_state is not None and state_bytes(dict(rebuilt_state)) != state_bytes(dict(state)):
        mismatches.append({"check": "RESTART_REBUILD_MISMATCH"})

    return ReconcileResult(not mismatches, tuple(mismatches))


def _replay(fills: list[Mapping]) -> dict[str, dict]:
    book: dict[str, dict] = {}
    for f in fills:
        cur = book.get(f["symbol"], {"quantity": 0.0, "avg_price": 0.0})
        qty = cur["quantity"] + (f["quantity"] if f["side"] == BUY else -f["quantity"])
        if f["side"] == BUY:
            cur["avg_price"] = ((cur["quantity"] * cur["avg_price"] + f["quantity"] * f["price"])
                                / qty)
        cur["quantity"] = qty
        if abs(qty) <= FILL_TOLERANCE:
            book.pop(f["symbol"], None)
        else:
            book[f["symbol"]] = cur
    return book


def _order_fill_pairing(orders: list[Mapping], fills: list[Mapping]) -> list[dict]:
    out: list[dict] = []
    fills_by_id: dict[str, list[Mapping]] = {}
    for f in fills:
        fills_by_id.setdefault(f["client_order_id"], []).append(f)
    order_ids = {o["client_order_id"] for o in orders}
    for order in orders:
        coid = order["client_order_id"]
        matched = fills_by_id.get(coid, [])
        if order["status"] != ORD_FILLED:
            if matched:
                out.append({"check": "FILL_FOR_UNFILLED_ORDER", "client_order_id": coid})
            continue
        if len(matched) != 1:
            out.append({"check": "ORDER_FILL_COUNT", "client_order_id": coid,
                        "fills": len(matched)})
            continue
        fill = matched[0]
        for order_key, fill_key in (("filled_quantity", "quantity"),
                                    ("avg_fill_price", "price"), ("fee", "fee")):
            if abs(float(order[order_key]) - float(fill[fill_key])) > FILL_TOLERANCE:
                out.append({"check": "ORDER_FILL_FIELD", "client_order_id": coid,
                            "field": fill_key, "order": order[order_key],
                            "fill": fill[fill_key]})
        if order["symbol"] != fill["symbol"] or order["side"] != fill["side"]:
            out.append({"check": "ORDER_FILL_IDENTITY", "client_order_id": coid})
    for coid in sorted(set(fills_by_id) - order_ids):
        out.append({"check": "FILL_WITHOUT_ORDER", "client_order_id": coid})
    return out
