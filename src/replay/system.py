"""ReplaySystem — cycle_id 하나로 당시 상태를 완전 복원(read-only).

- Journal(read-only)에서 cycle 을 읽고, 선택적으로 CandleStore(read-only)에서 캔들을 복원.
- 원본 상태를 절대 변경하지 않는다. 주문 기능 없음.
- TradingPipeline 과 완전히 분리(파이프라인 미import).
- 결과는 하나의 ReplayContext (JSON 직렬화 가능, GPT 입력 가능).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.engines.indicator_engine import candles_to_frame, resample
from src.replay.models import CycleNotFoundError, ReplayContext


def _parse_ts(cycle_ts: str | None) -> datetime | None:
    if not cycle_ts:
        return None
    try:
        return datetime.strptime(cycle_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def candle_to_dict(c) -> dict:
    return {
        "symbol": c.symbol, "interval": c.interval,
        "ts_utc": c.ts_utc.isoformat(), "ts_exchange": c.ts_exchange.isoformat(),
        "session": c.session, "open": c.open, "high": c.high, "low": c.low,
        "close": c.close, "volume": c.volume, "currency": c.currency, "adjusted": c.adjusted,
    }


class ReplaySystem:
    def __init__(self, journal, candle_store=None,
                 source_interval: str = "1m", resample_rule: str = "5min"):
        self._journal = journal
        self._store = candle_store
        self._source_interval = source_interval
        self._resample_rule = resample_rule

    def replay(self, cycle_id: str) -> ReplayContext:
        cycle = self._journal.get_cycle(cycle_id)        # read-only
        if cycle is None:
            raise CycleNotFoundError(cycle_id)

        as_of = _parse_ts(cycle.cycle_ts)
        positions = (cycle.portfolio or {}).get("positions", {}) if cycle.portfolio else {}

        symbols = []
        for o in cycle.outcomes:
            sym = o.get("symbol")
            candles = self._candles(sym, as_of)
            snap = o.get("indicator_snapshot") or {}
            last_price = candles[-1]["close"] if candles else snap.get("close")
            symbols.append({
                "symbol": sym,
                "action": o.get("action"),
                "reason": o.get("reason"),
                "candles": candles,
                "indicator_snapshot": o.get("indicator_snapshot"),
                "candidate": o.get("candidate"),
                "ai_decision": o.get("ai_decision"),
                "ai_log": o.get("ai_log"),
                "risk_decision": o.get("risk_decision"),
                "order_record": o.get("order_record"),
                "position": positions.get(sym),
                "last_price": last_price,
            })

        market_snapshot = {
            "cycle_ts": cycle.cycle_ts,
            "session": cycle.session,
            "is_eod_flatten_window": (cycle.market or {}).get("is_eod_flatten_window"),
            "last_prices": {s["symbol"]: s["last_price"] for s in symbols},
        }

        return ReplayContext(
            cycle_id=cycle.cycle_id, cycle_ts=cycle.cycle_ts, session=cycle.session,
            status=cycle.status, universe=list(cycle.universe),
            market_snapshot=market_snapshot, portfolio=cycle.portfolio,
            symbols=symbols, journal=cycle.to_dict())

    def _candles(self, symbol: str, as_of: datetime | None) -> list[dict]:
        """CandleStore(read-only)에서 당시 확정 캔들 복원. look-ahead 방지: as_of 이하만."""
        if self._store is None or not symbol:
            return []
        raw = self._store.query(symbol, self._source_interval)   # read-only
        if not raw:
            return []
        df = resample(candles_to_frame(raw), self._resample_rule)
        from src.live.paper_loop import frame_to_candles
        candles = frame_to_candles(df, symbol)
        if as_of is not None:
            candles = [c for c in candles if c.ts_utc <= as_of]
        return [candle_to_dict(c) for c in candles]
