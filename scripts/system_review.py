"""System Review Engine 진입점 — 특정 기간(예: 하루) 운영 회고 자동 생성.

RuntimeMetricsStore(read-only) + JournalStore 로 window 요약을 만들고, GPT(기본 Fake)로
운영 품질을 회고해 SystemReviewStore 에 로그를 남긴다.
**운영 회고 전용** — Rule Engine / Trading Pipeline / OrderManager / RiskManager / strategy.yaml
에 절대 영향 없음. 제안만 생성.

실제 OpenAI 회고는 이 스크립트에서 --provider openai 로만 수행(수동).

사용:
  ./.venv/bin/python scripts/system_review.py config/runtime_mixed.yaml            # 최근 24h
  ./.venv/bin/python scripts/system_review.py config/runtime_mixed.yaml \
      --start 2026-07-07T00:00:00Z --end 2026-07-08T00:00:00Z --provider openai
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402
from src.review.providers import FakeReviewProvider  # noqa: E402
from src.runtime.config import load_yaml  # noqa: E402
from src.runtime.metrics_store import SqliteMetricsStore  # noqa: E402
from src.system_review.engine import SystemReviewConfig, SystemReviewEngine  # noqa: E402
from src.system_review.store import SqliteSystemReviewStore  # noqa: E402

# Fake 기본 응답 — 결정론적 OK(파이프라인 검증용, 실분석 아님)
_FAKE = {
    "grade": "B", "summary": "Operational metrics within nominal ranges (fake review).",
    "major_issues": [], "recommendations": ["keep collecting paper data"],
    "confidence": 70,
}


def _parse_ts(s: str | None):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _provider(kind: str, model: str):
    if kind == "openai":
        from src.ai.providers import OpenAIProvider
        return OpenAIProvider(model_name=model)
    return FakeReviewProvider(response=_FAKE, model_name=model)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="System Review Engine (운영 회고 전용, read-only)")
    ap.add_argument("config", help="runtime yaml")
    ap.add_argument("--provider", choices=["fake", "openai"], default="fake")
    ap.add_argument("--model", default=None, help="모델명 (기본: yaml ai.model)")
    ap.add_argument("--start", default=None, help="window 시작 (ISO Z)")
    ap.add_argument("--end", default=None, help="window 끝 (ISO Z)")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="--start 미지정 시 --end(또는 지금) 기준 최근 N시간")
    ap.add_argument("--now", default=None, help="--end 미지정 시 기준 시각(ISO Z), 테스트용")
    ap.add_argument("--sysrev-db", default=None)
    ap.add_argument("--export-json", default=None)
    args = ap.parse_args(argv)

    cfg = load_yaml(args.config)
    model = args.model or cfg.ai_model
    end = _parse_ts(args.end) or _parse_ts(args.now)
    start = _parse_ts(args.start)
    if start is None and end is not None:
        start = end - timedelta(hours=args.hours)

    metrics = SqliteMetricsStore(cfg.metrics_db)
    journal = SqliteJournalStore(cfg.journal_db)
    store = SqliteSystemReviewStore(args.sysrev_db or cfg.system_review_db)
    engine = SystemReviewEngine(_provider(args.provider, model), metrics, journal,
                                SystemReviewConfig(), log_store=store)

    report = engine.review(start=start, end=end)
    rt = report.runtime_window.get("runtime", {})
    print(f"[system_review] status={report.status} grade={report.grade} "
          f"confidence={report.confidence} provider={args.provider} model={model}")
    print(f"[system_review] window={report.runtime_window.get('window')} "
          f"cycles_run={rt.get('cycles_run')} api_errors={rt.get('api_errors')} "
          f"exceptions={rt.get('exceptions')} avg_loop_ms={rt.get('avg_loop_ms')} "
          f"degraded={rt.get('degraded_cycles')} error_rate={rt.get('error_rate')}")
    print(f"[system_review] summary: {report.summary}")
    if report.major_issues:
        print(f"[system_review] major_issues: {report.major_issues}")
    if report.recommendations:
        print(f"[system_review] recommendations: {report.recommendations}")

    if args.export_json:
        from pathlib import Path
        Path(args.export_json).parent.mkdir(parents=True, exist_ok=True)
        with open(args.export_json, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"[system_review] exported → {args.export_json}")

    print("[system_review] 주의: 운영 회고(제안)일 뿐. Rule Engine/Pipeline/strategy 자동 변경 없음.")
    metrics.close(); journal.close(); store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
