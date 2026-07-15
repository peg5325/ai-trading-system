"""Offline Shadow Outcome dataset 모델 (v2.1 PR 4-B, 계약서 §2-4 오프라인).

세션 후 read-only batch 가 4-A shadow 결정(사이클 국면 gate)에 **price-direction** forward
return 을 부착한 관측. 각 candidate_id 별로 결정 시점 이후 가격 방향을 잰다.

⚠ price-direction shadow: forward_return/avoided_loss/missed_profit 은 '결정봉 종가 대비 가격
방향' 프록시일 뿐, 실제 체결 수량/비용/exit counterfactual 이 아니다(필드명·리포트에 명시).

원칙:
- 순수 데이터 컨테이너. 매매/판단 로직 없음. read-only 배치 산출물.
- Look-ahead 방지: forward 는 decision_time '초과'(strict) 봉만, 세션(거래일) clip. horizon 이
  세션 종료를 넘으면 forward=None + complete=False(INCOMPLETE) → 집계 자동 제외.
- 확장 스키마: 컬럼 추가 허용, 의미 변경은 SHADOW_OUTCOME_VERSION bump.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SHADOW_OUTCOME_VERSION = "1.0"


def iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ShadowForwardPoint:
    """단일 forward horizon(분)의 price-direction 결과. 세션 초과 시 complete=False, 값 None."""
    horizon_min: int
    target_time: str | None
    forward_return: float | None     # (price(target) - reference_price) / reference_price
    avoided_loss: float | None       # max(-forward_return, 0) — 진입 안 해 피한 하락
    missed_profit: float | None      # max(forward_return, 0) — 진입 안 해 놓친 상승
    complete: bool                   # horizon 이 세션 내에서 완결되었는가(INCOMPLETE=False)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ShadowOutcomeRecord:
    """한 candidate_id 의 오프라인 outcome = 조인키 + 결정 + price-direction forward.

    policies 는 4-A 사이클 국면 결정(would_allow)을 fan-out 으로 그대로 복제(국면-단위 결정을
    이 후보에 적용). forward 는 candidate symbol 의 결정봉 종가 대비 방향.
    """
    candidate_id: str
    symbol: str
    market: str | None
    decision_time: str | None
    reference_price: float | None            # 결정봉 종가(진입 프록시 기준가)
    policies: list[dict] = field(default_factory=list)   # [{policy_id, would_allow, ...}]
    forwards: list[ShadowForwardPoint] = field(default_factory=list)
    schema_version: str = SHADOW_OUTCOME_VERSION

    def forward_map(self) -> dict[int, ShadowForwardPoint]:
        return {f.horizon_min: f for f in self.forwards}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["forwards"] = [f.to_dict() for f in self.forwards]
        return d


@dataclass
class ShadowOutcomeMeta:
    schema_version: str = SHADOW_OUTCOME_VERSION
    session_id: str | None = None
    interval: str = "1m"
    horizons_min: tuple[int, ...] = (30, 60)
    source: str | None = None
    built_at: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["horizons_min"] = list(self.horizons_min)
        return d


@dataclass
class ShadowOutcomeDataset:
    records: list[ShadowOutcomeRecord] = field(default_factory=list)
    meta: ShadowOutcomeMeta = field(default_factory=ShadowOutcomeMeta)

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def to_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.records]

    def to_meta_dict(self) -> dict:
        d = self.meta.to_dict()
        d["n_records"] = len(self.records)
        return d
