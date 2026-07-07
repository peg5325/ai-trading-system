"""SQLite 캔들 저장소. 설계 08_Database 의 candle 테이블.

- upsert (symbol, interval, ts_utc) 유니크
- 조회: 기간/개수
저장은 UTC ISO 문자열 기준. 지표 계산은 저장소 밖(IndicatorEngine)에서.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.models import Candle, exchange_tz, market_from_currency

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candle (
    symbol       TEXT NOT NULL,
    interval     TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,   -- ISO8601 UTC
    ts_exchange  TEXT NOT NULL,   -- ISO8601 ET
    session      TEXT NOT NULL,
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    currency     TEXT NOT NULL,
    adjusted     INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, ts_utc)
);
"""


class CandleStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def flush(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CandleStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert_many(self, candles: list[Candle]) -> int:
        rows = [
            (
                c.symbol, c.interval,
                c.ts_utc.isoformat(), c.ts_exchange.isoformat(), c.session,
                c.open, c.high, c.low, c.close, c.volume,
                c.currency, 1 if c.adjusted else 0,
            )
            for c in candles
        ]
        self._conn.executemany(
            """INSERT OR REPLACE INTO candle
               (symbol, interval, ts_utc, ts_exchange, session,
                open, high, low, close, volume, currency, adjusted)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def count(self, symbol: str, interval: str) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM candle WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
        return int(cur.fetchone()["n"])

    def range(self, symbol: str, interval: str) -> tuple[datetime | None, datetime | None]:
        cur = self._conn.execute(
            "SELECT MIN(ts_utc) AS lo, MAX(ts_utc) AS hi FROM candle "
            "WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
        row = cur.fetchone()
        lo = datetime.fromisoformat(row["lo"]) if row["lo"] else None
        hi = datetime.fromisoformat(row["hi"]) if row["hi"] else None
        return lo, hi

    def query(self, symbol: str, interval: str, *,
              start: datetime | None = None, end: datetime | None = None,
              limit: int | None = None) -> list[Candle]:
        """저장된 캔들을 시간 오름차순으로 반환 (backtest/engine 입력용)."""
        sql = "SELECT * FROM candle WHERE symbol=? AND interval=?"
        params: list = [symbol, interval]
        if start is not None:
            sql += " AND ts_utc>=?"
            params.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            sql += " AND ts_utc<?"
            params.append(end.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY ts_utc ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self._conn.execute(sql, params)
        return [self._row_to_candle(r) for r in cur.fetchall()]

    def latest(self, symbol: str, interval: str, limit: int = 5) -> list[Candle]:
        cur = self._conn.execute(
            "SELECT * FROM candle WHERE symbol=? AND interval=? "
            "ORDER BY ts_utc DESC LIMIT ?",
            (symbol, interval, limit),
        )
        return [self._row_to_candle(r) for r in cur.fetchall()]

    @staticmethod
    def _row_to_candle(r: sqlite3.Row) -> Candle:
        ts_utc = datetime.fromisoformat(r["ts_utc"]).astimezone(timezone.utc)
        market = market_from_currency(r["currency"])   # 통화로 시장 복원
        return Candle(
            symbol=r["symbol"], interval=r["interval"],
            ts_utc=ts_utc, ts_exchange=ts_utc.astimezone(exchange_tz(market)),
            session=r["session"],
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"], currency=r["currency"], adjusted=bool(r["adjusted"]),
            market=market,
        )
