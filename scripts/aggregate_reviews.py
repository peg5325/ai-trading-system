"""Rule Improvement Aggregator 진입점 (read-only).

ReviewStore 의 회고를 집계해 전략 개선 후보(제안)를 산출한다.
**제안만 한다.** strategy.yaml/Rule Engine 을 자동 수정하지 않는다 — 실제 변경은 사람 승인 후 별도.

사용:
  ./.venv/bin/python scripts/aggregate_reviews.py config/runtime_mixed.yaml
  # 버전 비교(예: v1 vs v2)
  ./.venv/bin/python scripts/aggregate_reviews.py config/runtime_mixed.yaml \
      --review-version v1 --compare-review-version v2 --export-json reports/rule_improve.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.review.aggregator import (  # noqa: E402
    RuleImprovementAggregator,
    compare_reports,
    export_report_json,
)
from src.review.store import SqliteReviewStore  # noqa: E402
from src.runtime.config import load_yaml  # noqa: E402


def _parse_ts(s: str | None):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="Rule Improvement Aggregator (제안 전용, read-only)")
    ap.add_argument("config", help="runtime yaml (review.db 획득)")
    ap.add_argument("--review-db", default=None)
    ap.add_argument("--start", default=None, help="cycle_ts 하한 (ISO Z)")
    ap.add_argument("--end", default=None, help="cycle_ts 상한 (ISO Z)")
    ap.add_argument("--prompt-version", default=None)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--review-version", default=None)
    ap.add_argument("--decision-quality", default=None)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--threshold", type=float, default=0.6, help="유사 병합 Jaccard 임계")
    ap.add_argument("--export-json", default=None)
    # 비교(대상 리포트 B): 지정한 필드만 바꿔 두 번째 집계 → 추이 비교
    ap.add_argument("--compare-review-version", default=None)
    ap.add_argument("--compare-start", default=None)
    ap.add_argument("--compare-end", default=None)
    args = ap.parse_args(argv)

    cfg = load_yaml(args.config)
    store = SqliteReviewStore(args.review_db or cfg.review_db)
    agg = RuleImprovementAggregator(store, similarity_threshold=args.threshold, top_n=args.top_n)

    report = agg.aggregate(
        start=_parse_ts(args.start), end=_parse_ts(args.end),
        prompt_version=args.prompt_version, model_name=args.model_name,
        review_version=args.review_version, decision_quality=args.decision_quality)

    print(f"[aggregate] reviews={report.total_reviews} (ok={report.ok_reviews}) "
          f"rule_improvements={report.total_rule_improvements} "
          f"quality={report.decision_quality_distribution} confidence={report.confidence}")
    print("[aggregate] Top rule_improvement clusters:")
    for c in report.top_rule_improvements():
        print(f"  ({c.count:3d}) {c.representative}  {c.quality_distribution}")
    print("[aggregate] Top missed_signal clusters:")
    for c in report.top_missed_signals():
        print(f"  ({c.count:3d}) {c.representative}")

    if args.export_json:
        p = export_report_json(report, args.export_json)
        print(f"[aggregate] exported → {p}")

    if args.compare_review_version or args.compare_start or args.compare_end:
        report_b = agg.aggregate(
            start=_parse_ts(args.compare_start or args.start),
            end=_parse_ts(args.compare_end or args.end),
            prompt_version=args.prompt_version, model_name=args.model_name,
            review_version=args.compare_review_version or args.review_version,
            decision_quality=args.decision_quality)
        diff = compare_reports(report, report_b)
        print("[aggregate] compare (A→B) changes:")
        print(json.dumps(diff["changes"], ensure_ascii=False))

    print("[aggregate] 주의: 제안 산출물일 뿐. strategy.yaml/Rule Engine 자동 변경 없음 — 사람 승인 후 별도.")
    store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
