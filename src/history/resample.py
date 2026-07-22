"""1분봉 → 5분봉 결정론적 파생 (계약 §2.5, docs/research/DATA_CONTRACT_4STRATEGY_V1.md).

범위: **오프라인 연구/저장소/품질용** 결정론 파생. 라이브 결정 경로의 5분봉은 별도 구현
(`src/engines/indicator_engine.resample`, pandas·drop_partial)을 쓰며 세부 규칙이 다르다
(계약 §2.5 주석 참조) — 두 구현의 통일은 Phase 1B/2 과제다.

원칙:
- 5분봉은 **원천 다운로드하지 않고 1분봉에서 파생**한다(단일 진실원천 = 1분봉).
- **결정론**: 입력 순서와 무관하게 동일 결과(집계 전 ts_utc 정렬 → float volume 합도 순서불변).
  현재시각/난수 미사용.
- **look-ahead 없음(버킷 내부)**: 각 버킷은 자기 구간의 1분봉만 사용한다. 단 `min_bars=1`(기본)은
  **진행 중(마감 전) 마지막 버킷도 방출**하므로, 라이브 엣지에서 쓰는 호출자는 마지막 버킷을
  제외해야 한다(라이브 경로 `indicator_engine.resample` 는 drop_partial 로 이를 처리).
- 버킷 경계 = 거래소 로컬(ET/KST) 분을 5로 floor. **버킷 키에 session 을 포함**해 한 버킷이 두
  세션에 걸치지 않음을 **구조적으로** 보장한다(정렬 그리드가 세션 경계와 정렬돼 있어도 방어적).
- 입력은 **1분봉만** 받는다(다른 interval 은 ValueError). 5분봉을 만드는 함수라 혼합 interval 은 오용.
- ⚠ DST 가을 fold(01:00–02:00 ET 중복 시각)에서는 버킷 라벨이 이론상 모호할 수 있으나, 그 구간은
  OVERNIGHT 세션이라 4전략(PRE/REG/POST)이 사용하지 않는다(계약 §2.2). 라이브 통일 시(Phase 1B) 처리.
- Toss API 호출 없음(순수 변환).
"""
from __future__ import annotations

from datetime import timezone

from src.models import Candle


def _floor5_exchange(ts_exchange):
    """거래소 로컬 시각을 5분 버킷 시작(초/마이크로초 0)으로 내림. tzinfo 보존."""
    return ts_exchange.replace(minute=(ts_exchange.minute // 5) * 5,
                               second=0, microsecond=0)


def _value_tuple(c: Candle):
    return (c.open, c.high, c.low, c.close, c.volume)


def resample_1m_to_5m(candles: list[Candle], *, min_bars: int = 1) -> list[Candle]:
    """1분봉 목록을 5분봉으로 파생. (symbol, session, 5분버킷) 단위 집계.

    - open=버킷 내 첫(시간순) 1분봉 open, close=마지막 close, high=max, low=min, volume=합.
    - 5분봉 timestamp = 버킷 시작(정렬 그리드) — 첫 분이 결측이어도 버킷 시작으로 라벨.
      session 은 버킷 멤버(동일 session)에서 상속.
    - min_bars: 버킷에 최소 이만큼 1분봉이 있어야 생성(기본 1 = 존재하는 분만으로 생성,
      5 = 완전 버킷만). **min_bars 만으로는 마지막 진행 중 버킷을 버리지 않는다**(위 docstring).
    - 중복 ts_utc(값 불일치)는 결정론적으로 (open,high,low,close,volume) 최대 튜플을 채택
      (입력 순서 무관). 정상 데이터(PK 유일)에선 발생하지 않는다.
    - 반환은 (ts_utc, symbol, session) 오름차순.
    """
    # 1) ts_utc 기준 결정론적 dedup(입력 순서 무관): 충돌 시 value tuple 최대 채택.
    #    입력은 1분봉만 허용(혼합 interval 은 dedup/버킷 키 충돌 → 명시적 거부).
    dedup: dict[tuple[str, str], Candle] = {}
    for c in candles:
        if c.interval != "1m":
            raise ValueError(
                f"resample_1m_to_5m 는 1분봉만 입력받는다(interval={c.interval!r}).")
        key = (c.symbol, c.ts_utc.isoformat())
        prev = dedup.get(key)
        if prev is None or _value_tuple(c) > _value_tuple(prev):
            dedup[key] = c

    # 2) (symbol, session, 버킷 시작) 그룹핑. session 을 키에 포함 → 세션 경계 구조적 분리.
    buckets: dict[tuple[str, str, str], list[Candle]] = {}
    for c in dedup.values():
        bstart = _floor5_exchange(c.ts_exchange)
        bkey = (c.symbol, c.session, bstart.isoformat())
        buckets.setdefault(bkey, []).append(c)

    out: list[Candle] = []
    for members in buckets.values():
        if len(members) < min_bars:
            continue
        members.sort(key=lambda x: x.ts_utc)     # 시간순(결정론)
        first, last = members[0], members[-1]
        bstart_ex = _floor5_exchange(first.ts_exchange)
        bstart_utc = bstart_ex.astimezone(timezone.utc)
        out.append(Candle(
            symbol=first.symbol,
            interval="5m",
            ts_utc=bstart_utc,
            ts_exchange=bstart_ex,
            session=first.session,               # 동일 session 멤버에서 상속(키로 보장)
            open=first.open,
            high=max(m.high for m in members),
            low=min(m.low for m in members),
            close=last.close,
            volume=sum(m.volume for m in members),
            currency=first.currency,
            adjusted=first.adjusted,
            market=first.market,
        ))
    out.sort(key=lambda b: (b.ts_utc, b.symbol, b.session))
    return out
