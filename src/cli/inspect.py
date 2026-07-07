"""조회(read-only) 인스펙션 CLI — 4개 SQLite Single Source of Truth 를 안전하게 조회한다.

- journal_db / review_db / metrics_db / system_review_db 모두 표준 read-only 연결
  (`file:...?mode=ro` + `PRAGMA query_only=ON`)로만 열며, Store 클래스(CREATE TABLE +
  read-write)는 절대 인스턴스화하지 않는다. 실행 중인 Paper Trading 루프의 쓰기와
  충돌하지 않는다.
- 매매/주문/Runtime 제어 기능은 전혀 없다. 조회 전용.
- reader 함수는 모두 명시적 start/end datetime(또는 None)만 받는다. `datetime.now()`
  호출은 argparse 레이어(`_parse_since`)에만 존재한다 — 결정론적 테스트를 위함.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.review.aggregator import RuleImprovementAggregator
from src.review.store import ReviewRecord
from src.runtime.config import RuntimeConfig, load_yaml

DEFAULT_CONFIG = "config/runtime_mixed.yaml"

# --config 파일을 못 찾을 때 폴백으로 쓰는 문서화된 기본 경로(runtime_mixed.yaml 과 동일).
_FALLBACK_DB_PATHS = {
    "journal_db": "data/journal_mixed.sqlite",
    "review_db": "data/reviews_mixed.sqlite",
    "metrics_db": "data/metrics_mixed.sqlite",
    "system_review_db": "data/system_reviews_mixed.sqlite",
}


class DBError(Exception):
    """읽기 전용 DB 접근/쿼리 실패 — CLI 레이어에서 친절한 메시지로 변환하기 위한 예외."""


# ---------------------------------------------------------------------------
# 읽기 전용 연결 helper
# ---------------------------------------------------------------------------

def _connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """읽기 전용(mode=ro) SQLite 연결을 연다. CREATE TABLE/쓰기 없음 → 라이브 writer 와 안전."""
    path = Path(db_path)
    if not path.exists():
        raise DBError(f"DB 파일이 없습니다: {path} (아직 생성되지 않았거나 경로 오류)")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
    except sqlite3.OperationalError as e:
        raise DBError(f"DB 를 열 수 없습니다: {path} ({e})") from e
    return con


@contextmanager
def _ro(db_path: str | Path):
    con = _connect_ro(db_path)
    try:
        yield con
    finally:
        con.close()


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _require_table(con: sqlite3.Connection, table: str, db_path: str | Path) -> None:
    if not _table_exists(con, table):
        raise DBError(f"테이블이 없습니다: {table} (DB: {db_path})")


def _query(con: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise DBError(f"쿼리 실패: {e}") from e


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None


def _apply_ts_range(sql: str, params: list, column: str,
                    start: datetime | None, end: datetime | None) -> tuple[str, list]:
    lo, hi = _iso(start), _iso(end)
    if lo is not None:
        sql += f" AND {column}>=?"
        params.append(lo)
    if hi is not None:
        sql += f" AND {column}<=?"
        params.append(hi)
    return sql, params


# ---------------------------------------------------------------------------
# reader 함수 — 명시적 start/end 만 받는다 (now() 금지)
# ---------------------------------------------------------------------------

def read_summary(cfg: RuntimeConfig, *, journal_db: str | None = None) -> dict:
    """4개 DB 를 훑는 고수준 개요. 개별 DB 가 없어도 크래시하지 않고 note 로 남긴다."""
    return {
        "journal": _summary_journal(journal_db or cfg.journal_db),
        "reviews": _summary_reviews(cfg.review_db),
        "metrics": _summary_metrics(cfg.metrics_db),
        "system_reviews": _summary_system_reviews(cfg.system_review_db),
    }


def _summary_journal(db_path: str) -> dict:
    try:
        with _ro(db_path) as con:
            _require_table(con, "journal_cycle", db_path)
            rows = _query(con, "SELECT status, COUNT(*) AS n FROM journal_cycle GROUP BY status")
            status_counts = {(r["status"] or "UNKNOWN"): r["n"] for r in rows}
            rng = _query(con, "SELECT MIN(cycle_ts) AS lo, MAX(cycle_ts) AS hi FROM journal_cycle")[0]
            return {
                "available": True, "db": str(db_path),
                "cycle_count": sum(status_counts.values()),
                "status_counts": status_counts,
                "time_range": {"start": rng["lo"], "end": rng["hi"]},
            }
    except DBError as e:
        return {"available": False, "note": str(e)}


def _summary_reviews(db_path: str) -> dict:
    try:
        with _ro(db_path) as con:
            _require_table(con, "review", db_path)
            rows = _query(con, "SELECT status, COUNT(*) AS n FROM review GROUP BY status")
            status_counts = {(r["status"] or "UNKNOWN"): r["n"] for r in rows}
            qrows = _query(con, "SELECT decision_quality, COUNT(*) AS n FROM review GROUP BY decision_quality")
            quality_counts = {(r["decision_quality"] or "UNKNOWN"): r["n"] for r in qrows}
            return {
                "available": True, "db": str(db_path),
                "review_count": sum(status_counts.values()),
                "status_counts": status_counts,
                "quality_counts": quality_counts,
            }
    except DBError as e:
        return {"available": False, "note": str(e)}


def _summary_metrics(db_path: str) -> dict:
    try:
        with _ro(db_path) as con:
            _require_table(con, "metric_cycle", db_path)
            row = _query(con, """
                SELECT COUNT(*) AS rows,
                       COALESCE(SUM(cycles_run),0) AS cycles_run,
                       COALESCE(SUM(api_errors),0) AS api_errors,
                       COALESCE(SUM(exceptions),0) AS exceptions,
                       COALESCE(SUM(sqlite_locks),0) AS sqlite_locks,
                       COALESCE(SUM(orders),0) AS orders,
                       COALESCE(AVG(loop_ms),0) AS avg_loop_ms,
                       COALESCE(MAX(loop_ms),0) AS max_loop_ms,
                       SUM(CASE WHEN health_status IS NOT NULL AND health_status!='OK'
                                THEN 1 ELSE 0 END) AS degraded
                FROM metric_cycle
            """)[0]
            return {
                "available": True, "db": str(db_path), "row_count": row["rows"],
                "sum_cycles_run": row["cycles_run"], "sum_api_errors": row["api_errors"],
                "sum_exceptions": row["exceptions"], "sum_sqlite_locks": row["sqlite_locks"],
                "sum_orders": row["orders"],
                "avg_loop_ms": round(row["avg_loop_ms"] or 0.0, 2),
                "max_loop_ms": round(row["max_loop_ms"] or 0.0, 2),
                "degraded_count": row["degraded"] or 0,
            }
    except DBError as e:
        return {"available": False, "note": str(e)}


def _summary_system_reviews(db_path: str) -> dict:
    try:
        with _ro(db_path) as con:
            _require_table(con, "system_review", db_path)
            count = _query(con, "SELECT COUNT(*) AS n FROM system_review")[0]["n"]
            latest = _query(con, "SELECT grade, created_at FROM system_review ORDER BY id DESC LIMIT 1")
            latest_row = latest[0] if latest else None
            return {
                "available": True, "db": str(db_path), "review_count": count,
                "latest_grade": latest_row["grade"] if latest_row else None,
                "latest_created_at": latest_row["created_at"] if latest_row else None,
            }
    except DBError as e:
        return {"available": False, "note": str(e)}


def read_cycles(journal_db: str, *, limit: int = 20, newest_first: bool = False,
                start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    order = "DESC" if newest_first else "ASC"
    with _ro(journal_db) as con:
        _require_table(con, "journal_cycle", journal_db)
        sql = ("SELECT cycle_id, cycle_ts, status, reason, session, order_count "
               "FROM journal_cycle WHERE 1=1")
        params: list = []
        sql, params = _apply_ts_range(sql, params, "cycle_ts", start, end)
        sql += f" ORDER BY cycle_ts {order} LIMIT ?"
        params.append(limit)
        return [dict(r) for r in _query(con, sql, params)]


def read_latest(journal_db: str, *, limit: int = 10) -> list[dict]:
    return read_cycles(journal_db, limit=limit, newest_first=True)


def read_errors(journal_db: str, metrics_db: str, *,
                start: datetime | None = None, end: datetime | None = None) -> dict:
    """journal 의 ERROR/error 사이클 + metrics 의 api_errors/exceptions/sqlite_locks>0 행.
    두 DB 중 하나가 없어도 크래시하지 않고 note 로 남긴다(다른 쪽은 계속 보여줌)."""
    try:
        with _ro(journal_db) as con:
            _require_table(con, "journal_cycle", journal_db)
            sql = ("SELECT cycle_ts, status, reason, error FROM journal_cycle "
                   "WHERE (status='ERROR' OR error IS NOT NULL)")
            params: list = []
            sql, params = _apply_ts_range(sql, params, "cycle_ts", start, end)
            sql += " ORDER BY cycle_ts DESC"
            journal_errors: list | dict = [dict(r) for r in _query(con, sql, params)]
    except DBError as e:
        journal_errors = {"available": False, "note": str(e)}

    try:
        with _ro(metrics_db) as con:
            _require_table(con, "metric_cycle", metrics_db)
            sql = ("SELECT created_at, status, api_errors, exceptions, sqlite_locks "
                   "FROM metric_cycle WHERE (api_errors>0 OR exceptions>0 OR sqlite_locks>0)")
            params = []
            sql, params = _apply_ts_range(sql, params, "created_at", start, end)
            sql += " ORDER BY created_at DESC"
            metrics_errors: list | dict = [dict(r) for r in _query(con, sql, params)]
    except DBError as e:
        metrics_errors = {"available": False, "note": str(e)}

    return {"journal_errors": journal_errors, "metrics_errors": metrics_errors}


def read_candidates(journal_db: str, *, limit: int = 20, symbol: str | None = None) -> list[dict]:
    with _ro(journal_db) as con:
        _require_table(con, "journal_outcome", journal_db)
        sql = "SELECT cycle_ts, symbol, data FROM journal_outcome WHERE 1=1"
        params: list = []
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY cycle_ts DESC"
        out: list[dict] = []
        for r in _query(con, sql, params):
            data = json.loads(r["data"])
            candidate = data.get("candidate")
            if candidate is None:
                continue
            out.append({
                "cycle_ts": r["cycle_ts"], "symbol": r["symbol"],
                "score": candidate.get("score"),
                "candidate_type": candidate.get("candidate_type"),
                "ai_decision": (data.get("ai_decision") or {}).get("decision"),
            })
            if len(out) >= limit:
                break
        return out


def read_orders(journal_db: str, *, limit: int = 20, symbol: str | None = None) -> list[dict]:
    with _ro(journal_db) as con:
        _require_table(con, "journal_outcome", journal_db)
        sql = "SELECT cycle_ts, symbol, data FROM journal_outcome WHERE 1=1"
        params: list = []
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        sql += " ORDER BY cycle_ts DESC"
        out: list[dict] = []
        for r in _query(con, sql, params):
            data = json.loads(r["data"])
            order = data.get("order_record")
            if not order or order.get("status") != "FILLED":
                continue
            out.append({
                "cycle_ts": r["cycle_ts"], "symbol": r["symbol"],
                "side": order.get("side"), "kind": order.get("kind"),
                "filled_quantity": order.get("filled_quantity"),
                "avg_fill_price": order.get("avg_fill_price"),
            })
            if len(out) >= limit:
                break
        return out


def read_reviews(review_db: str, *, limit: int = 20, review_version: str | None = None,
                 model: str | None = None) -> list[dict]:
    with _ro(review_db) as con:
        _require_table(con, "review", review_db)
        sql = ("SELECT cycle_ts, decision_quality, confidence_reassessment, model_name, "
               "review_version, data FROM review WHERE 1=1")
        params: list = []
        if review_version is not None:
            sql += " AND review_version=?"
            params.append(review_version)
        if model is not None:
            sql += " AND model_name=?"
            params.append(model)
        sql += " ORDER BY cycle_ts DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in _query(con, sql, params):
            data = json.loads(r["data"])
            out.append({
                "cycle_ts": r["cycle_ts"], "decision_quality": r["decision_quality"],
                "confidence_reassessment": r["confidence_reassessment"],
                "model_name": r["model_name"], "review_version": r["review_version"],
                "rule_improvement_count": len(data.get("rule_improvement") or []),
            })
        return out


class _ReadOnlyReviewAdapter:
    """RuleImprovementAggregator 가 기대하는 `ReviewStore.query(**filters)` 의 read-only 어댑터.
    review 테이블을 직접 읽어 ReviewRecord 리스트로 변환한다(쓰기 없음)."""

    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              **filters) -> list[ReviewRecord]:
        sql = "SELECT data FROM review WHERE 1=1"
        params: list = []
        for col in ("prompt_version", "model_name", "review_version", "decision_quality"):
            val = filters.get(col)
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        sql, params = _apply_ts_range(sql, params, "cycle_ts", start, end)
        rows = _query(self._con, sql, params)
        return [ReviewRecord.from_dict(json.loads(r["data"])) for r in rows]


def read_improvements(review_db: str, *, top_n: int = 10,
                      start: datetime | None = None, end: datetime | None = None,
                      prompt_version: str | None = None, model_name: str | None = None,
                      review_version: str | None = None,
                      decision_quality: str | None = None) -> dict:
    with _ro(review_db) as con:
        _require_table(con, "review", review_db)
        adapter = _ReadOnlyReviewAdapter(con)
        report = RuleImprovementAggregator(adapter, top_n=top_n).aggregate(
            start=start, end=end, prompt_version=prompt_version, model_name=model_name,
            review_version=review_version, decision_quality=decision_quality)
        return report.to_dict()


def read_metrics(metrics_db: str, *, start: datetime | None = None,
                 end: datetime | None = None) -> dict:
    with _ro(metrics_db) as con:
        _require_table(con, "metric_cycle", metrics_db)
        sql = """
            SELECT COUNT(*) AS rows,
                   COALESCE(SUM(cycles_run),0) AS cycles_run,
                   COALESCE(SUM(skipped),0) AS skipped,
                   COALESCE(SUM(api_errors),0) AS api_errors,
                   COALESCE(SUM(pipeline_errors),0) AS pipeline_errors,
                   COALESCE(SUM(exceptions),0) AS exceptions,
                   COALESCE(SUM(sqlite_locks),0) AS sqlite_locks,
                   COALESCE(SUM(candidates),0) AS candidates,
                   COALESCE(SUM(ai_approve),0) AS ai_approve,
                   COALESCE(SUM(ai_wait),0) AS ai_wait,
                   COALESCE(SUM(ai_reject),0) AS ai_reject,
                   COALESCE(SUM(risk_block),0) AS risk_block,
                   COALESCE(SUM(orders),0) AS orders,
                   COALESCE(SUM(buys),0) AS buys,
                   COALESCE(SUM(sells),0) AS sells,
                   COALESCE(SUM(exits),0) AS exits,
                   COALESCE(SUM(eods),0) AS eods,
                   COALESCE(SUM(reviews),0) AS reviews,
                   COALESCE(AVG(loop_ms),0) AS avg_loop_ms,
                   COALESCE(MAX(loop_ms),0) AS max_loop_ms,
                   SUM(CASE WHEN health_status IS NOT NULL AND health_status!='OK'
                            THEN 1 ELSE 0 END) AS degraded
            FROM metric_cycle WHERE 1=1
        """
        params: list = []
        sql, params = _apply_ts_range(sql, params, "created_at", start, end)
        row = _query(con, sql, params)[0]
        out = {k: row[k] for k in row.keys()}
        out["avg_loop_ms"] = round(out["avg_loop_ms"] or 0.0, 2)
        out["max_loop_ms"] = round(out["max_loop_ms"] or 0.0, 2)
        out["degraded"] = out["degraded"] or 0

        bsql = "SELECT status, COUNT(*) AS n FROM metric_cycle WHERE 1=1"
        bparams: list = []
        bsql, bparams = _apply_ts_range(bsql, bparams, "created_at", start, end)
        bsql += " GROUP BY status"
        out["status_breakdown"] = {(r["status"] or "UNKNOWN"): r["n"] for r in _query(con, bsql, bparams)}
        return out


def read_system_reviews(system_review_db: str, *, limit: int = 20) -> list[dict]:
    with _ro(system_review_db) as con:
        _require_table(con, "system_review", system_review_db)
        sql = ("SELECT created_at, window_start, window_end, grade, confidence, status, "
               "model_name FROM system_review ORDER BY id DESC LIMIT ?")
        return [dict(r) for r in _query(con, sql, (limit,))]


def read_system_review_latest(system_review_db: str) -> dict | None:
    with _ro(system_review_db) as con:
        _require_table(con, "system_review", system_review_db)
        rows = _query(con, "SELECT data FROM system_review ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        data = json.loads(rows[0]["data"])
        # summary/major_issues/recommendations 는 SystemReviewLog 의 parsed_response 안에 있다
        parsed = data.get("parsed_response") or {}
        return {
            "created_at": data.get("created_at"), "window_start": data.get("window_start"),
            "window_end": data.get("window_end"), "grade": data.get("grade"),
            "confidence": data.get("confidence"), "status": data.get("status"),
            "model_name": data.get("model_name"),
            "summary": data.get("summary") or parsed.get("summary"),
            "major_issues": data.get("major_issues") or parsed.get("major_issues", []),
            "recommendations": data.get("recommendations") or parsed.get("recommendations", []),
        }


def read_health(metrics_db: str, *, start: datetime | None = None,
               end: datetime | None = None) -> dict:
    with _ro(metrics_db) as con:
        _require_table(con, "metric_cycle", metrics_db)
        latest_rows = _query(con, "SELECT cycle_ts, status, health_status, created_at "
                                  "FROM metric_cycle ORDER BY id DESC LIMIT 1")
        latest = dict(latest_rows[0]) if latest_rows else None

        sql = ("SELECT COUNT(*) AS rows, "
               "SUM(CASE WHEN health_status IS NOT NULL AND health_status!='OK' "
               "THEN 1 ELSE 0 END) AS degraded, "
               "COALESCE(SUM(api_errors),0) AS api_errors, "
               "COALESCE(SUM(exceptions),0) AS exceptions, "
               "COALESCE(SUM(sqlite_locks),0) AS sqlite_locks "
               "FROM metric_cycle WHERE 1=1")
        params: list = []
        sql, params = _apply_ts_range(sql, params, "created_at", start, end)
        row = dict(_query(con, sql, params)[0])
        row["degraded"] = row["degraded"] or 0
        return {"latest": latest, "window": row}


# ---------------------------------------------------------------------------
# 출력 포맷터 — 외부 의존성(tabulate/pandas) 없이 직접 정렬
# ---------------------------------------------------------------------------

def _render_table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "(데이터 없음)"
    str_rows = [["" if c is None else str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def _fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [_fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [_fmt(row) for row in str_rows]
    return "\n".join(lines)


def _format_summary(data: dict) -> str:
    lines = ["=== Summary ==="]

    j = data["journal"]
    if j.get("available"):
        lines.append(f"[journal] {j['db']}")
        lines.append(f"  cycles={j['cycle_count']}  status={j['status_counts']}")
        lines.append(f"  range: {j['time_range']['start']} ~ {j['time_range']['end']}")
    else:
        lines.append(f"[journal] {j['note']}")

    r = data["reviews"]
    if r.get("available"):
        lines.append(f"[reviews] {r['db']}")
        lines.append(f"  reviews={r['review_count']}  status={r['status_counts']}  "
                    f"quality={r['quality_counts']}")
    else:
        lines.append(f"[reviews] {r['note']}")

    m = data["metrics"]
    if m.get("available"):
        lines.append(f"[metrics] {m['db']}")
        lines.append(f"  rows={m['row_count']}  cycles_run={m['sum_cycles_run']} "
                    f"api_errors={m['sum_api_errors']} exceptions={m['sum_exceptions']} "
                    f"sqlite_locks={m['sum_sqlite_locks']} orders={m['sum_orders']}")
        lines.append(f"  avg_loop_ms={m['avg_loop_ms']} max_loop_ms={m['max_loop_ms']} "
                    f"degraded={m['degraded_count']}")
    else:
        lines.append(f"[metrics] {m['note']}")

    sr = data["system_reviews"]
    if sr.get("available"):
        lines.append(f"[system_reviews] {sr['db']}")
        lines.append(f"  reviews={sr['review_count']}  "
                    f"latest_grade={sr['latest_grade']} ({sr['latest_created_at']})")
    else:
        lines.append(f"[system_reviews] {sr['note']}")

    return "\n".join(lines)


def _format_cycles(rows: list[dict]) -> str:
    headers = ["cycle_ts", "status", "reason", "session", "order_count"]
    return _render_table(headers, [[r.get(k) for k in headers] for r in rows])


def _format_errors(data: dict) -> str:
    lines = ["=== Journal Errors ==="]
    je = data["journal_errors"]
    if isinstance(je, dict):
        lines.append(je["note"])
    else:
        lines.append(_render_table(["cycle_ts", "reason", "error"],
                                   [[r["cycle_ts"], r["reason"], r["error"]] for r in je]))
    lines.append("")
    lines.append("=== Metrics Errors ===")
    me = data["metrics_errors"]
    if isinstance(me, dict):
        lines.append(me["note"])
    else:
        lines.append(_render_table(["created_at", "status", "api_errors", "exceptions", "sqlite_locks"],
                                   [[r["created_at"], r["status"], r["api_errors"],
                                     r["exceptions"], r["sqlite_locks"]] for r in me]))
    return "\n".join(lines)


def _format_candidates(rows: list[dict]) -> str:
    headers = ["cycle_ts", "symbol", "score", "candidate_type", "ai_decision"]
    return _render_table(headers, [[r.get(k) for k in headers] for r in rows])


def _format_orders(rows: list[dict]) -> str:
    headers = ["cycle_ts", "symbol", "side", "kind", "filled_quantity", "avg_fill_price"]
    return _render_table(headers, [[r.get(k) for k in headers] for r in rows])


def _format_reviews(rows: list[dict]) -> str:
    headers = ["cycle_ts", "decision_quality", "confidence_reassessment", "model_name",
              "review_version", "rule_improvement_count"]
    return _render_table(headers, [[r.get(k) for k in headers] for r in rows])


def _format_improvements(data: dict) -> str:
    lines = ["=== Rule Improvements ==="]
    lines.append(f"total_reviews={data['total_reviews']} ok={data['ok_reviews']} "
                f"failed={data['failed_reviews']}")
    lines.append(f"decision_quality={data['decision_quality_distribution']}  "
                f"confidence={data['confidence']}")
    lines.append("")
    lines.append("-- Top Rule Improvements --")
    ri = data["rule_improvement_clusters"][:data["top_n"]]
    lines.append(_render_table(["count", "representative", "quality_distribution"],
                               [[c["count"], c["representative"], c["quality_distribution"]] for c in ri]))
    lines.append("")
    lines.append("-- Top Missed Signals --")
    ms = data["missed_signal_clusters"][:data["top_n"]]
    lines.append(_render_table(["count", "representative", "quality_distribution"],
                               [[c["count"], c["representative"], c["quality_distribution"]] for c in ms]))
    return "\n".join(lines)


def _format_metrics(data: dict) -> str:
    lines = ["=== Metrics Aggregate ===", f"rows={data['rows']}",
             f"cycles_run={data['cycles_run']} skipped={data['skipped']} "
             f"api_errors={data['api_errors']} pipeline_errors={data['pipeline_errors']} "
             f"exceptions={data['exceptions']} sqlite_locks={data['sqlite_locks']}",
             f"candidates={data['candidates']} ai_approve={data['ai_approve']} "
             f"ai_wait={data['ai_wait']} ai_reject={data['ai_reject']} "
             f"risk_block={data['risk_block']}",
             f"orders={data['orders']} buys={data['buys']} sells={data['sells']} "
             f"exits={data['exits']} eods={data['eods']} reviews={data['reviews']}",
             f"avg_loop_ms={data['avg_loop_ms']} max_loop_ms={data['max_loop_ms']} "
             f"degraded={data['degraded']}",
             f"status_breakdown={data['status_breakdown']}"]
    return "\n".join(lines)


def _format_system_reviews(rows: list[dict]) -> str:
    headers = ["created_at", "window_start", "window_end", "grade", "confidence", "status", "model_name"]
    return _render_table(headers, [[r.get(k) for k in headers] for r in rows])


def _format_system_review_latest(data: dict | None) -> str:
    if data is None:
        return "(데이터 없음)"
    lines = [
        f"created_at={data['created_at']}  window={data['window_start']} ~ {data['window_end']}",
        f"status={data['status']} grade={data['grade']} confidence={data['confidence']} "
        f"model={data['model_name']}",
        "", f"summary: {data['summary']}", "", "major_issues:",
    ]
    lines += [f"  - {x}" for x in data["major_issues"]] if data["major_issues"] else ["  (없음)"]
    lines.append("")
    lines.append("recommendations:")
    lines += [f"  - {x}" for x in data["recommendations"]] if data["recommendations"] else ["  (없음)"]
    return "\n".join(lines)


def _format_health(data: dict) -> str:
    latest = data["latest"]
    agg = data["window"]
    lines = ["=== Health ==="]
    if latest:
        lines.append(f"latest: cycle_ts={latest['cycle_ts']} status={latest['status']} "
                    f"health_status={latest['health_status']} created_at={latest['created_at']}")
    else:
        lines.append("latest: (데이터 없음)")
    lines.append(f"window: rows={agg['rows']} degraded={agg['degraded']} "
                f"api_errors={agg['api_errors']} exceptions={agg['exceptions']} "
                f"sqlite_locks={agg['sqlite_locks']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --since / --last 시간 파싱 (argparse 레이어 전용 — reader 는 datetime 만 받는다)
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)([hmd])$")


def _parse_since(s: str | None) -> datetime | None:
    """'today' | '<N>h'/'<N>m'/'<N>d' | ISO 'YYYY-MM-DDTHH:MM:SSZ' → aware UTC datetime.
    now() 를 쓰는 유일한 지점(테스트는 reader 함수를 직접 호출해 이 함수를 우회한다)."""
    if not s:
        return None
    if s == "today":
        return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    m = _RELATIVE_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "m": timedelta(minutes=n), "d": timedelta(days=n)}[unit]
        return datetime.now(timezone.utc) - delta
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"인식할 수 없는 시간 형식: {s} (today | Nh | Nm | Nd | ISO 'YYYY-MM-DDTHH:MM:SSZ')")


# ---------------------------------------------------------------------------
# argparse 레이어
# ---------------------------------------------------------------------------

def _load_config(path: str) -> RuntimeConfig:
    """설정 파일을 읽는다. 파일이 없으면 문서화된 기본 경로로 폴백(--db 오버라이드는 계속 허용)."""
    if not Path(path).exists():
        return RuntimeConfig(**_FALLBACK_DB_PATHS)
    try:
        return load_yaml(path)
    except OSError:
        return RuntimeConfig(**_FALLBACK_DB_PATHS)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, help="runtime yaml 경로")
    common.add_argument("--db", default=None, help="이 커맨드의 기본 DB 경로 오버라이드")
    common.add_argument("--json", action="store_true", help="JSON 으로 출력")

    parser = argparse.ArgumentParser(
        prog="inspect", description="Paper Trading 4대 SQLite DB 읽기 전용 조회 CLI",
        parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", parents=[common], help="4개 DB 전체 개요")

    p_cycles = sub.add_parser("cycles", parents=[common], help="최근 journal_cycle 목록")
    p_cycles.add_argument("--limit", type=int, default=20)

    p_latest = sub.add_parser("latest", parents=[common], help="최신 N 개 사이클(최신순)")
    p_latest.add_argument("--limit", type=int, default=10)

    p_errors = sub.add_parser("errors", parents=[common], help="journal/metrics 의 에러 사이클")
    p_errors.add_argument("--since", default=None, help="today | Nh | Nm | Nd | ISO")

    p_candidates = sub.add_parser("candidates", parents=[common], help="candidate 가 있는 outcome")
    p_candidates.add_argument("--limit", type=int, default=20)
    p_candidates.add_argument("--symbol", default=None)

    p_orders = sub.add_parser("orders", parents=[common], help="체결(FILLED) 주문 목록")
    p_orders.add_argument("--limit", type=int, default=20)
    p_orders.add_argument("--symbol", default=None)

    p_reviews = sub.add_parser("reviews", parents=[common], help="최근 review 목록")
    p_reviews.add_argument("--limit", type=int, default=20)
    p_reviews.add_argument("--review-version", default=None)
    p_reviews.add_argument("--model", default=None)

    p_improvements = sub.add_parser("improvements", parents=[common],
                                    help="RuleImprovementAggregator 집계")
    p_improvements.add_argument("--top", type=int, default=10)
    p_improvements.add_argument("--since", default=None, help="today | Nh | Nm | Nd | ISO")

    p_metrics = sub.add_parser("metrics", parents=[common], help="metrics 윈도우 집계")
    p_metrics.add_argument("--last", default=None, help="today | Nh | Nm | Nd | ISO")

    p_sysrev = sub.add_parser("system-review", parents=[common], help="system_review 목록/최신")
    p_sysrev.add_argument("--latest", action="store_true", help="가장 최근 1건만 상세 표시")
    p_sysrev.add_argument("--limit", type=int, default=20)

    p_health = sub.add_parser("health", parents=[common], help="metrics 기반 health 스냅샷")
    p_health.add_argument("--last", default=None, help="today | Nh | Nm | Nd | ISO")

    return parser


def _dispatch(args: argparse.Namespace, cfg: RuntimeConfig) -> tuple[object, str]:
    cmd = args.command
    if cmd == "summary":
        data = read_summary(cfg, journal_db=args.db)
        return data, _format_summary(data)
    if cmd == "cycles":
        data = read_cycles(args.db or cfg.journal_db, limit=args.limit)
        return data, _format_cycles(data)
    if cmd == "latest":
        data = read_latest(args.db or cfg.journal_db, limit=args.limit)
        return data, _format_cycles(data)
    if cmd == "errors":
        since = _parse_since(args.since)
        data = read_errors(args.db or cfg.journal_db, cfg.metrics_db, start=since)
        return data, _format_errors(data)
    if cmd == "candidates":
        data = read_candidates(args.db or cfg.journal_db, limit=args.limit, symbol=args.symbol)
        return data, _format_candidates(data)
    if cmd == "orders":
        data = read_orders(args.db or cfg.journal_db, limit=args.limit, symbol=args.symbol)
        return data, _format_orders(data)
    if cmd == "reviews":
        data = read_reviews(args.db or cfg.review_db, limit=args.limit,
                            review_version=args.review_version, model=args.model)
        return data, _format_reviews(data)
    if cmd == "improvements":
        since = _parse_since(args.since)
        data = read_improvements(args.db or cfg.review_db, top_n=args.top, start=since)
        return data, _format_improvements(data)
    if cmd == "metrics":
        since = _parse_since(args.last)
        data = read_metrics(args.db or cfg.metrics_db, start=since)
        return data, _format_metrics(data)
    if cmd == "system-review":
        if args.latest:
            data = read_system_review_latest(args.db or cfg.system_review_db)
            return data, _format_system_review_latest(data)
        data = read_system_reviews(args.db or cfg.system_review_db, limit=args.limit)
        return data, _format_system_reviews(data)
    if cmd == "health":
        since = _parse_since(args.last)
        data = read_health(args.db or cfg.metrics_db, start=since)
        return data, _format_health(data)
    raise ValueError(f"알 수 없는 커맨드: {cmd}")


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cfg = _load_config(args.config)

    try:
        data, human = _dispatch(args, cfg)
    except DBError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
