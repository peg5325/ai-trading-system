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
from datetime import datetime, timezone

import yaml

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
                       interval: str = INTERVAL) -> dict:
    """정렬된 (ts_utc,open,high,low,close,volume) 스트리밍 SHA-256 + 범위/행수/일수.

    행이 0개면 row_count=0, content_sha256 은 빈 스트림 해시(고정값).
    """
    h = hashlib.sha256()
    n = 0
    earliest = latest = None
    days: set[str] = set()
    cur = con.execute(
        "SELECT ts_utc, ts_exchange, open, high, low, close, volume "
        "FROM candle_history WHERE symbol=? AND interval=? ORDER BY ts_utc",
        (symbol, interval),
    )
    for r in cur:
        line = "%s|%r|%r|%r|%r|%r\n" % (
            r["ts_utc"], r["open"], r["high"], r["low"], r["close"], r["volume"],
        )
        h.update(line.encode("utf-8"))
        n += 1
        if earliest is None:
            earliest = r["ts_utc"]
        latest = r["ts_utc"]
        days.add(str(r["ts_exchange"])[:10])
    return {
        "symbol": symbol,
        "interval": interval,
        "row_count": n,
        "earliest": earliest,
        "latest": latest,
        "trading_days": len(days),
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
        "stopped_reason": last["note"] if last else None,
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


def daily_verdict(*, span_yrs: float | None, min_years: float,
                  stopped_reason: str | None, row_count: int) -> dict:
    """일봉 커버리지 판정.

    - row 0 또는 span 미상 → INCONCLUSIVE_DATA.
    - span_years >= min_years → OK. depth_source 로 페이지예산 절단인지 상장이력 소진인지 구분.
    - span_years < min_years → INCONCLUSIVE_DATA (★기준 완화 금지). listing_limited 로
      상장/스핀오프 이력 제한 여부를 별도 표시(추측으로 adjusted/OK 판정하지 않는다).
    """
    # stopped_reason(=ingest_log.note) 해석:
    #  - 'max_pages'                        → 우리 13페이지 예산에서 절단(원천은 더 과거까지
    #                                          있음 → 최소 ~10년 확보). depth=page_budget_capped.
    #  - 'nextbefore_null'/'empty_page'     → 공급자 원천 바닥 도달 = 상장이력 전체. depth=listing_full.
    #  - 'empty_budget_exhausted'/'no_progress'/'error' → 원천 바닥인지 불명(gap-skip 예산 소진·
    #                                          stalled 커서). '상장이력 전체'로 단정하지 않는다 → indeterminate.
    # 이번 일봉 run 은 gap_skip_minutes=0 이라 empty_budget_exhausted 는 발생하지 않는다.
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
    # listing_limited = '상장/스핀오프 이력이 원천 바닥을 이룬다'가 확인된 경우만.
    listing_limited = reached_floor
    if row_count == 0 or span_yrs is None:
        status = "INCONCLUSIVE_DATA"
    elif span_yrs >= min_years:
        status = "OK"
    else:
        status = "INCONCLUSIVE_DATA"
    return {
        "status": status,
        "meets_min_years": (span_yrs is not None and span_yrs >= min_years),
        "depth_source": depth_source,
        "listing_limited": listing_limited,
    }


def symbol_manifest(con: sqlite3.Connection, symbol: str, *, min_years: float,
                    interval: str = INTERVAL) -> dict:
    fp = symbol_fingerprint(con, symbol, interval)
    prov = symbol_provenance(con, symbol, interval)
    yrs = span_years(fp["earliest"], fp["latest"])
    verdict = daily_verdict(span_yrs=yrs, min_years=min_years,
                            stopped_reason=prov["stopped_reason"],
                            row_count=fp["row_count"])
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
                   generated_at_utc: str | None = None) -> dict:
    """P0-30 일봉 manifest 생성(read-only). db_path 는 절대경로여도 basename 만 기록.

    generated_at_utc 를 주지 않으면 현재 UTC(테스트는 고정값 주입 가능). 이 필드는
    manifest_content_sha256 대상이 아니므로 hash 재현성에 영향 없다.
    """
    uni = read_p0_universe(yaml_path)
    min_years = uni["min_years"]
    # 계약 게이트: min_years 누락은 daily_verdict 에서 'span >= None' TypeError 를 유발한다.
    # 애매한 크래시 대신 명시적 계약 오류로 중단(기준 완화 금지 원칙과 일치).
    if min_years is None:
        raise ValueError(
            "P0 계약 위반: requirements.swing_daily.min_years 누락 — manifest 생성 중단")
    con = _connect_ro(db_path)
    try:
        entries = [symbol_manifest(con, s, min_years=min_years) for s in uni["symbols"]]
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
        "collection": {
            **collection_params,
            "code_commit": collection_code_commit,
            "db_basename": os.path.basename(db_path),
        },
        "summary": {
            "symbols_total": len(entries),
            "verdict_counts": by_status,
            "attempted": sum(1 for e in entries if e["run_id"] is not None),
            "with_rows": sum(1 for e in entries if e["row_count"] > 0),
            "total_rows": sum(e["row_count"] for e in entries),
            "min_earliest": min(earliests) if earliests else None,
            "max_latest": max(latests) if latests else None,
        },
        "symbols": entries,
        "manifest_content_sha256": content_digest(entries),
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).isoformat(),
    }
    return manifest


# ---------------------------------------------------------------------------
# 5) R2 split 연속성 점검 (배당/거래량 조정 데이터 품질 — 보수적 판정)
# ---------------------------------------------------------------------------
def invariant_snapshot(con: sqlite3.Connection, interval: str) -> dict:
    """한 interval 전체의 결정론 지문 — 실행 전후 무변경 증명용(read-only).

    per-symbol (symbol,row_count,content_sha256) 를 정렬·직렬화해 combined_sha256 로 축약.
    1d backfill 전/후 interval='1m' 스냅샷의 combined_sha256 가 같으면 1m 무변경.
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
        "symbol_count": len(symbols),
        "total_rows": total,
        "combined_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "per_symbol": per,
    }


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
