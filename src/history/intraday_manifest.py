"""P0-30 1분봉(1m) coverage/snapshot manifest — read-only 검증.

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §2.2·§2.4·§4.4·§5.1 / AMENDMENT A1 · 이슈 #75.

일봉 manifest(`daily_manifest.py`)의 패턴을 1분봉에 맞춰 확장한다:
- **as_of 경계(B)**: 거래소-로컬 일자가 as_of 초과인 봉(진행 중 거래일 등)은 공식 지문에서
  제외하고 bars_beyond_as_of 로 보고 → 진행 중 거래일은 OK 판정을 받을 수 없다.
- **세션 커버리지(A/§2.2)**: 허용 세션(PRE/REGULAR/POST) 별 행수 + REGULAR 창 커버리지.
  거래소 캘린더(`exchange_calendar`)로 휴장·조기폐장을 결측과 구별하고 조기폐장일 커버리지를
  정규 390 으로 과대계상하지 않는다(§2.4).
- **3.0년 게이트(§5.1)**: span<3.0년 종목은 `INCONCLUSIVE_DATA`(기준 완화 금지).
- **후행 경계 게이트**: 허용 세션의 마지막 거래일이 as_of 기준 마지막 NYSE 거래일보다
  이르면 `INCONCLUSIVE_DATA`. 실행 cutoff 선언만으로 실제 최신 구간 수집을 인증하지 않는다.
- **결정론 content hash(§4.4)**: (ts_utc, session, o,h,l,c,v) 정렬 스트리밍 SHA-256.
- **raw snapshot hash(§4.1)**: 선택적으로 RawSnapshotStore.manifest() 의 raw_content_sha256 부기.

★ read-only(mode=ro). Toss API 호출 없음. **전략 수익률·forward return·blind OOS 성과 미계산.**
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import yaml

from src.history import exchange_calendar as cal
from src.history.daily_manifest import read_p0_universe, span_years
from src.history.ingest_provenance import (
    BACKFILL_SOURCE,
    declared_only_provenance,
    parse_stopped_reason,
    verify_collection_provenance,
)
from src.history.oos_guard import TRACK_INTRADAY, blind_oos_window

INTERVAL = "1m"
MANIFEST_SCHEMA = "p0_us30_intraday_1m_manifest/v2"
DEFAULT_MIN_YEARS = 3.0
# REGULAR 세션 하루 커버리지 합격선(quality.py min_coverage_pct 기본 90% 와 정합).
REGULAR_COVERAGE_FLOOR = 0.90

# data_content_sha256 대상 = **포함된 데이터**의 유도 필드만(provenance/시각/제외행 카운트 제외).
# bars_beyond_as_of(제외된 행 수)는 포함 데이터가 아니므로 넣지 않는다 — 같은 포함 데이터면
# 진행 중 봉 존재 여부와 무관하게 동일 지문이어야 한다.
_CONTENT_FIELDS = (
    "symbol", "market", "interval", "earliest", "latest",
    "row_count", "content_sha256", "sources", "adjusted", "trading_days",
    "sessions",
)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def read_intraday_requirements(yaml_path: str) -> dict:
    """YAML 의 requirements.intraday_minute(min_years·sessions_required·blind_oos_months)."""
    with open(yaml_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    reqs = (doc.get("requirements") or {}).get("intraday_minute") or {}
    return {
        "interval": reqs.get("interval", "1m"),
        "min_years": float(reqs.get("min_years", DEFAULT_MIN_YEARS)),
        "sessions_required": list(reqs.get("sessions_required", ["PRE", "REGULAR", "POST"])),
        "blind_oos_months": reqs.get("blind_oos_months"),
    }


def intraday_symbol_fingerprint(con: sqlite3.Connection, symbol: str, *,
                                as_of: str, market: str = "US") -> dict:
    """정렬 (ts_utc, session, o,h,l,c,v) 스트리밍 SHA-256 + 범위/세션/커버리지 집계.

    as_of(YYYY-MM-DD) 초과 거래일 봉은 공식 지문에서 제외하고 bars_beyond_as_of 로 보고한다(B).
    REGULAR 커버리지는 거래소 캘린더 기대분수로 산정(조기폐장 과대계상 방지, §2.4).
    """
    h = hashlib.sha256()
    n = 0
    beyond = 0
    earliest = latest = None
    days: list[str] = []
    seen_days: set[str] = set()
    per_session: dict[str, int] = {}
    session_days: dict[str, set[str]] = {}
    session_bounds: dict[str, list[str]] = {}
    reg_minutes: dict[str, set[int]] = {}     # ex_day → REGULAR minute-of-day 집합
    unparseable_ts = 0                        # ts_exchange 형식 이상으로 분 파싱 실패한 REGULAR 봉

    cur = con.execute(
        "SELECT ts_utc, ts_exchange, session, open, high, low, close, volume "
        "FROM candle_history WHERE symbol=? AND interval=? ORDER BY ts_utc",
        (symbol, INTERVAL),
    )
    for r in cur:
        ex = str(r["ts_exchange"])
        ex_day = ex[:10]
        if ex_day > as_of:                    # ★as_of 경계: 진행 중/미래 거래일 제외(B)
            beyond += 1
            continue
        sess = r["session"]
        line = "%s|%s|%r|%r|%r|%r|%r\n" % (
            r["ts_utc"], sess, r["open"], r["high"], r["low"], r["close"], r["volume"])
        h.update(line.encode("utf-8"))
        n += 1
        per_session[sess] = per_session.get(sess, 0) + 1
        session_days.setdefault(sess, set()).add(ex_day)
        bounds = session_bounds.setdefault(sess, [r["ts_utc"], r["ts_utc"]])
        bounds[1] = r["ts_utc"]
        if earliest is None:
            earliest = r["ts_utc"]
        latest = r["ts_utc"]
        if ex_day not in seen_days:
            seen_days.add(ex_day)
            days.append(ex_day)
        if sess == "REGULAR":
            # ts_exchange "YYYY-MM-DDTHH:MM..." → 분 단위 minute-of-day.
            try:
                hh = int(ex[11:13]); mm = int(ex[14:16])
                reg_minutes.setdefault(ex_day, set()).add(hh * 60 + mm)
            except (ValueError, IndexError):
                unparseable_ts += 1        # 조용히 버리지 않고 보고(§감사)

    coverage = _regular_coverage(reg_minutes, unparseable_ts)
    return {
        "symbol": symbol,
        "interval": INTERVAL,
        "row_count": n,
        "earliest": earliest,
        "latest": latest,
        "earliest_trading_day": days[0] if days else None,
        "latest_trading_day": days[-1] if days else None,
        "trading_days": len(days),
        "bars_beyond_as_of": beyond,
        "sessions": dict(sorted(per_session.items())),
        "regular_coverage": coverage,
        "content_sha256": h.hexdigest(),
        "_days": days,     # 내부 계산용(gap) — build 에서 제거
        "_session_days": session_days,
        "_session_bounds": session_bounds,
    }


def _regular_coverage(reg_minutes: dict[str, set[int]], unparseable_ts: int = 0) -> dict:
    """REGULAR 세션 일별 커버리지 요약 — 거래소 캘린더 기대분수 대비(§2.4)."""
    full = 0
    low = 0
    low_days: list[str] = []
    early_close_days: list[str] = []
    out_of_range = 0
    ratios: list[float] = []
    for day in sorted(reg_minutes):
        observed = len(reg_minutes[day])
        try:
            d = date.fromisoformat(day)
            expected = cal.expected_regular_minutes(d)
            if cal.is_early_close(d):
                early_close_days.append(day)
        except cal.CalendarRangeError:
            out_of_range += 1
            expected = cal.REGULAR_MINUTES         # 폴백(구간 밖) — 정규 390
        if expected <= 0:
            continue                               # 휴장인데 데이터 존재 → coverage 대상 아님
        ratio = observed / expected
        ratios.append(min(ratio, 1.0))
        if ratio >= REGULAR_COVERAGE_FLOOR:
            full += 1
        else:
            low += 1
            low_days.append(day)
    return {
        "regular_days": len(reg_minutes),
        "full_regular_days": full,
        "low_coverage_days": low,
        "low_coverage_day_list": low_days[:20],    # 상위 일부만(리포트 부피 제한)
        "early_close_days_seen": len(early_close_days),
        "calendar_out_of_range_days": out_of_range,
        "mean_coverage": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "coverage_floor": REGULAR_COVERAGE_FLOOR,
        "unparseable_ts": unparseable_ts,      # ts_exchange 형식 이상 REGULAR 봉 수(0 이 정상)
    }


def _max_gap_trading_days(days: list[str]) -> dict:
    """연속 저장 거래일 사이의 **기대 거래일 결측**(휴장 제외) — 캘린더로 판정.

    구간이 캘린더 커버 범위 밖이면 달력일 gap 으로 폴백(calendar_based=False).
    """
    if len(days) < 2:
        return {"max_missing_trading_days": 0, "calendar_based": True}
    calendar_based = True
    worst = 0
    for a, b in zip(days, days[1:]):
        da, db = date.fromisoformat(a), date.fromisoformat(b)
        try:
            expected = cal.trading_days(da, db)          # 폐구간
            missing = max(0, len(expected) - 2)          # 양끝(a,b) 제외한 기대 거래일 수
        except cal.CalendarRangeError:
            calendar_based = False
            missing = max(0, (db - da).days - 1)
        worst = max(worst, missing)
    return {"max_missing_trading_days": worst, "calendar_based": calendar_based}


def _latest_expected_trading_day(as_of: str) -> str:
    """as_of 이하의 마지막 NYSE 거래일. 캘린더 artifact 밖이면 fail-closed."""
    d = date.fromisoformat(as_of)
    while not cal.is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


def _trailing_missing_trading_days(
        latest_allowed_trading_day: str | None,
        expected_latest_trading_day: str) -> int | None:
    """허용 세션 최신일 다음부터 기대 최신일까지 빠진 NYSE 거래일 수."""
    if latest_allowed_trading_day is None:
        return None
    latest = date.fromisoformat(latest_allowed_trading_day)
    expected = date.fromisoformat(expected_latest_trading_day)
    if latest >= expected:
        return 0
    return len(cal.trading_days(latest + timedelta(days=1), expected))


def intraday_verdict(*, span_yrs: float | None, min_years: float, row_count: int,
                     bars_beyond_as_of: int, sessions: dict, allowed_sessions: list,
                     stopped_reason: str | None,
                     trailing_missing_trading_days: int | None = None) -> dict:
    """1분봉 커버리지 판정(기준 완화 금지).

    OK 조건(전부 충족):
      · row_count>0, span_years>=min_years(=3.0)   ★기준 완화 금지
      · bars_beyond_as_of==0                        (진행 중/미래 거래일 없음, B)
      · 저장 세션이 허용 세션의 부분집합            (계약 밖 세션 유입 없음, A)
      · 허용 세션 최신일이 as_of 기준 마지막 거래일 도달
    하나라도 불충족이면 INCONCLUSIVE_DATA + 사유. depth_source 는 stopped_reason 에서 유도.
    """
    _FLOOR = {"nextbefore_null", "empty_page", "empty_budget_exhausted", "no_progress"}
    if stopped_reason == "max_pages":
        depth_source = "page_budget_capped"
    elif stopped_reason in _FLOOR:
        depth_source = "listing_full"
    elif stopped_reason:
        depth_source = "indeterminate"
    else:
        depth_source = "unknown"

    reasons: list[str] = []
    if row_count == 0 or span_yrs is None:
        reasons.append("no_data")
    elif span_yrs < min_years:
        reasons.append(f"span<{min_years}y")
    if bars_beyond_as_of > 0:
        reasons.append("bars_beyond_as_of")
    if trailing_missing_trading_days:
        reasons.append(f"trailing_gap:{trailing_missing_trading_days}_trading_days")
    disallowed = sorted(set(sessions) - set(allowed_sessions))
    if disallowed:
        reasons.append(f"disallowed_sessions:{disallowed}")
    status = "OK" if not reasons else "INCONCLUSIVE_DATA"
    return {
        "status": status,
        "meets_min_years": (span_yrs is not None and span_yrs >= min_years),
        "depth_source": depth_source,
        "bars_beyond_as_of": bars_beyond_as_of,
        "trailing_missing_trading_days": trailing_missing_trading_days,
        "disallowed_sessions": disallowed,
        "inconclusive_reasons": reasons,
    }


def _symbol_provenance(con: sqlite3.Connection, symbol: str) -> dict:
    sources = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT source FROM candle_history WHERE symbol=? AND interval=?",
        (symbol, INTERVAL)))
    adjusted = sorted(int(r[0]) for r in con.execute(
        "SELECT DISTINCT adjusted FROM candle_history WHERE symbol=? AND interval=?",
        (symbol, INTERVAL)))
    agg = con.execute(
        "SELECT COALESCE(SUM(rows_inserted),0) ins, COALESCE(SUM(rows_duplicate),0) dup, "
        "COALESCE(SUM(rows_conflict),0) conf FROM ingest_log WHERE symbol=? AND interval=?",
        (symbol, INTERVAL)).fetchone()
    last = con.execute(
        "SELECT run_id, note FROM ingest_log WHERE symbol=? AND interval=? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1", (symbol, INTERVAL)).fetchone()
    conflict_rows = con.execute(
        "SELECT COUNT(*) FROM import_conflict WHERE symbol=? AND interval=?",
        (symbol, INTERVAL)).fetchone()[0]
    cur = con.execute(
        "SELECT exhausted FROM backfill_cursor WHERE symbol=? AND interval=?",
        (symbol, INTERVAL)).fetchone()
    return {
        "sources": sources, "adjusted": adjusted,
        "inserted": int(agg["ins"]), "duplicate": int(agg["dup"]),
        "conflict_rows": int(conflict_rows), "conflict_stat": int(agg["conf"]),
        "run_id": last["run_id"] if last else None,
        "stopped_reason": parse_stopped_reason(last["note"] if last else None),
        "cursor_exhausted": (bool(cur["exhausted"]) if cur else None),
    }


def symbol_manifest(con: sqlite3.Connection, symbol: str, *, min_years: float,
                    as_of: str, allowed_sessions: list,
                    expected_latest_trading_day: str) -> dict:
    fp = intraday_symbol_fingerprint(con, symbol, as_of=as_of)
    prov = _symbol_provenance(con, symbol)
    allowed_days = sorted({
        day for sess in allowed_sessions for day in fp["_session_days"].get(sess, set())
    })
    allowed_bounds = [
        fp["_session_bounds"][sess]
        for sess in allowed_sessions
        if sess in fp["_session_bounds"]
    ]
    allowed_earliest = min((b[0] for b in allowed_bounds), default=None)
    allowed_latest = max((b[1] for b in allowed_bounds), default=None)
    allowed_earliest_day = allowed_days[0] if allowed_days else None
    allowed_latest_day = allowed_days[-1] if allowed_days else None
    allowed_rows = sum(fp["sessions"].get(sess, 0) for sess in allowed_sessions)
    yrs = span_years(allowed_earliest, allowed_latest)
    gap = _max_gap_trading_days(allowed_days)
    trailing_gap = _trailing_missing_trading_days(
        allowed_latest_day, expected_latest_trading_day)
    verdict = intraday_verdict(
        span_yrs=yrs, min_years=min_years, row_count=allowed_rows,
        bars_beyond_as_of=fp["bars_beyond_as_of"], sessions=fp["sessions"],
        allowed_sessions=allowed_sessions, stopped_reason=prov["stopped_reason"],
        trailing_missing_trading_days=trailing_gap)
    market_row = con.execute(
        "SELECT market FROM candle_history WHERE symbol=? AND interval=? LIMIT 1",
        (symbol, INTERVAL)).fetchone()
    entry = {k: v for k, v in fp.items() if not k.startswith("_")}
    entry.update({
        "market": (market_row[0] if market_row else None),
        "span_years": yrs,
        "allowed_row_count": allowed_rows,
        "allowed_earliest": allowed_earliest,
        "allowed_latest": allowed_latest,
        "allowed_earliest_trading_day": allowed_earliest_day,
        "allowed_latest_trading_day": allowed_latest_day,
        "allowed_trading_days": len(allowed_days),
        "expected_latest_trading_day": expected_latest_trading_day,
        "trailing_missing_trading_days": trailing_gap,
        "gap": gap,
        "sources": prov["sources"],
        "adjusted": prov["adjusted"],
        "inserted": prov["inserted"],
        "duplicate": prov["duplicate"],
        "conflict": prov["conflict_rows"],
        "error": (None if prov["stopped_reason"] in (
            None, "max_pages", "nextbefore_null", "empty_page",
            "empty_budget_exhausted", "no_progress") else prov["stopped_reason"]),
        "stopped_reason": prov["stopped_reason"],
        "cursor_exhausted": prov["cursor_exhausted"],
        "run_id": prov["run_id"],
        "verdict": verdict,
    })
    return entry


def content_digest(entries: list[dict]) -> str:
    canon = [{k: e[k] for k in _CONTENT_FIELDS} for e in
             sorted(entries, key=lambda x: x["symbol"])]
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_manifest(db_path: str, yaml_path: str, *, collection_params: dict,
                   collection_code_commit: str | None,
                   allowed_sessions: list[str] | None = None,
                   as_of: str | None = None,
                   raw_manifest: dict | None = None,
                   generated_at_utc: str | None = None,
                   collection_completed_through: str | None = None,
                   collection_run_id: str | None = None) -> dict:
    """P0-30 1분봉 coverage/snapshot manifest(read-only).

    as_of: 공식 포함 경계(YYYY-MM-DD). 미지정 시 YAML universe.as_of. 인자가 YAML 보다 늦으면
    거부(경계 완화 금지, fail-closed). allowed_sessions 미지정 시 YAML sessions_required.
    raw_manifest: RawSnapshotStore.manifest() 결과(선택) — raw_content_sha256 를 부기.

    collection_completed_through(YYYY-MM-DD): 실제 수집 완료-거래일 cutoff(이슈 #72). 지정 시
    collection 에 부기한다.

    collection_run_id(이슈 #79): 지정 시 ingest_log strict provenance 교차검증을 켠다(daily_manifest
    와 동일 규율 — 검증된 cutoff 가 권위, 선언값 일치·전 종목·동일 cutoff·as_of 경계 fail-closed).
    반환 manifest 의 `provenance_verification` 블록은 실행 provenance 이며 data_content_sha256
    대상이 아니다(데이터 지문과 분리).
    """
    uni = read_p0_universe(yaml_path)
    reqs = read_intraday_requirements(yaml_path)
    min_years = reqs["min_years"]
    allowed = list(allowed_sessions) if allowed_sessions is not None else reqs["sessions_required"]

    # ★fail-closed as_of 경계(daily_manifest 와 동일 규율).
    yaml_as_of = uni["as_of"]
    resolved = as_of if as_of is not None else yaml_as_of
    if not resolved:
        raise ValueError("as_of 경계 미지정 — fail-closed: --as-of 또는 YAML universe.as_of 필요")
    try:
        date.fromisoformat(resolved)
    except (ValueError, TypeError):
        raise ValueError(f"as_of 형식 오류(YYYY-MM-DD): {resolved!r}")
    if as_of is not None and yaml_as_of and as_of > yaml_as_of:
        raise ValueError(
            f"--as-of {as_of} 가 YAML 정본 as_of {yaml_as_of} 보다 늦음 — 거부(경계 완화 금지)")
    as_of = resolved
    # 선언 cutoff 형식(양 경로 공통). run_id 미지정(declared-only)에서만 cutoff<as_of fail-closed
    # (이슈 #72). run_id 지정 시엔 아래 strict 교차검증이 검증된 cutoff 로 as_of 경계를 판정한다(이슈 #79).
    if collection_completed_through is not None:
        try:
            date.fromisoformat(collection_completed_through)
        except (ValueError, TypeError):
            raise ValueError(
                f"collection_completed_through 형식 오류(YYYY-MM-DD): {collection_completed_through!r}")
        if collection_run_id is None and collection_completed_through < as_of:
            raise ValueError(
                f"수집 cutoff {collection_completed_through} 가 as_of {as_of} 보다 이름 — "
                f"(cutoff, as_of] 구간 미수집으로 OK 인증 불가(fail-closed).")

    con = _connect_ro(db_path)
    try:
        # ★이슈 #79 strict provenance 교차검증(run_id 지정 시). read-only — DB 무변경.
        if collection_run_id is not None:
            prov_verification = verify_collection_provenance(
                con, run_id=collection_run_id, interval=INTERVAL, source=BACKFILL_SOURCE,
                symbols=uni["symbols"],
                declared_completed_through=collection_completed_through, as_of=as_of)
        else:
            prov_verification = declared_only_provenance(
                declared_completed_through=collection_completed_through,
                interval=INTERVAL, source=BACKFILL_SOURCE)
        expected_latest_trading_day = _latest_expected_trading_day(as_of)
        entries = [symbol_manifest(
            con, s, min_years=min_years, as_of=as_of,
            allowed_sessions=allowed,
            expected_latest_trading_day=expected_latest_trading_day,
        ) for s in uni["symbols"]]
    finally:
        con.close()

    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e["verdict"]["status"]] = by_status.get(e["verdict"]["status"], 0) + 1
    earliests = [e["earliest"] for e in entries if e["earliest"]]
    latests = [e["latest"] for e in entries if e["latest"]]
    session_totals: dict[str, int] = {}
    for e in entries:
        for sess, cnt in e["sessions"].items():
            session_totals[sess] = session_totals.get(sess, 0) + cnt

    _oos_lo, _oos_hi = blind_oos_window(TRACK_INTRADAY)   # 단일 원천(oos_guard)

    manifest = {
        # v2: allowed-session span/gap과 as_of 후행 결측을 verdict 계약에 추가.
        "schema": MANIFEST_SCHEMA,
        "interval": INTERVAL,
        "universe": {
            "source": os.path.basename(yaml_path), "version": uni["yaml_version"],
            "as_of": uni["as_of"], "declared_count": uni["declared_count"],
            "market": uni["market"], "currency": uni["currency"],
            "valid": uni["valid"], "errors": uni["errors"],
        },
        "requested_as_of": as_of,
        "expected_latest_trading_day": expected_latest_trading_day,
        "min_years": min_years,
        "allowed_sessions": sorted(allowed),
        # blind OOS 경계는 oos_guard 를 단일 원천으로 인용(이중 정의 drift 방지). oos_guard 는
        # 자본 경로(order/risk/live)를 import 하지 않아 격리 계약과 충돌하지 않는다.
        "blind_oos": {"track": TRACK_INTRADAY,
                      "start": _oos_lo.isoformat(), "end": _oos_hi.isoformat(),
                      "months": reqs["blind_oos_months"]},
        "collection": {**collection_params, "code_commit": collection_code_commit,
                       "db_basename": os.path.basename(db_path),
                       "completed_through": collection_completed_through},
        # 실행 provenance(이슈 #79) — 데이터 내용 지문과 실행 provenance 지문을 분리한다.
        # v2 판정 강화에도 data_content_sha256 정의는 불변이다.
        "provenance_verification": prov_verification,
        "raw_snapshot": ({"raw_content_sha256": raw_manifest.get("raw_content_sha256"),
                          "content_file_count": raw_manifest.get("content_file_count"),
                          "schema": raw_manifest.get("schema")}
                         if raw_manifest else None),
        "summary": {
            "symbols_total": len(entries),
            "verdict_counts": by_status,
            "attempted": sum(1 for e in entries if e["run_id"] is not None),
            "with_rows": sum(1 for e in entries if e["row_count"] > 0),
            "total_rows": sum(e["row_count"] for e in entries),
            "bars_beyond_as_of_total": sum(e["bars_beyond_as_of"] for e in entries),
            "trailing_gap_symbol_count": sum(
                1 for e in entries if (e["trailing_missing_trading_days"] or 0) > 0),
            "session_row_totals": dict(sorted(session_totals.items())),
            "min_earliest": min(earliests) if earliests else None,
            "max_latest": max(latests) if latests else None,
            "meets_3y_count": sum(1 for e in entries if e["verdict"]["meets_min_years"]),
        },
        "symbols": entries,
        "data_content_sha256": content_digest(entries),
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return manifest
