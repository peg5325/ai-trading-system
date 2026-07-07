"""ReviewBatchRunner — 기간 내 cycle 을 Replay→Review 순회하고 ReviewStore 에 영속화한다.

책임(스코프 #1):
- 순회 + 영속화 + 경량 요약. 본격 Rule Improvement Aggregator(정규화/군집/랭킹/기간비교)는 #2.
원칙:
- read-only 원본(Journal/Candle). 매매/주문 없음.
- resume: (cycle_id + prompt_version + model_name + review_version) 이미 있으면 skip.
- 실패한 review 도 REVIEW_FAILED 로 영속(재실행 시 중복 호출 방지 + 실패 목록 확보).
- 결정론: created_at/elapsed 는 외부 주입(now_iso/clock). 코어에 시각/난수 없음.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from src.review.models import REVIEW_OK
from src.review.store import REVIEW_SOURCE_BATCH, REVIEW_VERSION, ReviewRecord


@dataclass
class RunnerConfig:
    review_version: str = REVIEW_VERSION
    review_source: str = REVIEW_SOURCE_BATCH
    spacing_s: float = 0.0                 # review 호출 간격(실 provider rate limit 완화)
    only_with_outcomes: bool = True        # 판단이 있었던 cycle 만 회고
    top_n_improvements: int = 10


@dataclass
class RunSummary:
    """경량 요약 — 사람이 바로 이해할 수 있는 최소 지표."""
    total_cycles: int = 0
    reviewed: int = 0                      # 이번 실행에서 실제 review 시도(ok+failed)
    ok: int = 0
    failed: int = 0
    skipped_resumed: int = 0
    skipped_no_outcomes: int = 0
    quality_distribution: dict = field(default_factory=dict)
    avg_confidence_reassessment: float | None = None
    top_rule_improvements: list = field(default_factory=list)   # [[text, count], ...]
    failed_cycles: list = field(default_factory=list)           # [{cycle_id, error}, ...]
    elapsed_s: float = 0.0
    review_version: str = REVIEW_VERSION
    review_source: str = REVIEW_SOURCE_BATCH
    model_name: str | None = None
    prompt_version: str | None = None

    def to_dict(self) -> dict:
        return {
            "total_cycles": self.total_cycles, "reviewed": self.reviewed,
            "ok": self.ok, "failed": self.failed,
            "skipped_resumed": self.skipped_resumed,
            "skipped_no_outcomes": self.skipped_no_outcomes,
            "quality_distribution": self.quality_distribution,
            "avg_confidence_reassessment": self.avg_confidence_reassessment,
            "top_rule_improvements": self.top_rule_improvements,
            "failed_cycles": self.failed_cycles, "elapsed_s": self.elapsed_s,
            "review_version": self.review_version, "review_source": self.review_source,
            "model_name": self.model_name, "prompt_version": self.prompt_version,
        }


class ReviewBatchRunner:
    def __init__(self, journal, replay_system, review_engine, review_store, *,
                 config: RunnerConfig | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now_iso: Callable[[], str] | None = None):
        self._journal = journal
        self._replay = replay_system
        self._engine = review_engine
        self._store = review_store
        self.cfg = config or RunnerConfig()
        self._sleep = sleep
        self._clock = clock
        self._now_iso = now_iso or _utc_now_iso

    def run(self, *, start: datetime | None = None, end: datetime | None = None,
            cycle_ids: list[str] | None = None, resume: bool = True,
            limit: int | None = None) -> RunSummary:
        t0 = self._clock()
        cycles = self._select_cycles(start, end, cycle_ids)
        pv = self._engine.cfg.prompt_version
        mn = self._engine.provider.model_name
        rv = self.cfg.review_version

        s = RunSummary(total_cycles=len(cycles), review_version=rv,
                       review_source=self.cfg.review_source, model_name=mn,
                       prompt_version=pv)
        quality: Counter = Counter()
        improvements: Counter = Counter()
        conf_sum = 0
        conf_n = 0

        for cycle in cycles:
            if limit is not None and s.reviewed >= limit:
                break
            if self.cfg.only_with_outcomes and not cycle.outcomes:
                s.skipped_no_outcomes += 1
                continue
            if resume and self._store.has(cycle.cycle_id, pv, mn, rv):
                s.skipped_resumed += 1
                continue

            context = self._replay.replay(cycle.cycle_id)          # read-only 복원
            report = self._engine.review(context)                  # GPT/Fake 회고
            record = ReviewRecord.from_report(
                report, cycle_ts=cycle.cycle_ts, symbol=None,
                review_version=rv, review_source=self.cfg.review_source,
                created_at=self._now_iso())
            self._store.save(record)
            s.reviewed += 1

            if report.status == REVIEW_OK:
                s.ok += 1
                quality[report.decision_quality] += 1
                improvements.update(report.rule_improvement)
                conf_sum += report.confidence_reassessment
                conf_n += 1
            else:
                s.failed += 1
                s.failed_cycles.append({"cycle_id": cycle.cycle_id, "error": report.error})

            if self.cfg.spacing_s:
                self._sleep(self.cfg.spacing_s)

        s.quality_distribution = dict(quality)
        s.avg_confidence_reassessment = (conf_sum / conf_n) if conf_n else None
        s.top_rule_improvements = [[t, c] for t, c in
                                   improvements.most_common(self.cfg.top_n_improvements)]
        s.elapsed_s = self._clock() - t0
        return s

    def _select_cycles(self, start, end, cycle_ids):
        if cycle_ids is not None:
            out = []
            for cid in cycle_ids:
                c = self._journal.get_cycle(cid)
                if c is not None:
                    out.append(c)
            return out
        return self._journal.get_by_time_range(start, end)   # cycle_ts 오름차순


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
