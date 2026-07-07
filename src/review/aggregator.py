"""RuleImprovementAggregator — ReviewStore 의 회고를 집계해 전략 개선 후보를 도출한다.

원칙(중요):
- **제안만 한다.** strategy.yaml / Rule Engine 을 자동 수정하지 않는다. 실제 변경은 사람 승인 후 별도.
- ReviewStore(read-only)만 읽는다. 실시간 매매/주문/Runtime 에 관여하지 않는다.
- 결정론적: 난수/시각/외부호출 없음. 같은 입력 → 같은 리포트.

집계 대상: rule_improvement / missed_signals(텍스트 정규화 + 유사 그룹핑 + 빈도 랭킹),
decision_quality 분포, confidence_reassessment 통계. prompt/model/review_version·기간 필터로
버전별·기간별 비교를 지원(compare_reports).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.review.models import REVIEW_OK

_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "at", "is", "be",
    "with", "more", "less", "before", "after", "when", "than", "that", "this", "should",
})


def normalize_text(text: str) -> str:
    """소문자화 + 구두점 제거 + 공백 정규화. 결정론적."""
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tokens(norm: str) -> frozenset:
    return frozenset(w for w in norm.split() if w not in _STOPWORDS)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class ClusterStat:
    representative: str                 # 대표 문구(최다 빈도 원문)
    count: int                          # 총 등장 횟수
    variants: list = field(default_factory=list)          # [[raw, count], ...]
    quality_distribution: dict = field(default_factory=dict)   # {decision_quality: count}

    def to_dict(self) -> dict:
        return {"representative": self.representative, "count": self.count,
                "variants": self.variants, "quality_distribution": self.quality_distribution}


@dataclass
class RuleImprovementReport:
    filters: dict
    total_reviews: int
    ok_reviews: int
    failed_reviews: int
    total_rule_improvements: int
    total_missed_signals: int
    decision_quality_distribution: dict
    confidence: dict                    # {n, avg, min, max}
    rule_improvement_clusters: list = field(default_factory=list)   # count desc
    missed_signal_clusters: list = field(default_factory=list)
    top_n: int = 10

    def top_rule_improvements(self) -> list:
        return self.rule_improvement_clusters[:self.top_n]

    def top_missed_signals(self) -> list:
        return self.missed_signal_clusters[:self.top_n]

    def to_dict(self) -> dict:
        return {
            "filters": self.filters, "total_reviews": self.total_reviews,
            "ok_reviews": self.ok_reviews, "failed_reviews": self.failed_reviews,
            "total_rule_improvements": self.total_rule_improvements,
            "total_missed_signals": self.total_missed_signals,
            "decision_quality_distribution": self.decision_quality_distribution,
            "confidence": self.confidence, "top_n": self.top_n,
            "rule_improvement_clusters": [c.to_dict() for c in self.rule_improvement_clusters],
            "missed_signal_clusters": [c.to_dict() for c in self.missed_signal_clusters],
        }


class RuleImprovementAggregator:
    def __init__(self, review_store, *, similarity_threshold: float = 0.6, top_n: int = 10):
        self._store = review_store
        self._threshold = similarity_threshold
        self._top_n = top_n

    def aggregate(self, *, start: datetime | None = None, end: datetime | None = None,
                  prompt_version: str | None = None, model_name: str | None = None,
                  review_version: str | None = None,
                  decision_quality: str | None = None) -> RuleImprovementReport:
        q = {}
        if prompt_version is not None:
            q["prompt_version"] = prompt_version
        if model_name is not None:
            q["model_name"] = model_name
        if review_version is not None:
            q["review_version"] = review_version
        if decision_quality is not None:
            q["decision_quality"] = decision_quality
        records = self._store.query(start=start, end=end, **q)

        ok = [r for r in records if r.status == REVIEW_OK]
        quality_dist: Counter = Counter(r.decision_quality for r in ok)
        confs = [r.confidence_reassessment for r in ok
                 if r.confidence_reassessment is not None]

        # (normalized, raw, quality) 발생 수집
        ri_occ = [(imp, r.decision_quality) for r in ok for imp in (r.rule_improvement or [])]
        ms_occ = [(sig, r.decision_quality) for r in ok for sig in (r.missed_signals or [])]

        return RuleImprovementReport(
            filters={"start": _iso(start), "end": _iso(end),
                     "prompt_version": prompt_version, "model_name": model_name,
                     "review_version": review_version, "decision_quality": decision_quality},
            total_reviews=len(records), ok_reviews=len(ok),
            failed_reviews=len(records) - len(ok),
            total_rule_improvements=len(ri_occ), total_missed_signals=len(ms_occ),
            decision_quality_distribution=dict(quality_dist),
            confidence=_conf_stats(confs),
            rule_improvement_clusters=self._cluster(ri_occ),
            missed_signal_clusters=self._cluster(ms_occ), top_n=self._top_n)

    # ---- clustering ----
    def _cluster(self, occurrences: list) -> list[ClusterStat]:
        """(raw, quality) 발생들을 정규화→유사 그룹핑→빈도 내림차순 ClusterStat."""
        if not occurrences:
            return []
        # 1) 정규화 exact 그룹 (빈도/원문변형/품질 집계)
        groups: dict[str, dict] = {}
        for raw, quality in occurrences:
            norm = normalize_text(raw)
            if not norm:
                continue
            g = groups.setdefault(norm, {"count": 0, "raw": Counter(),
                                         "quality": Counter(), "tokens": _tokens(norm)})
            g["count"] += 1
            g["raw"][raw] += 1
            g["quality"][quality] += 1
        if not groups:
            return []

        # 2) greedy 유사 병합 (Jaccard >= threshold). 결정론: 빈도 desc, norm asc 순.
        order = sorted(groups.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        clusters: list[dict] = []
        for norm, g in order:
            placed = None
            for c in clusters:
                if _jaccard(g["tokens"], c["tokens"]) >= self._threshold:
                    placed = c
                    break
            if placed is None:
                clusters.append({"count": g["count"], "raw": Counter(g["raw"]),
                                 "quality": Counter(g["quality"]), "tokens": set(g["tokens"])})
            else:
                placed["count"] += g["count"]
                placed["raw"].update(g["raw"])
                placed["quality"].update(g["quality"])
                placed["tokens"] |= g["tokens"]

        # 3) ClusterStat (대표=최다 원문, 동률은 사전순)
        out = []
        for c in clusters:
            rep = max(sorted(c["raw"].items()), key=lambda kv: kv[1])[0]
            out.append(ClusterStat(
                representative=rep, count=c["count"],
                variants=[[t, n] for t, n in c["raw"].most_common()],
                quality_distribution=dict(c["quality"])))
        out.sort(key=lambda cs: (-cs.count, cs.representative))
        return out


def compare_reports(a: RuleImprovementReport, b: RuleImprovementReport) -> dict:
    """두 리포트(버전/기간 다름)의 rule_improvement 클러스터 빈도 추이 비교.
    대표 문구 기준. delta = b_count - a_count (b 가 '이후/대상')."""
    ca = {c.representative: c.count for c in a.rule_improvement_clusters}
    cb = {c.representative: c.count for c in b.rule_improvement_clusters}
    keys = sorted(set(ca) | set(cb))
    changes = [{"representative": k, "a": ca.get(k, 0), "b": cb.get(k, 0),
                "delta": cb.get(k, 0) - ca.get(k, 0)} for k in keys]
    changes.sort(key=lambda d: (-abs(d["delta"]), d["representative"]))
    return {
        "filters_a": a.filters, "filters_b": b.filters,
        "ok_reviews_a": a.ok_reviews, "ok_reviews_b": b.ok_reviews,
        "quality_a": a.decision_quality_distribution,
        "quality_b": b.decision_quality_distribution,
        "confidence_a": a.confidence, "confidence_b": b.confidence,
        "changes": changes,
    }


def export_report_json(report: RuleImprovementReport, path: str | Path) -> str:
    """RuleImprovementReport 를 JSON 파일로 저장(제안 산출물, 외부 공유/검토용)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    return str(path)


def _conf_stats(confs: list) -> dict:
    if not confs:
        return {"n": 0, "avg": None, "min": None, "max": None}
    return {"n": len(confs), "avg": round(sum(confs) / len(confs), 2),
            "min": min(confs), "max": max(confs)}


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None
