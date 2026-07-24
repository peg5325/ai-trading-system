"""P0-30 1분봉(1m) coverage/snapshot manifest + 리포트 — market_history.sqlite read-only.

주문/Toss API 호출 없음. **수익률·forward return·신호 성능·blind OOS 성과 미계산/미열람.**
DB 는 mode=ro 로만 연다(원본 무변경). 실제 bulk 미실행 — 수집 후 커버리지 검증 도구다.

사용:
  ./.venv/bin/python scripts/history_intraday_manifest.py \
      --db data/market_history.sqlite --yaml config/universe_p0_us30.yaml \
      --out docs/research/P0_US30_INTRADAY_1M_MANIFEST.json \
      --report docs/research/P0_US30_INTRADAY_1M_REPORT.md \
      --code-commit <sha> --as-of 2026-07-21 \
      --sessions PRE,REGULAR,POST \
      --raw-manifest reports/raw_snapshot_manifest.json \
      --params-json '{"gap_skip_minutes":200,"page_size":200,"interval":"1m"}'
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.intraday_manifest import build_manifest  # noqa: E402


def render_report_md(m: dict) -> str:
    lines: list[str] = []
    a = lines.append
    s = m["summary"]
    a(f"# P0-US30 1분봉(1m) coverage/snapshot 리포트\n")
    a(f"- schema: `{m['schema']}` · interval: **1m** · requested as_of: **{m['requested_as_of']}**")
    a(f"- allowed_sessions: {m['allowed_sessions']} · min_years: {m['min_years']}")
    a(f"- blind OOS: {m['blind_oos']['start']} ~ {m['blind_oos']['end']} "
      f"(months={m['blind_oos']['months']})")
    a(f"- data_content_sha256: `{m['data_content_sha256']}`")
    if m.get("raw_snapshot"):
        a(f"- raw_content_sha256: `{m['raw_snapshot'].get('raw_content_sha256')}` "
          f"(files={m['raw_snapshot'].get('content_file_count')})")
    a(f"- generated_at_utc: {m['generated_at_utc']} (hash 비대상)\n")
    a("## 요약")
    a(f"- 종목 {s['symbols_total']} / attempted {s['attempted']} / with_rows {s['with_rows']}")
    a(f"- verdict: {json.dumps(s['verdict_counts'], ensure_ascii=False)}")
    a(f"- 3.0년 충족 종목: {s['meets_3y_count']}")
    a(f"- 공식 총 1m 행수(≤as_of): {s['total_rows']} · as_of 초과: {s['bars_beyond_as_of_total']}")
    a(f"- 세션별 행수: {json.dumps(s['session_row_totals'], ensure_ascii=False)}")
    a(f"- 범위: {s['min_earliest']} ~ {s['max_latest']}\n")
    a("## 종목별 스냅샷 (≤as_of)")
    a("| symbol | rows | days | earliest_td | latest_td | span_y | PRE | REG | POST | "
      "beyond | verdict | reasons |")
    a("|--------|-----:|-----:|-------------|-----------|-------:|----:|----:|----:|-------:|"
      "---------|---------|")
    for e in m["symbols"]:
        sess = e["sessions"]
        v = e["verdict"]
        a(f"| {e['symbol']} | {e['row_count']} | {e['trading_days']} | "
          f"{e['earliest_trading_day']} | {e['latest_trading_day']} | {e['span_years']} | "
          f"{sess.get('PRE', 0)} | {sess.get('REGULAR', 0)} | {sess.get('POST', 0)} | "
          f"{e['bars_beyond_as_of']} | {v['status']} | "
          f"{','.join(v['inconclusive_reasons']) or '-'} |")
    a("\n> verdict OK = row_count>0 AND span≥min_years AND as_of 초과 0 AND 허용 세션만. "
      "하나라도 불충족 시 INCONCLUSIVE_DATA(사유는 verdict.inconclusive_reasons).")
    a("\n## 안전 재확인")
    a("- 전략 수익률·forward return·신호 성능·blind OOS 성과: **미계산/미열람**.")
    a("- 주문/Paper/전략·config 경로: **무변경**. DB 는 mode=ro 로만 열림.")
    return "\n".join(lines) + "\n"


def main(argv):
    ap = argparse.ArgumentParser(description="P0-30 1분봉 manifest/report (read-only)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--yaml", required=True)
    ap.add_argument("--out", required=True, help="manifest JSON 출력 경로")
    ap.add_argument("--report", help="리포트 MD 출력 경로")
    ap.add_argument("--code-commit", default=None)
    ap.add_argument("--as-of", default=None, help="공식 포함 경계(YYYY-MM-DD). 미지정 시 YAML as_of")
    ap.add_argument("--completed-through", default=None,
                    help="실제 수집 완료-거래일 cutoff(YYYY-MM-DD, 이슈 #72). cutoff<as_of 면 fail-closed.")
    ap.add_argument("--collection-run-id", default=None,
                    help="수집 실행의 collection run_id(이슈 #79). 지정 시 ingest_log 의 실제 수집 "
                         "cutoff 와 strict 교차검증(불일치/누락/malformed/conflicting 시 fail-closed).")
    ap.add_argument("--sessions", default=None, help="허용 세션(쉼표). 미지정 시 YAML sessions_required")
    ap.add_argument("--raw-manifest", default=None,
                    help="RawSnapshotStore.manifest() JSON 경로(선택) — raw_content_sha256 부기")
    ap.add_argument("--params-json", default="{}")
    ap.add_argument("--generated-at", default=None)
    args = ap.parse_args(argv)

    allowed = ([x.strip().upper() for x in args.sessions.split(",") if x.strip()]
               if args.sessions else None)
    raw_manifest = None
    if args.raw_manifest:
        with open(args.raw_manifest, encoding="utf-8") as f:
            raw_manifest = json.load(f)

    manifest = build_manifest(
        args.db, args.yaml, collection_params=json.loads(args.params_json),
        collection_code_commit=args.code_commit, allowed_sessions=allowed,
        as_of=args.as_of, raw_manifest=raw_manifest, generated_at_utc=args.generated_at,
        collection_completed_through=args.completed_through,
        collection_run_id=args.collection_run_id)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"[manifest] wrote {args.out}  data_content_sha256={manifest['data_content_sha256']}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(render_report_md(manifest))
        print(f"[report] wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
