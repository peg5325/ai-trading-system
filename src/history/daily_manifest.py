"""P0-30 일봉(1d) 스냅샷 manifest — market_history.sqlite read-only 검증.

배경: 기존 `src/history/quality.py`(quality_report)는 **분봉(1m) 의미에 결합**돼 있다.
REG_WINDOW(정규장 390분) 기준으로 missing_minutes/coverage_pct/full_session_days 를
계산하므로, 하루 1개 봉인 일봉에는 coverage_pct≈0.26%(=1/390) 같은 무의미한 값이 나온다.
따라서 일봉 검증에는 부적합하다(그 사실은 tests/test_daily_manifest.py 로 증명).

이 모듈은 일봉 전용 최소 검증만 추가한다. **read-only**(mode=ro) — 원본 DB 를 절대
쓰지 않는다. Toss API 호출 없음. 전략 성과·수익률·forward return 은 계산하지 않는다.

manifest 원칙:
- 원시 캔들 전체를 넣지 않는다. 검증 가능한 범위·행 수·정렬 SHA-256·provenance 만.
- 결정론: 같은 DB 내용 → 같은 `manifest_content_sha256`(생성 시각/실행 id/커밋 등
  provenance 는 hash 대상에서 제외, 데이터 유도 필드만 정렬해 해시).
- 자격증명·토큰·로컬 절대 비밀 경로는 포함하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timezone

import yaml

from src.history.ingest_provenance import (
    BACKFILL_SOURCE,
    declared_only_provenance,
    parse_stopped_reason,
    verify_collection_provenance,
)

INTERVAL = "1d"

# manifest_content_sha256 대상(데이터 유도 필드만 — provenance/시각 제외 → 재현 가능).
_CONTENT_FIELDS = (
    "symbol", "market", "interval", "earliest", "latest",
    "row_count", "content_sha256", "sources", "adjusted", "trading_days",
)


def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------------------
# 1) P0 YAML 결정론적 읽기 + 계약 검증
# ---------------------------------------------------------------------------
def read_p0_universe(yaml_path: str) -> dict:
    """P0-30 유니버스를 YAML 에서 **프로그램으로** 읽고 계약을 검증한다.

    수동 복사 목록을 정본으로 쓰지 않는다. 심볼 순서는 YAML 기재 순서를 보존한다
    (결정론). count/중복/시장/통화 계약 위반은 valid=False + errors 로 드러낸다.
    """
    with open(yaml_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    entries = doc.get("symbols", []) or []
    symbols = [e["symbol"] for e in entries]
    seen: set[str] = set()
    duplicates = sorted({s for s in symbols if s in seen or seen.add(s)})
    reqs = (doc.get("requirements") or {}).get("swing_daily") or {}
    result = {
        "yaml_version": doc.get("version"),
        "as_of": doc.get("as_of"),
        "declared_count": doc.get("count"),
        "market": doc.get("market"),
        "currency": doc.get("currency"),
        "min_years": reqs.get("min_years"),
        "adjusted_requirement": reqs.get("adjusted"),
        "symbols": symbols,
        "actual_count": len(symbols),
        "unique_count": len(set(symbols)),
        "duplicates": duplicates,
    }
    errors: list[str] = []
    if result["declared_count"] != 30:
        errors.append(f"declared count != 30 ({result['declared_count']})")
    if result["actual_count"] != 30:
        errors.append(f"actual symbol count != 30 ({result['actual_count']})")
    if result["unique_count"] != result["actual_count"]:
        errors.append(f"duplicate symbols: {duplicates}")
    if result["market"] != "US":
        errors.append(f"market != US ({result['market']})")
    if result["currency"] != "USD":
        errors.append(f"currency != USD ({result['currency']})")
    if result["min_years"] is None:
        errors.append("requirements.swing_daily.min_years 누락")
    result["errors"] = errors
    result["valid"] = not errors
    return result


# ---------------------------------------------------------------------------
# 2) 심볼별 데이터 지문 + 수집 provenance (read-only 집계)
# ---------------------------------------------------------------------------
def symbol_fingerprint(con: sqlite3.Connection, symbol: str,
                       interval: str = INTERVAL, *, as_of: str | None = None) -> dict:
    """정렬된 (ts_utc,open,high,low,close,volume) 스트리밍 SHA-256 + 범위/행수/일수/gap.

    as_of(YYYY-MM-DD) 를 주면 거래소-로컬 일자가 as_of 초과인 봉(미완성 진행일 등)을
    공식 지문에서 제외하고 그 수를 bars_beyond_as_of 로 보고한다. as_of=None(예: 1m
    불변식)이면 전체를 그대로 해시한다.

    행이 0개면 row_count=0, content_sha256 은 빈 스트림 해시(고정값).
    """
    h = hashlib.sha256()
    n = 0
    earliest = latest = None
    days: list[str] = []          # 정렬 순서로 누적(gap 계산)
    seen_days: set[str] = set()
    beyond = 0                    # as_of 초과(미완성 진행일 등) 봉 수 — 공식 지문에서 제외
    cur = con.execute(
        "SELECT ts_utc, ts_exchange, open, high, low, close, volume "
        "FROM candle_history WHERE symbol=? AND interval=? ORDER BY ts_utc",
        (symbol, interval),
    )
    for r in cur:
        ex_day = str(r["ts_exchange"])[:10]
        # ★as_of 경계: 거래소-로컬 일자가 as_of 초과면 '미완성/계약 밖'으로 보고 제외.
        if as_of is not None and ex_day > as_of:
            beyond += 1
            continue
        line = "%s|%r|%r|%r|%r|%r\n" % (
            r["ts_utc"], r["open"], r["high"], r["low"], r["close"], r["volume"],
        )
        h.update(line.encode("utf-8"))
        n += 1
        if earliest is None:
            earliest = r["ts_utc"]
        latest = r["ts_utc"]
        if ex_day not in seen_days:
            seen_days.add(ex_day)
            days.append(ex_day)
    # 내부 최대 결측(연속 거래일 간 캘린더 일수 차) — 장기 hole 탐지.
    max_gap_days = 0
    for a, b in zip(days, days[1:]):
        gap = (date.fromisoformat(b) - date.fromisoformat(a)).days
        if gap > max_gap_days:
            max_gap_days = gap
    return {
        "symbol": symbol,
        "interval": interval,
        "row_count": n,
        "earliest": earliest,
        "latest": latest,
        "trading_days": len(days),
        "max_gap_days": max_gap_days,
        "bars_beyond_as_of": beyond,
        "content_sha256": h.hexdigest(),
    }


def symbol_provenance(con: sqlite3.Connection, symbol: str,
                      interval: str = INTERVAL) -> dict:
    """candle_history + ingest_log + import_conflict + backfill_cursor 에서
    source/adjusted/insert/duplicate/conflict/error/run_id/정지사유 집계."""
    sources = sorted(
        r[0] for r in con.execute(
            "SELECT DISTINCT source FROM candle_history WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
    )
    adjusted = sorted(
        int(r[0]) for r in con.execute(
            "SELECT DISTINCT adjusted FROM candle_history WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
    )
    agg = con.execute(
        "SELECT COALESCE(SUM(rows_inserted),0) AS ins, COALESCE(SUM(rows_duplicate),0) AS dup, "
        "COALESCE(SUM(rows_conflict),0) AS conf FROM ingest_log WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()
    # 최신 run 의 run_id / note(정지사유 또는 error) — provenance 대표값.
    last = con.execute(
        "SELECT run_id, note FROM ingest_log WHERE symbol=? AND interval=? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1", (symbol, interval),  # rowid: 동시각 안정 선택
    ).fetchone()
    conflict_rows = con.execute(
        "SELECT COUNT(*) FROM import_conflict WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()[0]
    cur = con.execute(
        "SELECT exhausted, oldest_ts_utc FROM backfill_cursor WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()
    return {
        "sources": sources,
        "adjusted": adjusted,
        "inserted": int(agg["ins"]),
        "duplicate": int(agg["dup"]),
        "conflict_rows": int(conflict_rows),
        "conflict_stat": int(agg["conf"]),
        "run_id": last["run_id"] if last else None,
        "stopped_reason": parse_stopped_reason(last["note"] if last else None),
        "cursor_exhausted": (bool(cur["exhausted"]) if cur else None),
    }


# ---------------------------------------------------------------------------
# 3) 일봉 품질 판정 (기준 완화 금지)
# ---------------------------------------------------------------------------
def span_years(earliest: str | None, latest: str | None) -> float | None:
    if not earliest or not latest:
        return None
    e = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
    latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return round((latest_dt - e).total_seconds() / (365.25 * 86400), 3)


# 거래일 밀도/내부결측 임계(보수적 — 정상 일봉은 여유롭게 통과).
DENSITY_FLOOR = 0.90            # trading_days >= 0.90 * span_years * 252
EXPECTED_DAYS_PER_YEAR = 252    # NYSE 대략 거래일수
MAX_GAP_DAYS_ALLOWED = 10       # 연속 거래일 간 캘린더 gap(휴일 long-weekend 여유 초과)


def daily_verdict(*, span_yrs: float | None, min_years: float,
                  stopped_reason: str | None, row_count: int,
                  trading_days: int = 0, bars_beyond_as_of: int = 0,
                  max_gap_days: int = 0) -> dict:
    """일봉 커버리지 판정 — 기간뿐 아니라 as_of 경계·거래일 밀도·내부 결측까지 검사.

    OK 조건(전부 충족):
      · row_count>0, span_years>=min_years  (★기준 완화 금지)
      · bars_beyond_as_of==0  (미완성/진행 중 봉이 공식 범위에 없음)
      · density(=trading_days/(span*252)) >= DENSITY_FLOOR  (표본 밀도)
      · max_gap_days <= MAX_GAP_DAYS_ALLOWED  (장기 내부결측 없음)
    하나라도 불충족이면 INCONCLUSIVE_DATA + 사유 목록. 추측으로 완화하지 않는다.
    """
    _FLOOR = {"nextbefore_null", "empty_page"}
    page_capped = stopped_reason == "max_pages"
    reached_floor = stopped_reason in _FLOOR
    if page_capped:
        depth_source = "page_budget_capped"
    elif reached_floor:
        depth_source = "listing_full"
    elif stopped_reason:
        depth_source = "indeterminate"
    else:
        depth_source = "unknown"
    listing_limited = reached_floor

    density = (round(trading_days / (span_yrs * EXPECTED_DAYS_PER_YEAR), 3)
               if span_yrs else None)
    reasons: list[str] = []
    if row_count == 0 or span_yrs is None:
        reasons.append("no_data")
    else:
        if span_yrs < min_years:
            reasons.append(f"span<{min_years}y")
        if density is not None and density < DENSITY_FLOOR:
            reasons.append(f"low_density<{DENSITY_FLOOR}")
        if max_gap_days > MAX_GAP_DAYS_ALLOWED:
            reasons.append(f"internal_gap>{MAX_GAP_DAYS_ALLOWED}d")
    if bars_beyond_as_of > 0:
        reasons.append("bars_beyond_as_of")  # 미완성/진행 중 봉 존재
    status = "OK" if not reasons else "INCONCLUSIVE_DATA"
    return {
        "status": status,
        "meets_min_years": (span_yrs is not None and span_yrs >= min_years),
        "depth_source": depth_source,
        "listing_limited": listing_limited,
        "density": density,
        "max_gap_days": max_gap_days,
        "bars_beyond_as_of": bars_beyond_as_of,
        "inconclusive_reasons": reasons,
    }


def symbol_manifest(con: sqlite3.Connection, symbol: str, *, min_years: float,
                    interval: str = INTERVAL, as_of: str | None = None) -> dict:
    fp = symbol_fingerprint(con, symbol, interval, as_of=as_of)
    prov = symbol_provenance(con, symbol, interval)
    yrs = span_years(fp["earliest"], fp["latest"])
    verdict = daily_verdict(span_yrs=yrs, min_years=min_years,
                            stopped_reason=prov["stopped_reason"],
                            row_count=fp["row_count"],
                            trading_days=fp["trading_days"],
                            bars_beyond_as_of=fp["bars_beyond_as_of"],
                            max_gap_days=fp["max_gap_days"])
    market_row = con.execute(
        "SELECT market FROM candle_history WHERE symbol=? AND interval=? LIMIT 1",
        (symbol, interval),
    ).fetchone()
    return {
        "symbol": symbol,
        "market": (market_row[0] if market_row else None),
        "interval": interval,
        "earliest": fp["earliest"],
        "latest": fp["latest"],
        "row_count": fp["row_count"],
        "trading_days": fp["trading_days"],
        "max_gap_days": fp["max_gap_days"],
        "bars_beyond_as_of": fp["bars_beyond_as_of"],
        "span_years": yrs,
        "content_sha256": fp["content_sha256"],
        "sources": prov["sources"],
        "adjusted": prov["adjusted"],
        "inserted": prov["inserted"],
        "duplicate": prov["duplicate"],
        "conflict": prov["conflict_rows"],
        "error": None if (prov["stopped_reason"] in (None, "max_pages", "nextbefore_null",
                                                     "empty_page", "empty_budget_exhausted",
                                                     "no_progress"))
        else prov["stopped_reason"],
        "stopped_reason": prov["stopped_reason"],
        "cursor_exhausted": prov["cursor_exhausted"],
        "run_id": prov["run_id"],
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 4) manifest 전체 + 결정론적 content hash
# ---------------------------------------------------------------------------
def content_digest(symbol_entries: list[dict]) -> str:
    """데이터 유도 필드만(정렬) 직렬화해 SHA-256. provenance/시각 제외 → 재현 가능.

    심볼 정렬 + sort_keys 로 입력 순서 불변. 같은 DB 내용이면 항상 같은 값.
    """
    canon = [
        {k: e[k] for k in _CONTENT_FIELDS}
        for e in sorted(symbol_entries, key=lambda x: x["symbol"])
    ]
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_manifest(db_path: str, yaml_path: str, *, collection_params: dict,
                   collection_code_commit: str | None,
                   generated_at_utc: str | None = None,
                   as_of: str | None = None,
                   collection_completed_through: str | None = None,
                   collection_run_id: str | None = None) -> dict:
    """P0-30 일봉 manifest 생성(read-only). db_path 는 절대경로여도 basename 만 기록.

    as_of(YYYY-MM-DD): 공식 포함 경계. 미지정 시 YAML universe.as_of 를 사용한다.
    거래소-로컬 일자가 as_of 초과인 봉(미완성 진행일)은 공식 지문/판정에서 제외된다.

    collection_completed_through(YYYY-MM-DD): 실제 수집 때 적용한 완료-거래일 cutoff(이슈 #72).
    지정 시 collection provenance 에 부기한다.

    collection_run_id(이슈 #79): 지정 시 **strict provenance 교차검증**을 켠다. ingest_log 에서
    이 run 의 실제 수집 cutoff 를 읽어(BACKFILL_SOURCE·interval 고정), 유니버스 전 종목·재시도
    로그가 동일 cutoff 인지, 선언 cutoff·as_of 와 일치하는지 검증한다(불일치/누락/malformed/
    conflicting 은 ProvenanceError 로 fail-closed). 이때 **검증된 cutoff** 가 권위를 가지며
    선언값(있으면)은 그와 일치해야 한다 — CLI 인자만 믿던 오선언 경로를 닫는다.
    미지정이면 기존 declared-only 방어만 유지된다: collection_completed_through 가 as_of 보다
    이르면 fail-closed(raise). provenance_status 는 DECLARED_UNVERIFIED/NONE 로 정직 표기(VERIFIED 아님).

    반환 manifest 는 `provenance_verification` 블록(실행 provenance)을 담는다. 이 블록은
    **data_content_sha256 대상이 아니다**(데이터 내용 지문과 실행 provenance 지문을 혼동하지 않음).

    generated_at_utc 를 주지 않으면 현재 UTC(테스트는 고정값 주입 가능). 이 필드는
    data_content_sha256 대상이 아니므로 hash 재현성에 영향 없다.
    """
    uni = read_p0_universe(yaml_path)
    min_years = uni["min_years"]
    # 계약 게이트: min_years 누락은 daily_verdict 에서 'span >= None' TypeError 를 유발한다.
    # 애매한 크래시 대신 명시적 계약 오류로 중단(기준 완화 금지 원칙과 일치).
    if min_years is None:
        raise ValueError(
            "P0 계약 위반: requirements.swing_daily.min_years 누락 — manifest 생성 중단")
    # ★fail-closed 경계: as_of 가 인자·YAML 모두 없으면 필터가 조용히 비활성(fail-open)돼
    #   미완성 진행일 봉이 다시 OK 로 새어들 수 있다. 명시적으로 거부한다.
    yaml_as_of = uni["as_of"]
    resolved_as_of = as_of if as_of is not None else yaml_as_of
    if not resolved_as_of:
        raise ValueError(
            "as_of 경계 미지정 — fail-closed: --as-of 인자 또는 YAML universe.as_of 필요")
    try:
        date.fromisoformat(resolved_as_of)
    except (ValueError, TypeError):
        raise ValueError(f"as_of 형식 오류(YYYY-MM-DD 필요): {resolved_as_of!r}")
    # 명시 인자가 YAML 정본보다 늦으면 거부 — 계약 경계를 넘겨 미완성/진행 중 봉을
    # 다시 포함시키는 우회를 차단(인자는 더 보수적(이른) 경계로만 허용).
    if as_of is not None and yaml_as_of and as_of > yaml_as_of:
        raise ValueError(
            f"--as-of {as_of} 가 YAML 정본 as_of {yaml_as_of} 보다 늦음 — 거부(경계 완화 금지)")
    as_of = resolved_as_of
    # 선언 cutoff 형식 검증(양 경로 공통). run_id 미지정(declared-only) 경로에서는 이 값이 유일한
    # 방어라 cutoff<as_of 를 여기서 fail-closed 한다(이슈 #72). run_id 지정 시엔 아래 strict
    # 교차검증이 검증된 cutoff 로 as_of 경계를 판정하므로 여기서 조기 raise 하지 않는다(이슈 #79).
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
        entries = [symbol_manifest(con, s, min_years=min_years, as_of=as_of)
                   for s in uni["symbols"]]
    finally:
        con.close()

    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e["verdict"]["status"]] = by_status.get(e["verdict"]["status"], 0) + 1
    earliests = [e["earliest"] for e in entries if e["earliest"]]
    latests = [e["latest"] for e in entries if e["latest"]]

    manifest = {
        "schema": "p0_us30_daily_10y_manifest/v1",
        "interval": INTERVAL,
        "universe": {
            "source": os.path.basename(yaml_path),
            "version": uni["yaml_version"],
            "as_of": uni["as_of"],
            "declared_count": uni["declared_count"],
            "market": uni["market"],
            "currency": uni["currency"],
            "valid": uni["valid"],
            "errors": uni["errors"],
        },
        "min_years": min_years,
        "as_of": as_of,  # 공식 포함 경계(이 일자 이하 거래일만 집계)
        "collection": {
            **collection_params,
            "code_commit": collection_code_commit,
            "db_basename": os.path.basename(db_path),
            "completed_through": collection_completed_through,
        },
        # 실행 provenance(이슈 #79) — schema v1 에 **추가된 필드**(하위호환: 기존 키·값 무변경,
        # data_content_sha256 불변). 데이터 내용 지문과 실행 provenance 지문을 분리한다.
        "provenance_verification": prov_verification,
        "summary": {
            "symbols_total": len(entries),
            "verdict_counts": by_status,
            "attempted": sum(1 for e in entries if e["run_id"] is not None),
            "with_rows": sum(1 for e in entries if e["row_count"] > 0),
            "total_rows": sum(e["row_count"] for e in entries),
            "bars_beyond_as_of_total": sum(e["bars_beyond_as_of"] for e in entries),
            "min_earliest": min(earliests) if earliests else None,
            "max_latest": max(latests) if latests else None,
        },
        "symbols": entries,
        # data_content_sha256: symbol 별 데이터 유도 필드(_CONTENT_FIELDS)만 정렬 해시.
        # manifest 전체가 아니라 '포함된 데이터'의 결정론 지문(provenance/시각 제외).
        "data_content_sha256": content_digest(entries),
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return manifest


# ---------------------------------------------------------------------------
# 5) R2 split 연속성 점검 (배당/거래량 조정 데이터 품질 — 보수적 판정)
# ---------------------------------------------------------------------------
def invariant_snapshot(con: sqlite3.Connection, interval: str) -> dict:
    """한 interval 전체의 **OHLCV content** 결정론 지문 — 실행 전후 무변경 증명용(read-only).

    per-symbol (symbol,row_count,content_sha256) 를 정렬·직렬화해 combined_sha256 로 축약.
    content_sha256 은 (ts_utc,o,h,l,c,v) 만 해시한다(=OHLCV content). ts_exchange/session/
    currency/adjusted/source 까지 포함한 '모든 의미 데이터 필드' 대조는 full_field_invariant 참조.
    """
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM candle_history WHERE interval=? ORDER BY symbol",
        (interval,))]
    per = []
    total = 0
    for s in symbols:
        fp = symbol_fingerprint(con, s, interval)
        per.append({"symbol": s, "row_count": fp["row_count"],
                    "content_sha256": fp["content_sha256"]})
        total += fp["row_count"]
    blob = json.dumps(per, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "interval": interval,
        "scope": "ohlcv_content",
        "symbol_count": len(symbols),
        "total_rows": total,
        "combined_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "per_symbol": per,
    }


def full_field_invariant(con: sqlite3.Connection, interval: str) -> dict:
    """한 interval 의 **모든 의미 데이터 필드** 결정론 지문(read-only).

    OHLCV 뿐 아니라 symbol/market/interval/ts_utc/ts_exchange/session/currency/adjusted/
    source 까지 해시한다 → '의미 데이터 필드까지 완전 동일' 을 증명(backup↔현재 대조).
    ★ 감사 타임스탬프 created_at/updated_at 은 의도적으로 제외한다(행 재기록 시각은
      데이터 내용이 아니며 실행마다 달라질 수 있으므로 '무변경' 판정 대상이 아님).
    """
    h = hashlib.sha256()
    n = 0
    for r in con.execute(
        "SELECT symbol,market,interval,ts_utc,ts_exchange,session,open,high,low,close,"
        "volume,currency,adjusted,source FROM candle_history WHERE interval=? "
        "ORDER BY symbol, ts_utc", (interval,)):
        h.update(("|".join(str(r[k]) for k in r.keys()) + "\n").encode("utf-8"))
        n += 1
    return {"interval": interval, "scope": "all_semantic_fields_excl_audit_ts",
            "total_rows": n, "combined_sha256": h.hexdigest()}


def split_continuity_check(con: sqlite3.Connection, symbol: str, split_date: str,
                           ratio: float, interval: str = INTERVAL) -> dict:
    """문서화된 split 일자 경계에서 close/volume 불연속을 측정(read-only).

    - split_date 직전 마지막 봉과 직후 첫 봉을 잡아 close_ratio, volume_ratio 계산.
    - split-adjusted 계열이면 경계에서 불연속이 없어야 한다(close_ratio≈1).
    - volume 도 조정되면 volume_ratio≈1, 미조정이면 ≈ratio 로 튄다.
    - 일봉 변동 노이즈가 크므로 ratio 가 작으면(<5) 거래량 판정은 INCONCLUSIVE.
    추측 금지: 애매하면 'inconclusive' 를 반환한다.
    """
    before = con.execute(
        "SELECT ts_exchange, close, volume FROM candle_history "
        "WHERE symbol=? AND interval=? AND substr(ts_exchange,1,10) < ? "
        "ORDER BY ts_exchange DESC LIMIT 1", (symbol, interval, split_date),
    ).fetchone()
    after = con.execute(
        "SELECT ts_exchange, close, volume FROM candle_history "
        "WHERE symbol=? AND interval=? AND substr(ts_exchange,1,10) >= ? "
        "ORDER BY ts_exchange ASC LIMIT 1", (symbol, interval, split_date),
    ).fetchone()
    out = {
        "symbol": symbol, "split_date": split_date, "ratio": ratio,
        "before": (dict(before) if before else None),
        "after": (dict(after) if after else None),
        "close_ratio": None, "volume_ratio": None,
        "price_adjusted": "inconclusive", "volume_adjusted": "inconclusive",
    }
    if not before or not after or not after["close"]:
        out["note"] = "split 경계 봉 부족(범위 밖) — inconclusive"
        return out
    close_ratio = before["close"] / after["close"]
    out["close_ratio"] = round(close_ratio, 4)
    # 가격 조정 판정: 경계 close 비율이 1 근처면 조정됨, ratio 근처면 미조정.
    if abs(close_ratio - 1.0) < 0.2:
        out["price_adjusted"] = "adjusted"
    elif abs(close_ratio - ratio) < 0.2 * ratio:
        out["price_adjusted"] = "unadjusted"
    if before["volume"] and after["volume"]:
        vol_ratio = after["volume"] / before["volume"]
        out["volume_ratio"] = round(vol_ratio, 4)
        # ★추측 금지: 거래량은 일간 노이즈가 커서 경계비율≈1 이라고 '조정됨'을 단정할 수
        #   없다(정상 거래량 변동으로도 1 근처가 된다). 경계비율이 split ratio 에 가까울
        #   때만(큰 비율 ≥5) 'unadjusted' 로 단정하고, 그 외는 모두 'inconclusive'.
        if ratio >= 5 and abs(vol_ratio - ratio) < 0.4 * ratio:
            out["volume_adjusted"] = "unadjusted"
        else:
            out["volume_adjusted"] = "inconclusive"
    return out
