"""Realized PnL Engine — FIFO 기반 실현손익 계산(단일 엔진).

실거래 / Paper / Backtest 가 모두 이 엔진을 공유한다. 결정론적, 부분청산 지원.
"""
from src.pnl.fifo import FifoMatcher, match_fills
from src.pnl.journal_pnl import fills_from_journal, realized_from_journal
from src.pnl.metrics import summarize
from src.pnl.models import FillEvent, OpenLot, PnlSummary, RealizedTrade

__all__ = [
    "FifoMatcher", "match_fills", "summarize",
    "FillEvent", "RealizedTrade", "OpenLot", "PnlSummary",
    "fills_from_journal", "realized_from_journal",
]
