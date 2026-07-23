"""1분봉 세션 필터 — canonical DB 에 **쓰기 전에** 세션을 선별한다.

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §2.2 / AMENDMENT A1 · 이슈 #75 Phase 1B.

원칙(계약):
- 기본 공식 수집 세션 = **PRE + REGULAR + POST**. `OVERNIGHT` 은 기본 **제외**한다.
- 필터는 **저장(upsert) 전에** 적용한다. 사후 DELETE 방식을 쓰지 않는다.
- 허용 세션은 CLI/config 에서 **명시적으로 고정**한다(기본값 + 실제 실행값을 로그·manifest 에 기록).
- **unknown session label** 은 조용히 저장하지 않고 **fail-closed**(UnknownSessionError).
- 세션 필터 OFF(전 세션) 모드는 **명시적 opt-in(all_sessions=True)** 만 허용한다.
- 순수 함수(현재시각/난수 미사용). Toss API 호출 없음.

★ 자본 안전: 이 모듈은 시장데이터 리스트만 다룬다. 주문/broker/risk/pipeline 을 import 하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import (
    SESSION_CLOSED,
    SESSION_OVERNIGHT,
    SESSION_POST,
    SESSION_PRE,
    SESSION_REGULAR,
)

# classify_session 이 산출할 수 있는 **알려진** 세션 라벨의 전체 집합.
# 이 밖의 라벨은 계약 위반/버그 신호이므로 fail-closed 한다(조용히 저장 금지).
KNOWN_SESSIONS: frozenset[str] = frozenset({
    SESSION_PRE, SESSION_REGULAR, SESSION_POST, SESSION_OVERNIGHT, SESSION_CLOSED,
})

# 계약 §2.2 기본 공식 수집 세션(OVERNIGHT 제외).
DEFAULT_ALLOWED_SESSIONS: tuple[str, ...] = (SESSION_PRE, SESSION_REGULAR, SESSION_POST)


class UnknownSessionError(ValueError):
    """KNOWN_SESSIONS 에 없는 세션 라벨을 만났을 때(계약 밖 라벨 → 조용히 저장 금지)."""


@dataclass(frozen=True)
class SessionFilter:
    """저장 전 세션 선별기.

    - allowed: 저장을 허용할 세션 집합(정본). 기본은 DEFAULT_ALLOWED_SESSIONS.
    - all_sessions: True 면 **전 세션 저장(필터 OFF)**. 명시적 opt-in 전용.
      OFF 모드에서도 KNOWN_SESSIONS 밖 라벨은 여전히 fail-closed 한다.
    """

    allowed: frozenset[str]
    all_sessions: bool = False

    # ---- 생성자 ----
    @classmethod
    def default(cls) -> "SessionFilter":
        """계약 기본값(PRE/REGULAR/POST)."""
        return cls(allowed=frozenset(DEFAULT_ALLOWED_SESSIONS), all_sessions=False)

    @classmethod
    def all_sessions_optin(cls) -> "SessionFilter":
        """전 세션 저장(필터 OFF) — 명시적 opt-in. KNOWN_SESSIONS 로 여전히 검증."""
        return cls(allowed=frozenset(KNOWN_SESSIONS), all_sessions=True)

    @classmethod
    def from_spec(cls, spec: str | None, *, all_sessions: bool = False) -> "SessionFilter":
        """CLI/config 스펙에서 필터를 만든다.

        - all_sessions=True → 전 세션 저장(spec 무시, opt-in). spec 이 함께 오면 거부(모순).
        - spec=None → 기본값(PRE/REGULAR/POST).
        - spec="PRE,REGULAR,POST" → 그 집합만 허용. 각 라벨은 KNOWN_SESSIONS 여야 한다
          (오타/미지원 라벨은 즉시 ValueError — 조용히 빈 집합으로 새지 않음).
        """
        if all_sessions:
            if spec is not None:
                raise ValueError(
                    "all_sessions(전 세션 opt-in)와 명시 세션 목록은 함께 쓸 수 없다.")
            return cls.all_sessions_optin()
        if spec is None:
            return cls.default()
        labels = [s.strip().upper() for s in spec.split(",") if s.strip()]
        if not labels:
            raise ValueError("빈 세션 스펙 — 허용 세션을 명시하거나 all_sessions 를 opt-in 하라.")
        unknown = sorted(set(labels) - KNOWN_SESSIONS)
        if unknown:
            raise ValueError(
                f"미지원 세션 라벨 {unknown} — 허용: {sorted(KNOWN_SESSIONS)}")
        return cls(allowed=frozenset(labels), all_sessions=False)

    # ---- 적용 ----
    def partition(self, candles: list) -> tuple[list, dict[str, int]]:
        """(kept, excluded_by_session) 로 분할한다. **입력 순서를 보존**(결정론).

        - kept: allowed 세션 캔들만(입력 순서 유지).
        - excluded_by_session: 제외된 세션별 행수(run/symbol 집계용).
        - KNOWN_SESSIONS 밖 라벨을 만나면 즉시 UnknownSessionError(fail-closed) — kept 를
          부분 반환하지 않는다(조용한 부분 저장 방지).
        """
        kept: list = []
        excluded: dict[str, int] = {}
        for c in candles:
            sess = c.session
            if sess not in KNOWN_SESSIONS:
                raise UnknownSessionError(
                    f"알 수 없는 세션 라벨 {sess!r}(symbol={getattr(c, 'symbol', '?')}, "
                    f"ts_utc={getattr(c, 'ts_utc', '?')}) — 저장 거부(fail-closed).")
            if sess in self.allowed:
                kept.append(c)
            else:
                excluded[sess] = excluded.get(sess, 0) + 1
        return kept, excluded

    def as_dict(self) -> dict:
        """로그/manifest 기록용 요약(결정론적 정렬)."""
        return {
            "allowed_sessions": sorted(self.allowed),
            "all_sessions": self.all_sessions,
            "default_sessions": list(DEFAULT_ALLOWED_SESSIONS),
        }
