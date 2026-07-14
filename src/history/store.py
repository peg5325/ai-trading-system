"""HistoryStore — 통합 candle 저장소(market_history.sqlite) 초기화 + 병합.

원칙:
- 기존 session DB 는 read-only(mode=ro)로만 읽는다(원본 불변).
- 통합 DB 는 append/UPSERT only. 삭제/덮어쓰기 없음. 재실행 idempotent.
- PK (symbol, interval, ts_utc) 충돌 = 동일 봉. 값 동일이면 no-op, 다르면 conflict 정책 + 로그.
- Toss API 호출 없음(이 모듈은 순수 로컬 병합).
"""
from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.models import market_from_currency

SCHEMA = """
CREATE TABLE IF NOT EXISTS candle_history (
    symbol       TEXT NOT NULL,
    market       TEXT NOT NULL,
    interval     TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,
    ts_exchange  TEXT NOT NULL,
    session      TEXT NOT NULL,
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    currency     TEXT NOT NULL,
    adjusted     INTEGER NOT NULL DEFAULT 1,
    source       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, ts_utc)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_hist_market_ts  ON candle_history(market, interval, ts_utc);
CREATE INDEX IF NOT EXISTS ix_hist_symbol_day ON candle_history(symbol, interval, substr(ts_exchange,1,10));

CREATE TABLE IF NOT EXISTS ingest_log (
    run_id       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    source       TEXT NOT NULL,
    rows_seen    INTEGER NOT NULL,
    rows_inserted INTEGER NOT NULL,
    rows_updated INTEGER NOT NULL,
    rows_duplicate INTEGER NOT NULL,
    rows_conflict INTEGER NOT NULL,
    earliest     TEXT,
    latest       TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS import_conflict (
    ts           TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    interval     TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,
    field        TEXT NOT NULL,
    existing_val TEXT,
    incoming_val TEXT,
    existing_source TEXT,
    incoming_source TEXT,
    resolution   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backfill_cursor (
    symbol       TEXT NOT NULL,
    interval     TEXT NOT NULL,
    oldest_ts_utc TEXT,
    next_before  TEXT,
    exhausted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT,
    PRIMARY KEY (symbol, interval)
);
"""

# 값 비교 대상 필드(동일 봉 판정)
_VALUE_FIELDS = ("open", "high", "low", "close", "volume", "session")
_EPS = 1e-6

