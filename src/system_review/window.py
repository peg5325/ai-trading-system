"""운영 회고 입력 요약 — RuntimeMetricsStore(+Journal)에서 기간 지표를 집계.

결정론적. GPT 입력이자 SystemReviewReport.runtime_window 로 저장되는 스냅샷.
매매 전략을 평가하지 않는다 — 운영 품질 지표만 계산한다.
RuntimeMetricsStore 는 read-only(aggregate)로만 접근.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime


def build_window_summary(metrics_store, journal_store, *,
                         start: datetime | None = None,
                         end: datetime | None = None) -> dict:
    agg = metrics_store.aggregate(start=start, end=end)   # read-only

    ai_total = _i(agg, "ai_approve") + _i(agg, "ai_wait") + _i(agg, "ai_reject")
    candidates = _i(agg, "candidates")
    rows = _i(agg, "rows")
    error_events = _i(agg, "api_errors") + _i(agg, "pipeline_errors") + _i(agg, "exceptions")

    runtime = {
        "metric_rows": rows,
        "cycles_run": _i(agg, "cycles_run"),
        "skipped": _i(agg, "skipped"),
        "api_errors": _i(agg, "api_errors"),
        "pipeline_errors": _i(agg, "pipeline_errors"),
        "exceptions": _i(agg, "exceptions"),
        "sqlite_locks": _i(agg, "sqlite_locks"),
        "candidates": candidates,
        "ai_approve": _i(agg, "ai_approve"),
        "ai_wait": _i(agg, "ai_wait"),
        "ai_reject": _i(agg, "ai_reject"),
        "risk_block": _i(agg, "risk_block"),
        "orders": _i(agg, "orders"),
        "buys": _i(agg, "buys"),
        "sells": _i(agg, "sells"),
        "exits": _i(agg, "exits"),
        "eods": _i(agg, "eods"),
        "avg_loop_ms": agg.get("avg_loop_ms", 0.0),
        "max_loop_ms": agg.get("max_loop_ms", 0.0),
        "degraded_cycles": _i(agg, "degraded_cycles"),
        # 비율 (0 나눗셈 방지)
        "ai_total": ai_total,
        "approve_ratio": _ratio(_i(agg, "ai_approve"), ai_total),
        "wait_ratio": _ratio(_i(agg, "ai_wait"), ai_total),
        "reject_ratio": _ratio(_i(agg, "ai_reject"), ai_total),
        "risk_block_ratio": _ratio(_i(agg, "risk_block"), candidates),
        "candidate_to_order_ratio": _ratio(_i(agg, "orders"), candidates),
        "skipped_ratio": _ratio(_i(agg, "skipped"), rows),
        "degraded_ratio": _ratio(_i(agg, "degraded_cycles"), rows),
        "error_rate": _ratio(error_events, rows),
    }

    journal = _journal_summary(journal_store, start, end)

    data_quality = {
        "error_cycles": journal["error_cycles"],
        "error_cycle_ratio": _ratio(journal["error_cycles"], journal["cycles"]),
        "api_errors": _i(agg, "api_errors"),
        "exceptions": _i(agg, "exceptions"),
        "sqlite_locks": _i(agg, "sqlite_locks"),
    }

    return {
        "window": {"start": _iso(start), "end": _iso(end)},
        "runtime": runtime,
        "journal": journal,
        "data_quality": data_quality,
    }


def _journal_summary(journal_store, start, end) -> dict:
    cycles = journal_store.get_by_time_range(start, end)   # read-only
    status = Counter(c.status for c in cycles)
    sessions = Counter(c.session for c in cycles if c.session)
    error_cycles = sum(1 for c in cycles if getattr(c, "error", None))
    return {
        "cycles": len(cycles),
        "status_distribution": dict(status),
        "sessions": dict(sessions),
        "error_cycles": error_cycles,
    }


def _i(d: dict, k: str) -> int:
    v = d.get(k, 0)
    return int(v) if v is not None else 0


def _ratio(num, den) -> float:
    return round(num / den, 4) if den else 0.0


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None
