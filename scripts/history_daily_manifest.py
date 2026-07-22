"""P0-30 일봉(1d) 스냅샷 manifest + 리포트 생성 — market_history.sqlite read-only.

주문/Toss API 호출 없음. **수익률·forward return·신호 성능·blind OOS 미계산/미열람.**
데이터 품질 검사(R1/R2)도 수익률을 계산하지 않는다. DB 는 mode=ro 로만 연다.

사용:
  ./.venv/bin/python scripts/history_daily_manifest.py \
      --db data/market_history.sqlite --yaml config/universe_p0_us30.yaml \
      --out docs/research/P0_US30_DAILY_10Y_MANIFEST.json \
      --report docs/research/P0_US30_DAILY_10Y_REPORT.md \
      --code-commit 8a58fdc --as-of 2026-07-21 \
      --params-json '{"max_pages":13,"page_size":200,"spacing":0.5,"resume":true,"gap_skip_minutes":0,"interval":"1d"}'
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.daily_manifest import (  # noqa: E402
    build_manifest,
    full_field_invariant,
    invariant_snapshot,
    split_continuity_check,
)

# 계약/YAML note 에 명시된 split 이력(정보). '검증' = PROBE_REPORT 에서 실증된 것.
KNOWN_SPLITS = [
    {"symbol": "AAPL", "date": "2020-08-31", "ratio": 4, "verified": True},
    {"symbol": "NVDA", "date": "2024-06-10", "ratio": 10, "verified": True},
    {"symbol": "AMZN", "date": "2022-06-06", "ratio": 20, "verified": True},
    {"symbol": "TSLA", "date": "2022-08-25", "ratio": 3, "verified": True},
    {"symbol": "AVGO", "date": "2024-07-15", "ratio": 10, "verified": False},
    {"symbol": "GOOGL", "date": "2022-07-18", "ratio": 20, "verified": False},
]


def _connect_ro(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def r2_split_checks(con):
    return [split_continuity_check(con, s["symbol"], s["date"], s["ratio"]) | {
        "verified_in_probe": s["verified"]} for s in KNOWN_SPLITS]


def r1_dividend_check(con, symbol="XLU"):
    """R1: 고배당 XLU 배당(총수익) 조정 여부 — OHLCV 만으로 독립 검증 불가 → INCONCLUSIVE.

    ★수익률(종가 변화율 등)을 계산하지 않는다. adjusted 플래그(source 주장)만 보고하고
    추측으로 adjusted 판정하지 않는다(계약: 불명확하면 INCONCLUSIVE)."""
    adj = sorted(int(r[0]) for r in con.execute(
        "SELECT DISTINCT adjusted FROM candle_history WHERE symbol=? AND interval='1d'",
        (symbol,)))
    return {
        "symbol": symbol,
        "adjusted_flag": adj,
        "dividend_adjustment_verdict": "INCONCLUSIVE",
        "note": ("배당(총수익) 조정은 캔들 OHLCV 만으로 독립 검증 불가(외부 배당/총수익 기준 "
                 "필요). adjusted 플래그는 source(toss_backfill) 주장이며 PROBE_REPORT 는 "
                 "split 조정만 실증. 수익률 미계산·추측 금지 → INCONCLUSIVE."),
    }


def render_report_md(manifest, r1, r2, inv_post, ff_post, prov):
    m = manifest
    lines = []
    a = lines.append
    a("# P0-US30 일봉(1d) 10년 Bulk Backfill 리포트")
    a("")
    a(f"- schema: `{m['schema']}`  ·  interval: **{m['interval']}**  ·  "
      f"**as_of(공식 경계): {m.get('as_of')}**")
    a(f"- 유니버스 정본: `{m['universe']['source']}` v{m['universe']['version']} "
      f"(as_of {m['universe']['as_of']}, count={m['universe']['declared_count']}, "
      f"{m['universe']['market']}/{m['universe']['currency']}, valid={m['universe']['valid']})")
    a(f"- 수집 코드 커밋(merged path): `{m['collection'].get('code_commit')}`  ·  "
      f"DB: `{m['collection']['db_basename']}`")
    a(f"- 수집 파라미터: max_pages={m['collection'].get('max_pages')}, "
      f"page_size={m['collection'].get('page_size')}, spacing={m['collection'].get('spacing')}s, "
      f"resume={m['collection'].get('resume')}, gap_skip_minutes={m['collection'].get('gap_skip_minutes')}")
    a(f"- **data_content_sha256**(포함 데이터 결정론 지문): `{m['data_content_sha256']}`")
    a(f"- generated_at_utc: {m['generated_at_utc']} (hash 비대상)")
    a("")
    s = m["summary"]
    a("## 요약")
    a(f"- 종목 {s['symbols_total']} / attempted {s['attempted']} / with_rows {s['with_rows']}")
    a(f"- verdict: {json.dumps(s['verdict_counts'], ensure_ascii=False)}")
    a(f"- 공식 총 일봉 행수(≤as_of): {s['total_rows']:,}  ·  범위 "
      f"{(s['min_earliest'] or '')[:10]} ~ {(s['max_latest'] or '')[:10]}")
    a(f"- as_of 초과(미완성) 봉: {s['bars_beyond_as_of_total']} (0 이어야 정상)")
    if prov.get("backfill"):
        bf = prov["backfill"]
        a(f"- 수집 실행: 총 API 데이터페이지 {bf['pages']}, inserted {bf['inserted']:,}, "
          f"duplicate {bf['duplicate']}, conflict {bf['conflict']}, errors {bf['errors']}")
    a("")
    if prov.get("remediation"):
        rm = prov["remediation"]
        a("## as_of 경계 교정 (감사 가능)")
        a(f"- 수집분에는 미완성 진행일 **{rm['date']}** 일봉 {rm['removed_rows']}개(30종목)가 "
          f"포함돼 있었다(정규장 미완료·거래량 극소).")
        a(f"- `{rm['action']}` 로 제거 → 1d {rm['rows_1d_after']:,}행, integrity `{rm['integrity_after']}`. "
          f"1m 무변경. 감사 로그 `{rm['audit_basename']}`(reports/, gitignore).")
        a(f"- 정책: 수집 CLI 에는 완료-거래일 컷오프가 없으므로 **수집 후 as_of 초과 봉 제거**가 "
          f"표준 절차. 같은 날짜 partial→완성 갱신은 store.py first-write-wins(동일 source·adjusted "
          f"유지) 때문에 in-place 로 안 되며 **삭제 후 재수집**이 필요(별도 정책).")
        a("")
    a("## 저장소/백업 durability")
    a("- ⚠ canonical DB·백업은 **로컬 디스크에만** 존재(GitHub 미저장, gitignore). "
      "여기서 durable=로컬 디스크 기준. API 재소모 방지를 위해 외부 저장소 복제 권장.")
    for b in prov.get("backups", []):
        a(f"- 백업 `{b['path']}` — {b['size_bytes']:,}B · sha256 `{b['sha256']}`")
    a("")
    a("## 1m 무변경 불변식 (자본/실험 안전)")
    a(f"- 실행 후 1m OHLCV-content 스냅샷: symbols={inv_post['symbol_count']}, "
      f"rows={inv_post['total_rows']:,}, combined_sha256=`{inv_post['combined_sha256']}`")
    if prov.get("baseline_1m_ohlcv_sha"):
        held = prov["baseline_1m_ohlcv_sha"] == inv_post["combined_sha256"]
        a(f"- 실행 전 baseline(OHLCV-content) `{prov['baseline_1m_ohlcv_sha']}` → "
          f"**HELD: {held}**")
    if prov.get("baseline_1m_full_sha"):
        held_ff = prov["baseline_1m_full_sha"] == ff_post["combined_sha256"]
        a(f"- **모든 의미 데이터 필드**(ts_exchange/session/currency/adjusted/source 포함, "
          f"감사 ts created_at/updated_at 제외) 대조: 현재 `{ff_post['combined_sha256']}` vs "
          f"pre-1d 백업 `{prov['baseline_1m_full_sha']}` → **HELD: {held_ff}** "
          f"(rows={ff_post['total_rows']:,})")
    a("")
    a("## 종목별 스냅샷 (≤as_of)")
    a("| symbol | rows | days | earliest | latest | span_y | density | gap_d | verdict | depth | listing | sha256(12) |")
    a("|--------|-----:|-----:|----------|--------|-------:|--------:|------:|---------|-------|:-------:|------------|")
    for e in m["symbols"]:
        v = e["verdict"]
        a(f"| {e['symbol']} | {e['row_count']} | {e['trading_days']} | "
          f"{(e['earliest'] or '')[:10]} | {(e['latest'] or '')[:10]} | "
          f"{e['span_years']} | {v.get('density')} | {e['max_gap_days']} | {v['status']} | "
          f"{v['depth_source']} | {'Y' if v['listing_limited'] else ''} | `{e['content_sha256'][:12]}` |")
    a("")
    a("> verdict OK = span≥min_years AND as_of 초과봉 0 AND density≥0.90 AND 내부gap≤10d. "
      "하나라도 불충족 시 INCONCLUSIVE_DATA(사유는 manifest.verdict.inconclusive_reasons).")
    a("")
    a("### 상장/스핀오프 이력 제한(별도 표시)")
    for e in m["symbols"]:
        if e["verdict"]["listing_limited"]:
            a(f"- **{e['symbol']}**: earliest {(e['earliest'] or '')[:10]} "
              f"(≈{e['span_years']}y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥{m['min_years']}년 요건 충족.")
    a("")
    a("## R1 배당 조정 점검 (XLU)")
    a(f"- adjusted 플래그(source 주장): {r1['adjusted_flag']}")
    a(f"- **판정: {r1['dividend_adjustment_verdict']}** — {r1['note']}")
    a("")
    a("## R2 split 거래량/가격 조정 점검")
    a("| symbol | split | ratio | close_ratio | price | volume_ratio | volume | probe검증 |")
    a("|--------|-------|------:|------------:|-------|-------------:|--------|:--------:|")
    for c in r2:
        a(f"| {c['symbol']} | {c['split_date']} | {c['ratio']} | {c['close_ratio']} | "
          f"{c['price_adjusted']} | {c['volume_ratio']} | {c['volume_adjusted']} | "
          f"{'Y' if c['verified_in_probe'] else '정보'} |")
    a("")
    a("> price=adjusted 는 split 경계에서 가격 불연속이 없음(수정주가). volume 판정은 큰 비율"
      "(≥5)에서만 신뢰 가능하며 작은 split·경계비율≈1 은 노이즈로 INCONCLUSIVE(추측 금지).")
    a("")
    a("## 안전 재확인")
    a("- 전략 수익률·forward return·신호 성능·blind OOS 성과: **미계산/미열람**(R1/R2 도 수익률 미계산).")
    a("- 주문/Paper 세션/전략·config·가중치 경로: **무변경**. DB 는 canonical 절대경로에만 기록.")
    a("- interval `1d` 고정 · 1m gap-skip 미사용 · resume 활성 · 단일 프로세스.")
    return "\n".join(lines) + "\n"


def main(argv):
    ap = argparse.ArgumentParser(description="P0-30 일봉 manifest/report (read-only)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--out", required=True, help="manifest JSON 출력 경로")
    ap.add_argument("--report", help="리포트 MD 출력 경로")
    ap.add_argument("--code-commit", default=None)
    ap.add_argument("--as-of", default=None, help="공식 포함 경계(YYYY-MM-DD). 미지정 시 YAML as_of")
    ap.add_argument("--params-json", default="{}")
    ap.add_argument("--baseline-1m-sha", default=None, help="pre-1d OHLCV-content combined sha")
    ap.add_argument("--baseline-1m-full-sha", default=None, help="pre-1d 전체 저장 필드 sha")
    ap.add_argument("--provenance-json", default=None,
                    help="백업/교정 provenance JSON 파일(선택)")
    ap.add_argument("--backfill-json", default=None, help="실행 로그 JSON(리포트 provenance)")
    ap.add_argument("--generated-at", default=None)
    args = ap.parse_args(argv)

    params = json.loads(args.params_json)
    manifest = build_manifest(args.db, args.yaml, collection_params=params,
                              collection_code_commit=args.code_commit,
                              generated_at_utc=args.generated_at, as_of=args.as_of)
    # P0 계약 게이트: count=30/중복/시장/통화 위반이면 산출물을 쓰지 않는다.
    if not manifest["universe"]["valid"]:
        print(f"[error] P0 유니버스 계약 위반 — 중단: {manifest['universe']['errors']}",
              file=sys.stderr)
        return 2

    con = _connect_ro(args.db)
    try:
        r1 = r1_dividend_check(con)
        r2 = r2_split_checks(con)
        inv_post = invariant_snapshot(con, "1m")
        ff_post = full_field_invariant(con, "1m")
    finally:
        con.close()

    # provenance 조립(백업/교정/baseline) — content hash 대상 아님.
    prov = {}
    if args.provenance_json and os.path.exists(args.provenance_json):
        prov = json.load(open(args.provenance_json, encoding="utf-8"))
    prov["baseline_1m_ohlcv_sha"] = args.baseline_1m_sha
    prov["baseline_1m_full_sha"] = args.baseline_1m_full_sha
    if args.backfill_json and os.path.exists(args.backfill_json):
        bj = json.load(open(args.backfill_json, encoding="utf-8"))
        prov["backfill"] = {
            "pages": sum(r["pages_fetched"] for r in bj["results"]),
            "inserted": sum(r["inserted"] for r in bj["results"]),
            "duplicate": sum(r["duplicate"] for r in bj["results"]),
            "conflict": sum(r["conflict"] for r in bj["results"]),
            "errors": [r["symbol"] for r in bj["results"] if r["error"]],
        }

    manifest["data_quality_checks"] = {"R1_dividend_xlu": r1, "R2_split_continuity": r2}
    manifest["invariant_1m"] = {
        "ohlcv_content": {k: inv_post[k] for k in
                          ("symbol_count", "total_rows", "combined_sha256")},
        "ohlcv_baseline_sha256": args.baseline_1m_sha,
        "ohlcv_held": (args.baseline_1m_sha == inv_post["combined_sha256"]
                       if args.baseline_1m_sha else None),
        "full_field": {k: ff_post[k] for k in ("total_rows", "combined_sha256")},
        "full_field_baseline_sha256": args.baseline_1m_full_sha,
        "full_field_held": (args.baseline_1m_full_sha == ff_post["combined_sha256"]
                            if args.baseline_1m_full_sha else None),
    }
    manifest["provenance"] = {k: prov[k] for k in prov
                              if k in ("backups", "remediation", "backfill")}

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"[manifest] wrote {args.out}  data_content_sha256={manifest['data_content_sha256']}")

    if args.report:
        md = render_report_md(manifest, r1, r2, inv_post, ff_post, prov)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[report] wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
