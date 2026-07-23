"""1분봉 → 5분·15분봉 결정론적 파생 + opening range (계약 §2.5 / AMENDMENT A1 · 이슈 #75).

ORB 후보 지평: 15분(primary)·5분(sensitivity) opening range. 모두 **1분봉이 단일 진실원천**이며
결정론적으로 파생한다(원천 다운로드 안 함). opening_range 는 09:30~(09:30+minutes) ET 완료 1분봉만
사용한다(미래 봉 금지).

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

from src.models import SESSION_REGULAR, Candle, regular_session_window

# 지원 버킷 크기(분). 5·15 모두 60 의 약수라 시(hour) 경계와 정렬된다(정규장 09:30 시작도 정합).
SUPPORTED_BUCKETS = (5, 15)


def _floor_exchange(ts_exchange, bucket_minutes: int):
    """거래소 로컬 시각을 bucket_minutes 버킷 시작(초/마이크로초 0)으로 내림. tzinfo 보존.

    minute-of-hour 를 bucket 으로 floor 한다(5·15 는 60 의 약수 → 09:30 등 정규장 그리드와 정합).
    """
    return ts_exchange.replace(minute=(ts_exchange.minute // bucket_minutes) * bucket_minutes,
                               second=0, microsecond=0)


def _value_tuple(c: Candle):
    return (c.open, c.high, c.low, c.close, c.volume)


def _resample_1m(candles: list[Candle], bucket_minutes: int, *,
                 min_bars: int = 1) -> list[Candle]:
    """1분봉 → bucket_minutes 분봉 결정론 파생. (symbol, session, 버킷 시작) 단위 집계.

    - open=버킷 내 첫(시간순) 1분봉 open, close=마지막 close, high=max, low=min, volume=합.
    - 파생봉 timestamp = 버킷 시작(정렬 그리드) — 첫 분이 결측이어도 버킷 시작으로 라벨.
      session 은 버킷 멤버(동일 session)에서 상속. **세션 경계를 넘는 버킷은 만들지 않는다**
      (session 을 버킷 키에 포함 → 구조적 분리).
    - min_bars: 버킷 최소 1분봉 수(기본 1 = 존재하는 분만으로 생성, =bucket_minutes 는 완전 버킷만).
      **min_bars 만으로는 마지막 진행 중 버킷을 자동으로 버리지 않는다**(라이브 엣지는 호출측 책임).
    - 중복 ts_utc(값 불일치)는 결정론적으로 (o,h,l,c,v) 최대 튜플 채택(입력 순서 무관).
    - 반환은 (ts_utc, symbol, session) 오름차순. look-ahead 없음(각 버킷은 자기 구간 1분봉만).
    """
    # 1) ts_utc 기준 결정론적 dedup(입력 순서 무관): 충돌 시 value tuple 최대 채택.
    dedup: dict[tuple[str, str], Candle] = {}
    for c in candles:
        if c.interval != "1m":
            raise ValueError(
                f"1분봉 리샘플러는 1분봉만 입력받는다(interval={c.interval!r}).")
        key = (c.symbol, c.ts_utc.isoformat())
        prev = dedup.get(key)
        if prev is None or _value_tuple(c) > _value_tuple(prev):
            dedup[key] = c

    # 2) (symbol, session, 버킷 시작) 그룹핑. session 을 키에 포함 → 세션 경계 구조적 분리.
    buckets: dict[tuple[str, str, str], list[Candle]] = {}
    for c in dedup.values():
        bstart = _floor_exchange(c.ts_exchange, bucket_minutes)
        bkey = (c.symbol, c.session, bstart.isoformat())
        buckets.setdefault(bkey, []).append(c)

    label = f"{bucket_minutes}m"
    out: list[Candle] = []
    for members in buckets.values():
        if len(members) < min_bars:
            continue
        members.sort(key=lambda x: x.ts_utc)     # 시간순(결정론)
        first, last = members[0], members[-1]
        bstart_ex = _floor_exchange(first.ts_exchange, bucket_minutes)
        bstart_utc = bstart_ex.astimezone(timezone.utc)
        out.append(Candle(
            symbol=first.symbol,
            interval=label,
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


def resample_1m(candles: list[Candle], bucket_minutes: int, *,
                min_bars: int = 1) -> list[Candle]:
    """1분봉을 지원 버킷(5·15분)으로 결정론 파생. bucket_minutes 검증 후 `_resample_1m` 위임."""
    if bucket_minutes not in SUPPORTED_BUCKETS:
        raise ValueError(
            f"지원하지 않는 버킷 {bucket_minutes} — 허용: {SUPPORTED_BUCKETS}")
    return _resample_1m(candles, bucket_minutes, min_bars=min_bars)


def resample_1m_to_5m(candles: list[Candle], *, min_bars: int = 1) -> list[Candle]:
    """1분봉 → 5분봉(계약 §2.5). 상세는 `_resample_1m`."""
    return _resample_1m(candles, 5, min_bars=min_bars)


def resample_1m_to_15m(candles: list[Candle], *, min_bars: int = 1) -> list[Candle]:
    """1분봉 → 15분봉(계약 §2.5, ORB primary 후보 지평). 상세는 `_resample_1m`."""
    return _resample_1m(candles, 15, min_bars=min_bars)


def opening_range(candles: list[Candle], *, minutes: int = 15, market: str = "US",
                  require_complete: bool = True) -> list[Candle]:
    """정규장 개장 opening range 봉을 (symbol, 거래일) 단위로 만든다(ORB 후보).

    - 창 = [정규장 개장, 개장+minutes) 거래소-로컬(US 09:30~09:44 ET, minutes=15).
      **REGULAR 세션의 그 창 안 완료 1분봉만** 사용한다 — 개장+minutes 이후 봉(미래 봉)은
      **구조적으로 제외**(계약 §2.5: '미래 봉 사용 금지', '완료된 1분봉만').
    - require_complete=True: 창의 모든 분(minutes 개)이 있어야 opening range 를 만든다(불완전이면
      해당 (symbol,거래일)은 생략 — 조작 없이 '미완'으로 둔다). False 면 존재하는 분만으로 집계.
    - open=첫 분 open, high=max, low=min, close=창의 **마지막 분** close, volume=합.
      ts_utc/ts_exchange = 개장 시각(창 시작). interval 라벨 = f"{minutes}m_or".
    - 결정론(입력 순서/중복 무관): ts_utc dedup(최대 튜플) 후 시간순 집계.
    """
    open_min, _close_min = regular_session_window(market)
    lo, hi = open_min, open_min + minutes

    dedup: dict[tuple[str, str], Candle] = {}
    for c in candles:
        if c.interval != "1m":
            raise ValueError(f"opening_range 는 1분봉만 입력받는다(interval={c.interval!r}).")
        if c.market != market or c.session != SESSION_REGULAR:
            continue
        mod = c.ts_exchange.hour * 60 + c.ts_exchange.minute
        if not (lo <= mod < hi):        # ★미래 봉/창 밖 봉 구조적 제외(look-ahead 금지)
            continue
        key = (c.symbol, c.ts_utc.isoformat())
        prev = dedup.get(key)
        if prev is None or _value_tuple(c) > _value_tuple(prev):
            dedup[key] = c

    groups: dict[tuple[str, str], list[Candle]] = {}
    for c in dedup.values():
        gkey = (c.symbol, c.ts_exchange.date().isoformat())
        groups.setdefault(gkey, []).append(c)

    label = f"{minutes}m_or"
    out: list[Candle] = []
    for members in groups.values():
        if require_complete and len(members) < minutes:
            continue        # 불완전 opening range 는 만들지 않는다(미완으로 둠)
        members.sort(key=lambda x: x.ts_utc)
        first, last = members[0], members[-1]
        # 방어: 창 밖 분이 섞이지 않았음을 재확인(look-ahead 불변식).
        assert all(lo <= (m.ts_exchange.hour * 60 + m.ts_exchange.minute) < hi
                   for m in members)
        out.append(Candle(
            symbol=first.symbol, interval=label,
            ts_utc=first.ts_utc, ts_exchange=first.ts_exchange,
            session=SESSION_REGULAR,
            open=first.open,
            high=max(m.high for m in members),
            low=min(m.low for m in members),
            close=last.close,
            volume=sum(m.volume for m in members),
            currency=first.currency, adjusted=first.adjusted, market=first.market,
        ))
    out.sort(key=lambda b: (b.ts_utc, b.symbol))
    return out
