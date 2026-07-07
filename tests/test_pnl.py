"""Realized PnL Engine (FIFO) 테스트.

부분청산 / 여러 번 매수 / FIFO 순서 / Long-only / 수수료 / 슬리피지 / 불변식 /
Profit Factor·Expectancy / 결정론 / Journal 연동.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.journal.models import InMemoryJournalStore, JournalCycle
from src.pnl.fifo import FifoMatcher, match_fills
from src.pnl.journal_pnl import realized_from_journal
from src.pnl.metrics import summarize
from src.pnl.models import FillEvent

T0 = datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc)


def _b(sym, qty, price, fee=0.0, ref=None, t=None, bar=None):
    return FillEvent(sym, "BUY", qty, price, fee=fee, ref_price=ref, timestamp=t, bar_index=bar)


def _s(sym, qty, price, fee=0.0, ref=None, t=None, bar=None):
    return FillEvent(sym, "SELL", qty, price, fee=fee, ref_price=ref, timestamp=t, bar_index=bar)


# ---- FIFO 부분청산 ----
def test_fifo_partial_close():
    trades, lots = match_fills([_b("X", 100, 10.0, fee=1.0),
                                _b("X", 100, 12.0, fee=1.2),
                                _s("X", 150, 13.0, fee=1.95)])
    assert len(trades) == 2
    assert abs(trades[0].net_pnl - 297.7) < 1e-9    # (13-10)*100 - (1.0 + 1.95*100/150)
    assert abs(trades[1].net_pnl - 48.75) < 1e-9    # (13-12)*50 - (1.2*50/100 + 1.95*50/150)
    assert trades[0].quantity == 100 and trades[1].quantity == 50
    assert len(lots) == 1 and lots[0].quantity == 50 and lots[0].entry_price == 12.0


def test_multiple_partial_sells_fifo_order():
    # 100 매수 → 50/30/20 순차 매도. FIFO 라 모두 같은 롯(entry 10)에서 나감
    trades, lots = match_fills([_b("Z", 100, 10.0),
                                _s("Z", 50, 11.0), _s("Z", 30, 12.0), _s("Z", 20, 9.0)])
    assert [round(t.net_pnl, 2) for t in trades] == [50.0, 60.0, -20.0]
    assert [t.quantity for t in trades] == [50, 30, 20]
    assert lots == []                                # 전량 청산


def test_multiple_buys_then_sell_spans_lots():
    # 서로 다른 진입가 롯을 하나의 매도가 가로지름
    trades, lots = match_fills([_b("Y", 40, 10.0), _b("Y", 60, 20.0), _s("Y", 70, 25.0)])
    # 40@10 전량 + 60@20 중 30 → 2 trades
    assert len(trades) == 2
    assert abs(trades[0].net_pnl - (25 - 10) * 40) < 1e-9
    assert abs(trades[1].net_pnl - (25 - 20) * 30) < 1e-9
    assert lots[0].quantity == 30 and lots[0].entry_price == 20.0


# ---- 수수료 / 슬리피지 / 불변식 ----
def test_commission_allocation():
    # 진입 수수료 2.0(100주 → 0.02/주), 50 청산 시 진입수수료 1.0 배분
    trades, _ = match_fills([_b("A", 100, 10.0, fee=2.0), _s("A", 50, 11.0, fee=0.55)])
    t = trades[0]
    assert abs(t.commission - (0.02 * 50 + 0.55)) < 1e-9   # 1.0 + 0.55
    assert abs(t.net_pnl - ((11 - 10) * 50 - 1.55)) < 1e-9


def test_slippage_decomposition_and_invariant():
    t = match_fills([_b("S", 10, 100.05, fee=0.02, ref=100.0),
                     _s("S", 10, 109.95, fee=0.022, ref=110.0)])[0][0]
    assert abs(t.gross_pnl - 100.0) < 1e-9          # (110-100)*10
    assert abs(t.slippage - 1.0) < 1e-9             # (0.05 + 0.05)*10
    assert abs(t.commission - 0.042) < 1e-9
    assert abs(t.net_pnl - 98.958) < 1e-9
    # 불변식
    assert abs(t.net_pnl - (t.gross_pnl - t.slippage - t.commission)) < 1e-9
    assert abs(t.net_pnl - ((t.exit_price - t.entry_price) * t.quantity - t.commission)) < 1e-9


def test_ref_none_slippage_zero():
    t = match_fills([_b("N", 5, 10.0), _s("N", 5, 12.0)])[0][0]
    assert t.slippage == 0.0
    assert abs(t.gross_pnl - t.net_pnl) < 1e-9      # 수수료 0 + 슬리피지 0


# ---- Long-only 방어 ----
def test_long_only_oversell_defensive():
    m = FifoMatcher()
    m.ingest(_b("L", 10, 10.0))
    closed = m.ingest(_s("L", 30, 11.0))            # 보유 초과 매도
    assert len(closed) == 1 and closed[0].quantity == 10
    assert m.unmatched_sells == [("L", 20.0)]       # 초과분 방어 기록, 크래시 없음


# ---- holding ----
def test_holding_minutes_and_bars():
    t = match_fills([_b("H", 1, 10.0, t=T0, bar=0),
                     _s("H", 1, 11.0, t=T0 + timedelta(minutes=45), bar=9)])[0][0]
    assert t.holding_minutes == 45.0
    assert t.holding_bars == 9                       # bar_index 차이 우선
    # bar_index 없으면 분/5 로 추정
    t2 = match_fills([_b("H", 1, 10.0, t=T0), _s("H", 1, 11.0, t=T0 + timedelta(minutes=30))])[0][0]
    assert t2.holding_bars == 6                      # 30/5


# ---- summarize ----
def test_summarize_profit_factor_expectancy():
    trades, lots = match_fills([_b("P", 100, 10.0), _s("P", 50, 11.0),
                                _s("P", 30, 12.0), _s("P", 20, 9.0)])
    s = summarize(trades, lots)
    assert s.trades == 3 and s.wins == 2 and s.losses == 1
    assert abs(s.win_rate - 2 / 3) < 1e-9
    assert abs(s.total_net_pnl - 90.0) < 1e-9        # 50+60-20
    assert abs(s.profit_factor - (110.0 / 20.0)) < 1e-9
    assert abs(s.expectancy - 30.0) < 1e-9           # 90/3
    assert s.open_positions == 0


def test_summarize_empty():
    s = summarize([], [])
    assert s.trades == 0 and s.profit_factor is None and s.win_rate == 0.0


# ---- 결정론 ----
def test_determinism():
    fills = [_b("D", 100, 10.0, fee=1.0), _b("D", 50, 11.0), _s("D", 120, 12.0, fee=1.0)]
    a = [t.to_dict() for t in match_fills(fills)[0]]
    b = [t.to_dict() for t in match_fills(fills)[0]]
    assert a == b


# ---- Journal 연동 ----
def _cycle(cid, cts, outcomes):
    return JournalCycle(cid, cts, "REGULAR", [o["symbol"] for o in outcomes],
                        "RAN", "OK", None, outcomes=outcomes, order_count=len(outcomes),
                        portfolio={}, market={"market": "US"})


def _order_outcome(sym, side, qty, price, fee, kind, ref=None):
    return {"symbol": sym, "action": "ORDERED", "reason": kind,
            "order_record": {"symbol": sym, "side": side, "kind": kind, "status": "FILLED",
                             "filled_quantity": qty, "avg_fill_price": price, "fee": fee,
                             "ref_price": ref}}


def test_realized_from_journal():
    j = InMemoryJournalStore()
    j.save_cycle(_cycle("c1", "2026-07-07T04:00:00Z",
                        [_order_outcome("SOXL", "BUY", 10, 100.0, 0.2, "NEW_ENTRY", ref=100.0)]))
    j.save_cycle(_cycle("c2", "2026-07-07T04:30:00Z",
                        [_order_outcome("SOXL", "SELL", 10, 110.0, 0.22, "EXIT", ref=110.0)]))
    trades, open_lots, summary = realized_from_journal(j)
    assert len(trades) == 1 and open_lots == []
    t = trades[0]
    assert abs(t.net_pnl - ((110 - 100) * 10 - 0.42)) < 1e-9
    assert t.holding_minutes == 30.0 and t.holding_bars == 6   # 30min / 5
    assert summary.trades == 1 and summary.wins == 1


def test_realized_from_journal_open_position():
    # 진입만 있고 청산 없음 → 거래 0, 미청산 롯 1
    j = InMemoryJournalStore()
    j.save_cycle(_cycle("c1", "2026-07-07T04:00:00Z",
                        [_order_outcome("QQQ", "BUY", 5, 500.0, 0.5, "NEW_ENTRY")]))
    trades, open_lots, summary = realized_from_journal(j)
    assert trades == [] and len(open_lots) == 1 and open_lots[0].quantity == 5
    assert summary.trades == 0 and summary.open_positions == 1