# source 신뢰 순위(높을수록 우선). backfill(수정주가 원천) > session 라이브 캡처.
_SOURCE_RANK = {"toss_backfill": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(source, 1)  # import:* 등은 기본 1


@dataclass
class ImportStats:
    source: str
    rows_seen: int = 0
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    conflict: int = 0
    earliest: str | None = None
    latest: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": self.source, "rows_seen": self.rows_seen,
            "inserted": self.inserted, "updated": self.updated,
            "duplicate": self.duplicate, "conflict": self.conflict,
            "earliest": self.earliest, "latest": self.latest,
            "errors": self.errors,
        }


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._con = sqlite3.connect(db_path)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.executescript(SCHEMA)
        self._con.commit()

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- schema introspection (리포트/검증용) ----
    def schema_sql(self) -> str:
        rows = self._con.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
        ).fetchall()
        return "\n\n".join(r[0] for r in rows)

    def symbol_coverage(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT symbol, market, interval, COUNT(*) AS n, "
            "MIN(ts_utc) AS earliest, MAX(ts_utc) AS latest, "
            "COUNT(DISTINCT substr(ts_exchange,1,10)) AS days "
            "FROM candle_history GROUP BY symbol, interval ORDER BY market, symbol, interval"
        ).fetchall()
        return [dict(r) for r in rows]

    def total_rows(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM candle_history").fetchone()[0]

    # ---- core: idempotent upsert of one canonical row ----
    def _upsert_row(self, run_id: str, r: dict, stats: ImportStats) -> None:
        pk = (r["symbol"], r["interval"], r["ts_utc"])
        existing = self._con.execute(
            "SELECT * FROM candle_history WHERE symbol=? AND interval=? AND ts_utc=?", pk
        ).fetchone()
        now = _now()
        if existing is None:
            self._con.execute(
                "INSERT INTO candle_history (symbol,market,interval,ts_utc,ts_exchange,session,"
                "open,high,low,close,volume,currency,adjusted,source,created_at,updated_at) "
                "VALUES (:symbol,:market,:interval,:ts_utc,:ts_exchange,:session,"
                ":open,:high,:low,:close,:volume,:currency,:adjusted,:source,:created_at,:updated_at)",
                {**r, "created_at": now, "updated_at": now},
            )
            stats.inserted += 1
            return

        # 값 동일? -> idempotent no-op
        diffs = []
        for f in _VALUE_FIELDS:
            ev, iv = existing[f], r[f]
            if isinstance(ev, (int, float)) and isinstance(iv, (int, float)):
                if abs(float(ev) - float(iv)) > _EPS:
                    diffs.append((f, ev, iv))
            elif str(ev) != str(iv):
                diffs.append((f, ev, iv))
        if not diffs:
            stats.duplicate += 1
            return

        # 값 충돌: 우선순위(adjusted desc, source_rank desc). 동률이면 기존 유지(first-write-wins).
        stats.conflict += 1
        incoming_wins = (
            (int(r["adjusted"]), _source_rank(r["source"]))
            > (int(existing["adjusted"]), _source_rank(existing["source"]))
        )
        resolution = "overwritten" if incoming_wins else "kept_existing"
        for (f, ev, iv) in diffs:
            self._con.execute(
                "INSERT INTO import_conflict (ts,run_id,symbol,interval,ts_utc,field,"
                "existing_val,incoming_val,existing_source,incoming_source,resolution) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, run_id, r["symbol"], r["interval"], r["ts_utc"], f,
                 str(ev), str(iv), existing["source"], r["source"], resolution),
            )
        if incoming_wins:
            self._con.execute(
                "UPDATE candle_history SET open=:open,high=:high,low=:low,close=:close,"
                "volume=:volume,session=:session,ts_exchange=:ts_exchange,currency=:currency,"
                "adjusted=:adjusted,source=:source,updated_at=:updated_at "
                "WHERE symbol=:symbol AND interval=:interval AND ts_utc=:ts_utc",
                {**r, "updated_at": now},
            )
            stats.updated += 1

    # ---- import one session DB (read-only) ----
    def import_session_db(self, src_db: str, run_id: str | None = None) -> ImportStats:
        run_id = run_id or f"import-{_now()}"
        source = f"import:{os.path.basename(src_db)}"
        stats = ImportStats(source=source)
        try:
            ro = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
            ro.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            stats.errors.append(f"open: {e}")
            return stats
        try:
            tabs = [x[0] for x in ro.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if "candle" not in tabs:
                stats.errors.append("no candle table")
                return stats
            try:
                for row in ro.execute(
                    "SELECT symbol,interval,ts_utc,ts_exchange,session,open,high,low,close,"
                    "volume,currency,adjusted FROM candle"
                ):
                    d = dict(row)
                    # market_from_currency 는 문자열 비교만 하여 예외를 던지지 않으나,
                    # 방어적 폴백은 원함수(KRW→KR, 그 외→US)와 규칙을 일치시킨다.
                    try:
                        d["market"] = market_from_currency(d["currency"])
                    except Exception:
                        d["market"] = "KR" if str(d["currency"]).upper() == "KRW" else "US"
                    d["source"] = source
                    stats.rows_seen += 1
                    if stats.earliest is None or d["ts_utc"] < stats.earliest:
                        stats.earliest = d["ts_utc"]
                    if stats.latest is None or d["ts_utc"] > stats.latest:
                        stats.latest = d["ts_utc"]
                    self._upsert_row(run_id, d, stats)
            except sqlite3.OperationalError as e:
                # 컬럼 불일치 등 소스 스키마 문제 → 소스별 격리(import_glob 전체 중단 방지).
                stats.errors.append(f"query: {e}")
        finally:
            ro.close()
        self._con.execute(
            "INSERT INTO ingest_log (run_id,ts,source,rows_seen,rows_inserted,rows_updated,"
            "rows_duplicate,rows_conflict,earliest,latest,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, _now(), source, stats.rows_seen, stats.inserted, stats.updated,
             stats.duplicate, stats.conflict, stats.earliest, stats.latest,
             "; ".join(stats.errors) if stats.errors else None),
        )
        self._con.commit()
        return stats

    def import_glob(self, src_glob: str, run_id: str | None = None) -> list[ImportStats]:
        run_id = run_id or f"import-{_now()}"
        self_base = os.path.basename(self.db_path)
        out: list[ImportStats] = []
        for src in sorted(glob.glob(src_glob)):
            if os.path.basename(src) == self_base:
                continue  # 자기 자신 제외
            out.append(self.import_session_db(src, run_id=run_id))
        return out

    def commit(self) -> None:
        """드라이버가 페이지 단위로 커밋해 resume 내구성을 확보하도록 노출."""
        self._con.commit()

    # ---- backfill 지원(PR 3-A): canonical Candle upsert + cursor + 감사 로그 ----
    @staticmethod
    def _candle_to_row(candle, source: str) -> dict:
        """canonical Candle → candle_history row dict. ts 는 import 경로와 동일하게
        isoformat 문자열로 저장해 PK(symbol,interval,ts_utc)를 정합시킨다."""
        return {
            "symbol": candle.symbol, "market": candle.market, "interval": candle.interval,
            "ts_utc": candle.ts_utc.isoformat(), "ts_exchange": candle.ts_exchange.isoformat(),
            "session": candle.session,
            "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.volume,
            "currency": candle.currency, "adjusted": 1 if candle.adjusted else 0,
            "source": source,
        }

    def upsert_candles(self, candles: list, *, source: str, run_id: str,
                       stats: ImportStats) -> None:
        """canonical Candle 목록을 멱등 upsert(기존 _upsert_row 재사용). 커밋은 호출측."""
        for c in candles:
            self._upsert_row(run_id, self._candle_to_row(c, source), stats)

    def get_cursor(self, symbol: str, interval: str) -> dict | None:
        row = self._con.execute(
            "SELECT symbol, interval, oldest_ts_utc, next_before, exhausted, updated_at "
            "FROM backfill_cursor WHERE symbol=? AND interval=?", (symbol, interval),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["exhausted"] = bool(d["exhausted"])
        return d

    def save_cursor(self, symbol: str, interval: str, *, oldest_ts_utc: str | None,
                    next_before: str | None, exhausted: bool) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO backfill_cursor "
            "(symbol, interval, oldest_ts_utc, next_before, exhausted, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (symbol, interval, oldest_ts_utc, next_before, 1 if exhausted else 0, _now()),
        )

    def write_ingest_log(self, run_id: str, *, source: str, stats: ImportStats,
                         rows_seen: int, earliest: str | None, latest: str | None,
                         note: str | None) -> None:
        self._con.execute(
            "INSERT INTO ingest_log (run_id,ts,source,rows_seen,rows_inserted,rows_updated,"
            "rows_duplicate,rows_conflict,earliest,latest,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, _now(), source, rows_seen, stats.inserted, stats.updated,
             stats.duplicate, stats.conflict, earliest, latest, note),
        )
