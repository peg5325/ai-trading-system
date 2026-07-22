"""P0-30 일봉(1d) 스냅샷 manifest + 리포트 생성 — market_history.sqlite read-only.

주문/Toss API 호출 없음. 전략 성과·수익률·forward return 미계산. DB 는 mode=ro 로만 연다.

사용:
  ./.venv/bin/python scripts/history_daily_manifest.py \
      --db data/market_history.sqlite --yaml config/universe_p0_us30.yaml \
      --out docs/research/P0_US30_DAILY_10Y_MANIFEST.json \
      --report docs/research/P0_US30_DAILY_10Y_REPORT.md \
      --code-commit 8a58fdc \
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
    """R1: 고배당 XLU 배당(총수익) 조정 여부 — OHLCV 만으로는 독립 검증 불가 → INCONCLUSIVE.

    adjusted 플래그(source 주장)와 최대 단일일 낙폭(정보)만 보고한다. 추측으로 adjusted
    판정하지 않는다(계약: 불명확하면 INCONCLUSIVE)."""
    row = con.execute(
        "SELECT DISTINCT adjusted FROM candle_history WHERE symbol=? AND interval='1d'",
        (symbol,)).fetchall()
    adj = sorted(int(r[0]) for r in row)
    closes = [r[0] for r in con.execute(
        "SELECT close FROM candle_history WHERE symbol=? AND interval='1d' ORDER BY ts_utc",
        (symbol,))]
    worst = None
    for a, b in zip(closes, closes[1:]):
        if a:
            chg = (b - a) / a
            if worst is None or chg < worst:
                worst = chg
    return {
        "symbol": symbol,
        "adjusted_flag": adj,
        "max_single_day_drop_pct": (round(worst * 100, 2) if worst is not None else None),
        "dividend_adjustment_verdict": "INCONCLUSIVE",
        "note": ("배당(총수익) 조정은 캔들 OHLCV 만으로 독립 검증 불가. adjusted 플래그는 "
                 "source(toss_backfill) 주장이며 PROBE_REPORT 는 split 조정만 실증. "
                 "추측 금지 → INCONCLUSIVE."),
    }


def render_report_md(manifest, r1, r2, inv_post, baseline_sha, backfill_json):
    m = manifest
    lines = []
    a = lines.append
    a("# P0-US30 일봉(1d) 10년 Bulk Backfill 리포트")
    a("")
    a(f"- schema: `{m['schema']}`  ·  interval: **{m['interval']}**")
    a(f"- 유니버스 정본: `{m['universe']['source']}` v{m['universe']['version']} "
      f"(as_of {m['universe']['as_of']}, count={m['universe']['declared_count']}, "
      f"{m['universe']['market']}/{m['universe']['currency']}, valid={m['universe']['valid']})")
    a(f"- 수집 코드 커밋(merged path): `{m['collection'].get('code_commit')}`  ·  "
      f"DB: `{m['collection']['db_basename']}`")
    a(f"- 수집 파라미터: max_pages={m['collection'].get('max_pages')}, "
      f"page_size={m['collection'].get('page_size')}, spacing={m['collection'].get('spacing')}s, "
      f"resume={m['collection'].get('resume')}, gap_skip_minutes={m['collection'].get('gap_skip_minutes')}")
    a(f"- **manifest_content_sha256**: `{m['manifest_content_sha256']}`")
    a(f"- generated_at_utc: {m['generated_at_utc']} (hash 비대상)")
    a("")
    s = m["summary"]
    a("## 요약")
    a(f"- 종목 {s['symbols_total']} / attempted {s['attempted']} / with_rows {s['with_rows']}")
    a(f"- verdict: {json.dumps(s['verdict_counts'], ensure_ascii=False)}")
    a(f"- 총 일봉 행수: {s['total_rows']:,}  ·  범위 {s['min_earliest']} ~ {s['max_latest']}")
    if backfill_json:
        tot_pages = sum(r["pages_fetched"] for r in backfill_json["results"])
        tot_ins = sum(r["inserted"] for r in backfill_json["results"])
        tot_dup = sum(r["duplicate"] for r in backfill_json["results"])
        tot_conf = sum(r["conflict"] for r in backfill_json["results"])
        errs = [r["symbol"] for r in backfill_json["results"] if r["error"]]
        a(f"- 실행: 총 API 데이터페이지 {tot_pages}, inserted {tot_ins:,}, "
          f"duplicate {tot_dup}, conflict {tot_conf}, errors {len(errs)} {errs}")
    a("")
    a("## 1m 무변경 불변식 (자본/실험 안전)")
    a(f"- 실행 후 1m 스냅샷: symbols={inv_post['symbol_count']}, rows={inv_post['total_rows']:,}, "
      f"combined_sha256=`{inv_post['combined_sha256']}`")
    if baseline_sha:
        held = baseline_sha == inv_post["combined_sha256"]
        a(f"- 실행 전 baseline combined_sha256=`{baseline_sha}`")
        a(f"- **1m INVARIANT HELD: {held}** (전후 combined_sha256 {'일치' if held else '불일치'})")
    a("")
    a("## 종목별 스냅샷")
    a("| symbol | rows | days | earliest | latest | span_y | verdict | depth | listing | sha256(12) |")
    a("|--------|-----:|-----:|----------|--------|-------:|---------|-------|:-------:|------------|")
    for e in m["symbols"]:
        v = e["verdict"]
        a(f"| {e['symbol']} | {e['row_count']} | {e['trading_days']} | "
          f"{(e['earliest'] or '')[:10]} | {(e['latest'] or '')[:10]} | "
          f"{e['span_years']} | {v['status']} | {v['depth_source']} | "
          f"{'Y' if v['listing_limited'] else ''} | `{e['content_sha256'][:12]}` |")
    a("")
    a("### 상장/스핀오프 이력 제한(별도 표시)")
    for e in m["symbols"]:
        if e["verdict"]["listing_limited"]:
            a(f"- **{e['symbol']}**: earliest {(e['earliest'] or '')[:10]} "
              f"(≈{e['span_years']}y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥5년 요건 충족.")
    a("")
    a("## R1 배당 조정 점검 (XLU)")
    a(f"- adjusted 플래그: {r1['adjusted_flag']} · 최대 단일일 낙폭: {r1['max_single_day_drop_pct']}%")
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
      "(≥5)에서만 신뢰 가능하며 작은 split 은 노이즈로 INCONCLUSIVE.")
    a("")
    a("## 안전 재확인")
    a("- 전략 수익률·forward return·신호 성능·blind OOS 성과: **미계산/미열람**.")
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
    ap.add_argument("--params-json", default="{}")
    ap.add_argument("--baseline-1m-sha", default=None)
    ap.add_argument("--backfill-json", default=None, help="실행 로그 JSON(리포트 provenance)")
    ap.add_argument("--generated-at", default=None)
    args = ap.parse_args(argv)

    params = json.loads(args.params_json)
    manifest = build_manifest(args.db, args.yaml, collection_params=params,
                              collection_code_commit=args.code_commit,
                              generated_at_utc=args.generated_at)

    con = _connect_ro(args.db)
    try:
        r1 = r1_dividend_check(con)
        r2 = r2_split_checks(con)
        inv_post = invariant_snapshot(con, "1m")
    finally:
        con.close()
    # data_quality_checks 는 provenance/분석 → content hash 대상 아님(별도 섹션).
    manifest["data_quality_checks"] = {"R1_dividend_xlu": r1, "R2_split_continuity": r2}
    manifest["invariant_1m_post"] = {k: inv_post[k] for k in
                                     ("interval", "symbol_count", "total_rows", "combined_sha256")}
    if args.baseline_1m_sha:
        manifest["invariant_1m_baseline_combined_sha256"] = args.baseline_1m_sha
        manifest["invariant_1m_held"] = (args.baseline_1m_sha == inv_post["combined_sha256"])

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"[manifest] wrote {args.out}  content_sha={manifest['manifest_content_sha256']}")

    if args.report:
        backfill_json = None
        if args.backfill_json and os.path.exists(args.backfill_json):
            backfill_json = json.load(open(args.backfill_json, encoding="utf-8"))
        md = render_report_md(manifest, r1, r2, inv_post, args.baseline_1m_sha, backfill_json)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[report] wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
