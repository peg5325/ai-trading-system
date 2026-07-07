"""Review Batch Runner 진입점 (read-only).

runtime yaml 의 journal/candle 을 읽어 기간 내 cycle 을 Replay→Review 순회하고,
ReviewStore(SQLite, SSoT)에 영속화한다. 실시간 매매에 관여하지 않는다.

기본 provider 는 FakeReviewProvider(비용/네트워크 없음, 결정론). 실제 회고는 --provider openai.

사용:
  ./.venv/bin/python scripts/run_reviews.py config/runtime_mixed.yaml
  ./.venv/bin/python scripts/run_reviews.py config/runtime_mixed.yaml \
      --provider openai --spacing 0.5 --export-jsonl reports/reviews_mixed.jsonl \
      --start 2026-07-07T00:00:00Z --end 2026-07-08T00:00:00Z
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.review.batch_runner import ReviewBatchRunner, RunnerConfig  # noqa: E402
from src.review.engine import AIReviewEngine, ReviewConfig  # noqa: E402
from src.review.exporters import ReviewJsonlExporter  # noqa: E402
from src.review.providers import FakeReviewProvider  # noqa: E402
from src.review.store import REVIEW_VERSION, SqliteReviewStore  # noqa: E402
from src.replay.system import ReplaySystem  # noqa: E402
from src.runtime.config import load_yaml  # noqa: E402
from src.storage.candle_store import CandleStore  # noqa: E402
from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402

# Fake provider 기본 응답 — 결정론적 REVIEW_OK (파이프라인 검증용, 실분석 아님)
_FAKE_REVIEW = {
    "decision_quality": "ACCEPTABLE",
    "missed_signals": [],
    "risk_assessment": "handled within limits (fake review)",
    "alternative_action": "none",
    "rule_improvement": ["collect more paper data before tuning thresholds"],
    "confidence_reassessment": 50,
}


def _parse_ts(s: str | None):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _provider(kind: str, model: str):
    if kind == "openai":
        from src.ai.providers import OpenAIProvider
        return OpenAIProvider(model_name=model)
    return FakeReviewProvider(response=_FAKE_REVIEW, model_name=model)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="Review Batch Runner (Replay→Review, read-only)")
    ap.add_argument("config", help="runtime yaml")
    ap.add_argument("--review-db", default=None, help="ReviewStore 경로 (기본: yaml review.db)")
    ap.add_argument("--provider", choices=["fake", "openai"], default="fake")
    ap.add_argument("--model", default=None, help="모델명 (기본: yaml ai.model)")
    ap.add_argument("--review-version", default=REVIEW_VERSION)
    ap.add_argument("--source", default="BATCH", help="review_source (AI/MANUAL/BATCH/SYSTEM)")
    ap.add_argument("--start", default=None, help="cycle_ts 하한 (ISO Z)")
    ap.add_argument("--end", default=None, help="cycle_ts 상한 (ISO Z)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--spacing", type=float, default=0.0, help="review 호출 간격(초)")
    ap.add_argument("--no-resume", action="store_true", help="이미 회고한 cycle 도 재실행")
    ap.add_argument("--export-jsonl", default=None, help="회고 결과 JSONL export 경로")
    args = ap.parse_args(argv)

    cfg = load_yaml(args.config)
    review_db = args.review_db or cfg.review_db
    model = args.model or cfg.ai_model

    journal = SqliteJournalStore(cfg.journal_db)
    candles = CandleStore(cfg.candle_db)
    replay = ReplaySystem(journal, candles)
    engine = AIReviewEngine(_provider(args.provider, model), ReviewConfig())
    store = SqliteReviewStore(review_db)

    runner = ReviewBatchRunner(
        journal, replay, engine, store,
        config=RunnerConfig(review_version=args.review_version, review_source=args.source,
                            spacing_s=args.spacing))
    summary = runner.run(start=_parse_ts(args.start), end=_parse_ts(args.end),
                         resume=not args.no_resume, limit=args.limit)

    print("[run_reviews] " + json.dumps(summary.to_dict(), ensure_ascii=False))
    print(f"[run_reviews] store={review_db} provider={args.provider} model={model} "
          f"review_version={args.review_version}")

    if args.export_jsonl:
        recs = store.query(start=_parse_ts(args.start), end=_parse_ts(args.end),
                           review_version=args.review_version)
        n = ReviewJsonlExporter().export(recs, args.export_jsonl,
                                         meta={"source": review_db,
                                               "review_version": args.review_version})
        print(f"[run_reviews] exported {n} reviews → {args.export_jsonl}")

    journal.close()
    candles.close()
    store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
