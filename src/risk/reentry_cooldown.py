"""Re-entry Cooldown 정책 — 순수/결정론적.

**과매매(overtrading)/whipsaw 방지 안전장치이며, 수익 개선 기능이 아니다.** 동일 종목 청산
이후 일정 시간 동안 NEW_ENTRY 를 차단한다(특히 STOP_LOSS 직후 즉시 재추격 방지). 기본 비활성
(opt-in)이고 사용 전 Backtest A/B 로 검증해야 한다.

경과 측정(핵심):
- **timeframe 기반**으로 청산 이후 경과를 계산한다. 절대시간 버킷(`timestamp // 300`)을 쓰지 않으며,
  청산 timestamp 와 현재 봉 timestamp 의 delta 를 `timeframe_minutes` 로 환산한다.
- unit="trading_bars": elapsed_bars = elapsed_minutes / timeframe_minutes (같은 세션 내에서는
  연속된 봉 수와 동일). unit="elapsed_minutes": elapsed 를 분 단위로 직접 비교.
- `reset_on_new_session=True`(기본): 세션(거래일) 전환 시 상태를 초기화해 쿨다운이 세션/일 경계를
  넘어 번지지 않게 한다(오버나잇 갭 안전). Paper·Backtest 가 동일 timestamp 입력으로 동일 동작.
- restart-safe: 청산 timestamp 로만 판정하므로 저널 실현거래에서 복원 가능(휘발 카운터 불필요).

v1 은 hard 차단만 구현한다. `mode="conditional"`/`scope="market"|"session"`/`override_conditions`
(예: close>vwap, ema7>ema20, RSI/StochRSI 회복, 거래량 급증, 이전 손절가 회복)는 향후 지표 기반
'예외 재진입'을 위한 스키마/시그니처 자리만 예약하며 v1 에서는 무시된다(설정 시 경고 1회).
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from src.risk.models import EOD_FLATTEN, STOP_LOSS, TAKE_PROFIT, TIME_EXIT

UNIT_BARS = "trading_bars"
UNIT_MINUTES = "elapsed_minutes"
MODE_HARD = "hard"
MODE_CONDITIONAL = "conditional"     # 예약(v1 미구현)
SCOPE_SYMBOL = "symbol"
SCOPE_MARKET = "market"              # 예약(v1 미구현)
SCOPE_SESSION = "session"            # 예약(v1 미구현)


@dataclass(frozen=True)
class ReentryCooldownConfig:
    """재진입 쿨다운 설정. 전부 0/False = 비활성(기본). duration 값은 `unit` 단위로 해석."""
    enabled: bool = False
    mode: str = MODE_HARD               # "hard" | "conditional"(예약)
    scope: str = SCOPE_SYMBOL           # "symbol" | "market"|"session"(예약)
    unit: str = UNIT_BARS               # "trading_bars" | "elapsed_minutes"
    reset_on_new_session: bool = True   # 세션(거래일) 전환 시 쿨다운 상태 초기화
    timeframe_minutes: float = 5.0      # trading_bars ↔ 분 환산용(봉 길이)
    stop_loss: int = 0                  # 1차 목적: 손절 후 재진입 차단(unit 단위)
    take_profit: int = 0               # 별도 정책(기본 0 — 분리 검토)
    time_exit: int = 0
    eod_flatten: int = 0
    override_conditions: tuple[str, ...] = field(default_factory=tuple)  # 예약: 예외 재진입 조건

    def __post_init__(self):
        # 예약값(v1 미구현) 주입 시 조용한 오동작 방지 — 경고 1회.
        if self.enabled and (self.mode != MODE_HARD or self.scope != SCOPE_SYMBOL
                             or self.override_conditions):
            warnings.warn(
                "reentry_cooldown: mode/scope/override 는 v1 예약(미구현)이라 hard/symbol 로 동작한다.",
                stacklevel=2)

    def duration_for(self, reason: str | None) -> int:
        """청산 사유 → 쿨다운 지속값(unit 단위; 미지정/기타=0)."""
        return {
            STOP_LOSS: self.stop_loss,
            TAKE_PROFIT: self.take_profit,
            TIME_EXIT: self.time_exit,
            EOD_FLATTEN: self.eod_flatten,
        }.get(reason, 0)


class ReentryCooldownPolicy:
    """순수 정책. 청산 timestamp 와 현재 timestamp 만으로 재진입 차단 여부를 판정."""

    def __init__(self, config: ReentryCooldownConfig | None = None):
        self.cfg = config or ReentryCooldownConfig()

    def _elapsed_units(self, now_time, last_exit_time) -> float | None:
        """청산 이후 경과를 unit 단위로 환산. 음수(미래 청산)/누락은 None."""
        if now_time is None or last_exit_time is None:
            return None
        minutes = (now_time - last_exit_time).total_seconds() / 60.0
        if minutes < 0:
            return None
        if self.cfg.unit == UNIT_MINUTES:
            return minutes
        tf = self.cfg.timeframe_minutes or 5.0
        return minutes / tf                       # trading_bars

    def in_cooldown(self, now_time, last_exit_time, last_exit_reason) -> bool:
        """마지막 청산 후 쿨다운 창(unit) 안에 있는가."""
        dur = self.cfg.duration_for(last_exit_reason)
        if dur <= 0:
            return False
        elapsed = self._elapsed_units(now_time, last_exit_time)
        if elapsed is None:
            return False
        return elapsed < dur

    def blocked(self, now_time, last_exit_time, last_exit_reason,
                features: dict | None = None) -> bool:
        """재진입을 차단해야 하는가.

        `features`(지표 스냅샷)는 conditional 모드용 예약 인자로 v1(hard)에서는 무시한다.
        conditional 확장 시: in_cooldown 이고 override_conditions 중 하나도 충족 안 하면 차단.
        """
        if not self.cfg.enabled:
            return False
        return self.in_cooldown(now_time, last_exit_time, last_exit_reason)
