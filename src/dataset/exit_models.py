"""Exit Follow-up Dataset 의 record / dataset 모델.

각 SELL(청산=RealizedTrade) 이벤트에 대해 "청산이 적절했는가?"를 사후 평가하기 위한
관측 데이터. Replay AI Benchmark / Rule Improvement Aggregator / OpenAI A/B Test /
(향후) Legacy Position Manager 가 공통으로 쓰는 Single Source of Truth.

설계 원칙:
- 순수 데이터 컨테이너. 매매/판단 로직 없음. read-only 배치 산출물.
- 확장 가능 스키마: 컬럼 추가는 허용, 기존 컬럼 의미 변경(Breaking Change)은 금지.
  스키마 변경 시 EXIT_DATASET_VERSION 을 올려 재현성을 보장한다.
- Look-ahead 방지: 모든 forward 값은 exit_time '이후' 봉만 사용. 세션 종료를 넘는
  horizon 은 price=None + complete=False(INCOMPLETE)로 표기한다.
- 통화 안전: per-symbol 관측이므로 단일 통화. 계좌 혼합 합산을 하지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# 스키마 버전 (컬럼 추가는 minor, 의미 변경은 major 로 올린다)
EXIT_DATASET_VERSION = "1.0"


def iso(ts: datetime | None) -> str | None:
    """aware/naive datetime → UTC ISO(Z) 문자열."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ExitForwardPoint:
    """단일 forward horizon(분)의 청산 후 결과. 세션 종료를 넘으면 complete=False, price=None."""
    horizon_min: int
    target_time: str | None          # exit_time + horizon (감사/추적용)
    price: float | None              # +horizon 시점 가격(청산 이후 봉만). 세션 초과 시 None
    forward_return: float | None     # (price - exit_price) / exit_price
    complete: bool                   # horizon 이 세션 내에서 완결되었는가(INCOMPLETE=False)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExitFollowupRecord:
    """한 SELL(청산) 이벤트의 사후 관측 = 기본/가격/청산/forward/사후평가/label."""
    # ---- 기본 정보 ----
    session_id: str | None
    symbol: str
    market: str | None
    side: str                        # SELL
    entry_time: str | None
    exit_time: str | None
    holding_minutes: float | None
    holding_bars: int | None
    # ---- 가격 정보 ----
    entry_price: float | None
    exit_price: float | None
    # ---- 청산 정보 ----
    exit_reason: str | None          # STOP_LOSS | TAKE_PROFIT | TIME_EXIT | EOD_FLATTEN | ...
    # ---- Forward Price / Return (+5/+15/+30/+60분) ----
    forwards: list[ExitForwardPoint] = field(default_factory=list)
    # ---- Session End ----
    session_end_time: str | None = None
    session_end_price: float | None = None
    session_end_return: float | None = None
    session_end_complete: bool = False   # 청산 이후 관측된 세션 마감가인가(exit가 마지막봉이면 False)
    # ---- 사후 평가 지표(청산 후 eval 창 기준) ----
    mfe: float | None = None             # (best_price_after_exit - exit)/exit  상방(놓친 이익)
    mae: float | None = None             # (worst_price_after_exit - exit)/exit 하방(피한 손실)
    best_price_after_exit: float | None = None
    worst_price_after_exit: float | None = None
    eval_window_min: int = 0             # MFE/MAE/best/worst 계산 창(분, 세션말로 clip)
    eval_n_bars: int = 0
    eval_complete: bool = False          # eval 창이 세션 내에서 완결되었는가
    # ---- Replay AI Labels ----
    # STOP_LOSS 이후 일정 시간 내 가격이 청산가 위로 회복했는가(손절이 너무 빨랐는가).
    did_price_recover_after_stop_loss: bool | None = None
    # TAKE_PROFIT 이후 일정 시간 내 추가 상승이 이어졌는가(익절이 너무 빨랐는가).
    did_price_continue_after_take_profit: bool | None = None
    # ---- provenance ----
    schema_version: str = EXIT_DATASET_VERSION

    def forward_map(self) -> dict[int, ExitForwardPoint]:
        return {f.horizon_min: f for f in self.forwards}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["forwards"] = [f.to_dict() for f in self.forwards]
        return d

    def to_flat(self) -> dict:
        """단일 레벨 dict (Parquet/DataFrame/사용자 요청 컬럼명)."""
        out: dict = {
            "session_id": self.session_id, "symbol": self.symbol, "market": self.market,
            "side": self.side, "entry_time": self.entry_time, "exit_time": self.exit_time,
            "holding_minutes": self.holding_minutes, "holding_bars": self.holding_bars,
            "entry_price": self.entry_price, "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
        }
        for fp in self.forwards:
            h = fp.horizon_min
            out[f"forward_price_{h}m"] = fp.price
            out[f"forward_return_{h}m"] = fp.forward_return
            out[f"forward_complete_{h}m"] = fp.complete
        out.update({
            "session_end_time": self.session_end_time,
            "session_end_price": self.session_end_price,
            "session_end_return": self.session_end_return,
            "session_end_complete": self.session_end_complete,
            "mfe": self.mfe, "mae": self.mae,
            "best_price_after_exit": self.best_price_after_exit,
            "worst_price_after_exit": self.worst_price_after_exit,
            "eval_window_min": self.eval_window_min, "eval_n_bars": self.eval_n_bars,
            "eval_complete": self.eval_complete,
            "did_price_recover_after_stop_loss": self.did_price_recover_after_stop_loss,
            "did_price_continue_after_take_profit": self.did_price_continue_after_take_profit,
            "schema_version": self.schema_version,
        })
        return out


@dataclass
class ExitDatasetMeta:
    """데이터셋 수준 provenance/재현성."""
    schema_version: str = EXIT_DATASET_VERSION
    session_id: str | None = None
    interval: str = "1m"
    horizons_min: tuple[int, ...] = (5, 15, 30, 60)
    eval_window_min: int = 60
    source: str | None = None            # journal db 경로 등
    built_at: str | None = None          # 빌드 시각(ISO, 외부 주입)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["horizons_min"] = list(self.horizons_min)
        return d


@dataclass
class ExitFollowupDataset:
    """청산 관측 record 모음 + provenance. Exporter 프로토콜(to_meta_dict/__iter__/to_flat_dicts) 호환."""
    records: list[ExitFollowupRecord] = field(default_factory=list)
    meta: ExitDatasetMeta = field(default_factory=ExitDatasetMeta)

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
