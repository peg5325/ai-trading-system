"""Journal(FILLED 주문) → 실현손익 (Paper/실거래 경로).

Trade Journal 에 이미 기록된 체결을 시간순으로 FIFO 엔진에 넣어 실현 거래를 계산한다.
Backtest 와 동일한 FifoMatcher 를 사용(계산 코드 단일화). read-only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.pnl.fifo import FifoMatcher
from src.pnl.metrics import summarize
from src.pnl.models import FillEvent
from src.risk.models import BUY, SELL

_FILLED = "FILLED"


def _parse(cycle_ts: str | None) -> datetime | None:
    if not cycle_ts:
        return None
    try:
        return datetime.strptime(cycle_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def fills_from_journal(journal, *, symbol: str | None = None,
                       start: datetime | None = None,
                       end: datetime | None = None) -> list[FillEvent]:
    """Journal 의 FILLED 주문을 시간순 FillEvent 로 변환.
    한 사이클 내에서는 outcome 순서(청산 먼저, 진입 나중)를 그대로 사용."""
    fills: list[FillEvent] = []
    for c in journal.get_by_time_range(start, end):        # cycle_ts 오름차순
        ts = _parse(c.cycle_ts)
        for o in c.outcomes:
            sym = o.get("symbol")
            if symbol is not None and sym != symbol:
                continue
            rec = o.get("order_record")
            if not rec or rec.get("status") != _FILLED:
                continue
            if rec.get("side") not in (BUY, SELL):    # 유효 체결만(방어적)
                continue
            q = rec.get("filled_quantity") or 0.0
            if q <= 0:
                continue
            kind = rec.get("kind")
            fills.append(FillEvent(
                symbol=rec.get("symbol") or sym, side=rec.get("side"),
                quantity=float(q), price=float(rec.get("avg_fill_price") or 0.0),
                fee=float(rec.get("fee") or 0.0),
                ref_price=rec.get("ref_price"),          # 없으면 None → 슬리피지 미분리
                timestamp=ts, bar_index=None,
                reason=(kind,) if kind else ()))
    return fills


def realized_from_journal(journal, *, symbol: str | None = None,
                          start: datetime | None = None,
                          end: datetime | None = None, bar_minutes: float = 5.0):
    """Journal → (RealizedTrade 리스트, 미청산 롯 리스트, PnlSummary)."""
    fills = fills_from_journal(journal, symbol=symbol, start=start, end=end)
    m = FifoMatcher(bar_minutes=bar_minutes)
    m.ingest_many(fills)
    open_lots = m.open_lots(symbol)
    return m.trades, open_lots, summarize(m.trades, open_lots)
