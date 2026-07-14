"""history loader — 통합 저장소 market_history.sqlite 에서 canonical Candle 을 읽는다(PR 3-B).

- **read-only(`mode=ro`)**: 원본 DB 불변. Toss API 호출 없음. Runtime/Paper 무관.
- BacktestEngine 이 `list[Candle]` 을 그대로 소비 → 통합 저장소 ↔ 백테스트 배선.
- 결정론: 저장된 캔들과 인자만 사용(현재 시각/난수 미사용). 시간(ts_utc) 오름차순 반환.
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md §6.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.models import Candle, exchange_tz


def _row_to_candle(r: sqlite3.Row) -> Candle:
    """candle_history row → canonical Candle. market 컬럼(명시 저장)으로 거래소 tz 복원."""
    ts_utc = datetime.fromisoformat(r["ts_utc"]).astimezone(timezone.utc)
    market = r["market"]
    return Candle(
        symbol=r["symbol"], interval=r["interval"],
        ts_utc=ts_utc, ts_exchange=ts_utc.astimezone(exchange_tz(market)),
        session=r["session"],
        open=r["open"], high=r["high"], low=r["low"], close=r["close"],
        volume=r["volume"], currency=r["currency"], adjusted=bool(r["adjusted"]),
        market=market,
    )


def load_candles(db_path: str, symbol: str, interval: str, *,
                 start: datetime | None = None,
                 end: datetime | None = None,
                 market: str | None = None) -> list[Candle]:
    """통합 저장소에서 (symbol, interval) 캔들을 시간 오름차순으로 반환(read-only).

    start/end 는 **tz-aware 필수**(UTC 로 정규화해 [start, end) 반개구간 필터, end 배타적·
    look-ahead 안전). naive datetime 은 실행 환경 TZ 로 해석돼 경계가 비결정적이 되므로 거부.
    market 지정 시 해당 시장으로 격리(시장 분리 평가 — 동일 티커가 두 시장 행을 가질 때
    통화/시장 혼재 방지). 미지정 시 종목의 전 시장.
    """
    for _name, _ts in (("start", start), ("end", end)):
        if _ts is not None and _ts.tzinfo is None:
            raise ValueError(
                f"{_name} 는 tz-aware 여야 한다(naive datetime 은 실행 환경 TZ 의존 → 결정론 위배).")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM candle_history WHERE symbol=? AND interval=?"
        params: list = [symbol, interval]
        if market is not None:
            sql += " AND market=?"
            params.append(market)
        if start is not None:
            sql += " AND ts_utc>=?"
            params.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            sql += " AND ts_utc<?"
            params.append(end.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY ts_utc ASC"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [_row_to_candle(r) for r in rows]
