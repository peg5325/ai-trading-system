"""AI Learning Dataset 의 record / dataset 모델.

설계 원칙:
- 순수 데이터 컨테이너. 매매/판단 로직 없음.
- 모든 record 는 JSON 직렬화 가능(to_dict) + 평탄화(to_flat) 가능.
- FeatureRecord = 진입 시점 상태, LabelRecord = 사후 outcome, MetaRecord = 재현성 버전.
- DatasetRecord = 한 관찰 지점의 (feature + label + meta).
- 스키마 진화 시 *_VERSION 을 올리고 재현성을 보장한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# ---- 스키마 버전 (스키마 변경 시 반드시 갱신) ----
FEATURE_VERSION = "1.0"      # FeatureRecord 필드 구성
DATASET_VERSION = "1.0"      # Dataset row(=feature+label+meta) 구성


@dataclass
class FeatureRecord:
    """진입(관찰) 시점의 상태. Journal outcome + cycle 에서 추출."""
    cycle_id: str
    cycle_ts: str | None
    symbol: str
    market: str | None                  # US | KR
    session: str | None                 # PRE/REGULAR/POST/OVERNIGHT/CLOSED (종목별)
    action: str | None                  # ORDERED | SKIPPED (진입 결과)
    reason: str | None                  # ORDERED / AI_WAIT / NO_CANDIDATE ...
    # 지표 스냅샷 (진입 시점)
    close: float | None = None
    ema7: float | None = None
    ema20: float | None = None
    rsi14: float | None = None
    vwap: float | None = None
    bb_upper: float | None = None
    bb_lower: float | None = None
    stochrsi_k: float | None = None
    stochrsi_d: float | None = None
    # 스크리너 후보
    candidate_score: float | None = None
    candidate_type: str | None = None       # LONG_SETUP | EXCLUDED
    candidate_rank: int | None = None
    risk_flags: list[str] = field(default_factory=list)
    # AI 판단
    ai_decision: str | None = None          # APPROVE_ENTRY / REJECT_ENTRY / WAIT
    ai_confidence: int | None = None
    ai_entry_quality: str | None = None
    ai_risk_level: str | None = None
    # 리스크
    risk_allowed: bool | None = None
    # 포트폴리오 스냅샷 (사이클 시점)
    account_value: float | None = None
    open_positions: int | None = None
    kill_switch: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ForwardMetric:
    """단일 forward window 의 결과."""
    window_min: int
    n_bars: int
    forward_return: float | None        # (종가_t+w - entry)/entry
    mfe: float | None                   # max favorable excursion (max high 기준)
    mae: float | None                   # max adverse excursion (min low 기준)
    end_price: float | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LabelRecord:
    """사후 outcome. Candle(read-only)로 forward return 계산 + (있으면) 실제 체결 정보."""
    entry_price: float | None
    forwards: list[ForwardMetric] = field(default_factory=list)
    # 대표 라벨 (primary window 기준)
    label_window_min: int | None = None
    label_return: float | None = None
    label_class: str | None = None          # WIN | LOSS | NEUTRAL | None(불충분)
    holding_minutes: int | None = None      # forward 데이터가 존재하는 실제 범위(분)
    complete: bool = False                  # primary window 를 채울 forward 데이터 유무
    # 실제 paper trade (진입 체결이 있으면)
    trade_taken: bool = False
    entry_fill_price: float | None = None
    entry_qty: float | None = None
    entry_side: str | None = None
    entry_fee: float | None = None
    # 실현 손익/청산 사유 — MVP 는 관찰 라벨 중심. 청산-매칭은 후속(문서 참고).
    realized_pnl: float | None = None
    exit_reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["forwards"] = [f.to_dict() for f in self.forwards]
        return d

    def forward_map(self) -> dict:
        """window_min -> ForwardMetric (평탄화/조회용)."""
        return {f.window_min: f for f in self.forwards}


@dataclass
class MetaRecord:
    """재현성 provenance. 스키마/프롬프트/전략/모델 버전을 못박는다."""
    feature_version: str = FEATURE_VERSION
    dataset_version: str = DATASET_VERSION
    prompt_version: str | None = None       # AI entry-filter 프롬프트 버전 (row별)
    strategy_version: str | None = None      # 스크리너/지표 세트 버전
    model_name: str | None = None            # AI 모델 (row별)
    built_at: str | None = None              # 빌드 시각(ISO, 옵션)
    source: str | None = None                # journal db 경로 등

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetRecord:
    """한 관찰 지점 = feature + label + meta."""
    feature: FeatureRecord
    label: LabelRecord
    meta: MetaRecord

    def to_dict(self) -> dict:
        """중첩 dict (JSONL 용)."""
        return {
            "feature": self.feature.to_dict(),
            "label": self.label.to_dict(),
            "meta": self.meta.to_dict(),
        }

    def to_flat(self) -> dict:
        """단일 레벨 dict (Parquet/DataFrame 용). 접두사로 네임스페이스 구분."""
        out: dict = {}
        f = self.feature
        out.update({
            "cycle_id": f.cycle_id, "cycle_ts": f.cycle_ts, "symbol": f.symbol,
            "market": f.market, "session": f.session,
            "action": f.action, "reason": f.reason,
            "close": f.close, "ema7": f.ema7, "ema20": f.ema20, "rsi14": f.rsi14,
            "vwap": f.vwap, "bb_upper": f.bb_upper, "bb_lower": f.bb_lower,
            "stochrsi_k": f.stochrsi_k, "stochrsi_d": f.stochrsi_d,
            "candidate_score": f.candidate_score, "candidate_type": f.candidate_type,
            "candidate_rank": f.candidate_rank,
            "risk_flags": ",".join(f.risk_flags) if f.risk_flags else "",
            "risk_flag_count": len(f.risk_flags),
            "ai_decision": f.ai_decision, "ai_confidence": f.ai_confidence,
            "ai_entry_quality": f.ai_entry_quality, "ai_risk_level": f.ai_risk_level,
            "risk_allowed": f.risk_allowed,
            "account_value": f.account_value, "open_positions": f.open_positions,
            "kill_switch": f.kill_switch,
        })
        lab = self.label
        out.update({
            "entry_price": lab.entry_price,
            "label_window_min": lab.label_window_min,
            "label_return": lab.label_return,
            "label_class": lab.label_class,
            "holding_minutes": lab.holding_minutes,
            "label_complete": lab.complete,
            "trade_taken": lab.trade_taken,
            "entry_fill_price": lab.entry_fill_price,
            "entry_qty": lab.entry_qty, "entry_side": lab.entry_side,
            "entry_fee": lab.entry_fee,
            "realized_pnl": lab.realized_pnl, "exit_reason": lab.exit_reason,
        })
        for fm in lab.forwards:
            w = fm.window_min
            out[f"forward_return_{w}m"] = fm.forward_return
            out[f"mfe_{w}m"] = fm.mfe
            out[f"mae_{w}m"] = fm.mae
            out[f"n_bars_{w}m"] = fm.n_bars
        m = self.meta
        out.update({
            "feature_version": m.feature_version, "dataset_version": m.dataset_version,
            "prompt_version": m.prompt_version, "strategy_version": m.strategy_version,
            "model_name": m.model_name,
        })
        return out


@dataclass
class Dataset:
    """관찰 record 모음 + 데이터셋 수준 provenance."""
    records: list[DatasetRecord] = field(default_factory=list)
    meta: MetaRecord = field(default_factory=MetaRecord)

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self):
        return iter(self.records)

    def to_dicts(self) -> list[dict]:
        return [r.to_dict() for r in self.records]

    def to_flat_dicts(self) -> list[dict]:
        return [r.to_flat() for r in self.records]

    def to_meta_dict(self) -> dict:
        d = self.meta.to_dict()
        d["n_records"] = len(self.records)
        return d
