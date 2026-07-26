"""1분봉 → 5분·15분봉 결정론적 파생 + opening range — **연구·라이브·replay 공용 canonical core**.

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §2.5 / AMENDMENT A1(#75) / **A2(#76)**.

ORB 후보 지평: 15분(primary)·5분(sensitivity) opening range. 모두 **1분봉이 단일 진실원천**이며
결정론적으로 파생한다(원천 다운로드 안 함). opening_range 는 09:30~(09:30+minutes) ET 완료 1분봉만
사용한다(미래 봉 금지).

**단일 소스화(이슈 #76)**: 이 모듈이 유일한 집계 권위다. 라이브 결정 경로
(`src/engines/indicator_engine.resample`)는 pandas DataFrame ↔ Candle 을 오가는 **thin adapter**
이며 집계·dedup·세션·버킷 경계를 스스로 계산하지 않는다. 따라서 연구(candidate_stream)·
라이브(paper_loop)·replay 는 동일 입력에서 **동일 직렬화 결과**를 낸다.

원칙:
- 5분봉은 **원천 다운로드하지 않고 1분봉에서 파생**한다(단일 진실원천 = 1분봉).
- **결정론**: 입력 순서와 무관하게 동일 결과(집계 전 ts_utc 정렬 → float volume 합도 순서불변).
  현재시각/난수 미사용 — **wall clock 에 암묵적으로 의존하지 않는다**.
- **look-ahead 없음(버킷 내부)**: 각 버킷은 자기 구간의 1분봉만 사용한다.
- **완료(closed) 판정은 명시적**이다(#76). 두 축을 혼동하지 않는다:
  · `closed_only` = **시간축** — 버킷 구간이 끝났다는 증거가 있는가(진행 중 버킷 배제).
  · `min_bars` = **커버리지축** — 그 구간에 1분봉이 몇 개나 있는가(데이터 공백 필터).
  버킷이 닫혔어도 결측 분이 있을 수 있고(공백), 분이 다 차 있어도 아직 안 닫혔을 수 있다.
- **완료 경계(frontier)**: `closed_only=True` 면 버킷 [t, t+Δ) 는 `t+Δ <= frontier` 일 때만 방출한다.
  `evaluation_time` 을 주면 그 시각이 frontier 이고, 없으면 **그 symbol 의 마지막 1분봉 ts_utc**
  (= 시간이 그 지점까지 진행됐다는 데이터 증거)를 쓴다. 마지막 1분봉이 **형성 중일 수 있으므로
  +1분 하지 않는다**(보수적) — 그 결과 기존 라이브 `drop_partial=True` 와 관측 동치다.
- **frontier 는 symbol 별**이다 → `resample(A∪B) == resample(A) ∪ resample(B)`(합성 가능).
  라이브가 종목별로 리샘플하므로 연구가 여러 종목을 한 번에 로드해도 결과가 같다. 시장 전체
  시계가 필요하면 `evaluation_time` 으로 **명시**한다(암묵적 cross-symbol 누출 금지).
  stale 종목의 마지막 버킷 지연은 **#44 의 범위**이며 여기서 바꾸지 않는다.
- 버킷 경계 = 거래소 로컬(ET/KST) 분을 Δ로 floor(**market-aware**, US 하드코딩 없음).
  **버킷 키에 session 을 포함**해 한 버킷이 두 세션에 걸치지 않음을 **구조적으로** 보장한다.
- 입력은 **1분봉만** 받는다(다른 interval 은 ValueError). 5분봉을 만드는 함수라 혼합 interval 은 오용.
- ⚠ DST 가을 fold(01:00–02:00 ET 중복 시각): 로컬 라벨은 같아도 **UTC offset 이 달라 버킷 키가
  분리**된다(두 시간대의 봉이 한 버킷으로 합쳐지지 않음). 해당 구간은 OVERNIGHT 이라 4전략이
  쓰지 않지만(계약 §2.2) 계약을 구조적으로 보장한다. 버킷 종료 시각은 **UTC 산술**로 계산해
  전환 시각을 넘는 버킷에서도 길이가 왜곡되지 않는다.
- Toss API 호출 없음(순수 변환).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import SESSION_REGULAR, Candle, regular_session_window

# 지원 버킷 크기(분). 5·15 모두 60 의 약수라 시(hour) 경계와 정렬된다(정규장 09:30 시작도 정합).
SUPPORTED_BUCKETS = (5, 15)

# 호출자 규칙 문자열 → 버킷 분. 라이브/replay 가 쓰는 pandas 스타일("5min")과 canonical
# interval 라벨("5m")을 모두 받되, **그 외는 fail-closed**(임의 rule 로 다른 집계가 생기지 않게).
_RULE_TO_MINUTES = {"5min": 5, "5m": 5, "15min": 15, "15m": 15}


def bucket_minutes_from_rule(rule: str | int) -> int:
    """리샘플 rule("5min"/"5m"/5) → 버킷 분. 지원 밖은 ValueError(fail-closed, #76)."""
    if isinstance(rule, bool):      # bool 은 int 하위형 — 오용 차단
        raise ValueError(f"지원하지 않는 resample rule {rule!r} — 허용: {sorted(_RULE_TO_MINUTES)}")
    if isinstance(rule, int):
        if rule not in SUPPORTED_BUCKETS:
            raise ValueError(f"지원하지 않는 버킷 {rule} — 허용: {SUPPORTED_BUCKETS}")
        return rule
    key = str(rule).strip().lower()
    if key not in _RULE_TO_MINUTES:
        raise ValueError(
            f"지원하지 않는 resample rule {rule!r} — 허용: {sorted(_RULE_TO_MINUTES)}")
    return _RULE_TO_MINUTES[key]


def _frontiers(candles: list[Candle],
               evaluation_time: datetime | None) -> dict[str, datetime]:
    """symbol → 완료 경계.

    - `evaluation_time`(명시적 시장 전체 시계) 이 있으면 **모든 symbol 이 그 값**을 쓴다.
    - 없으면 **symbol 별 최대 ts_utc**(그 종목 데이터가 증명하는 시간 진행).

    ★ symbol 별인 이유(#76 독립 리뷰 B1): 라이브(`paper_loop`)는 **종목별로** 리샘플한다.
      frontier 를 입력 전체의 최대 ts 로 잡으면 `resample(A∪B) != resample(A) ∪ resample(B)`
      가 되어(신선한 B 가 stale 한 A 의 버킷을 닫아버림) 연구가 여러 종목을 한 번에 로드하는
      순간 다시 연구≠라이브가 된다. 종목별 frontier 는 **합성 가능(compositional)** 하다.
      시장 전체 시계가 필요한 호출자는 `evaluation_time` 으로 **명시**한다.
    """
    if evaluation_time is not None:
        if evaluation_time.tzinfo is None:
            raise ValueError("evaluation_time 은 timezone-aware 여야 한다(naive 금지).")
        at = evaluation_time.astimezone(timezone.utc)
        return {c.symbol: at for c in candles}
    out: dict[str, datetime] = {}
    for c in candles:
        prev = out.get(c.symbol)
        if prev is None or c.ts_utc > prev:
            out[c.symbol] = c.ts_utc
    return out


def _floor_exchange(ts_exchange, bucket_minutes: int):
    """거래소 로컬 시각을 bucket_minutes 버킷 시작(초/마이크로초 0)으로 내림. tzinfo 보존.

    minute-of-hour 를 bucket 으로 floor 한다(5·15 는 60 의 약수 → 09:30 등 정규장 그리드와 정합).
    """
    return ts_exchange.replace(minute=(ts_exchange.minute // bucket_minutes) * bucket_minutes,
                               second=0, microsecond=0)


def _value_tuple(c: Candle):
    return (c.open, c.high, c.low, c.close, c.volume)


def _resample_1m(candles: list[Candle], bucket_minutes: int, *,
                 min_bars: int = 1, closed_only: bool = False,
                 evaluation_time: datetime | None = None) -> list[Candle]:
    """1분봉 → bucket_minutes 분봉 결정론 파생. (symbol, session, 버킷 시작) 단위 집계.

    - open=버킷 내 첫(시간순) 1분봉 open, close=마지막 close, high=max, low=min, volume=합.
    - 파생봉 timestamp = 버킷 시작(정렬 그리드) — 첫 분이 결측이어도 버킷 시작으로 라벨.
      session 은 버킷 멤버(동일 session)에서 상속. **세션 경계를 넘는 버킷은 만들지 않는다**
      (session 을 버킷 키에 포함 → 구조적 분리).
    - min_bars(**커버리지축**): 버킷 최소 1분봉 수(기본 1 = 존재하는 분만으로 생성,
      =bucket_minutes 는 결측 없는 버킷만). 진행 중 버킷을 버리는 수단이 **아니다**.
    - closed_only(**시간축**): True 면 `버킷 종료 <= frontier` 인 버킷만 방출한다(진행 중 배제).
      frontier = `evaluation_time` 또는 입력 최대 ts_utc(모듈 docstring 참조). 버킷 종료는
      UTC 산술(`시작 + Δ분`)로 계산해 DST 전환 구간에서도 길이가 일정하다.
      `evaluation_time` 은 `closed_only=True` 에서만 의미가 있으며, False 와 함께 주면
      **조용히 무시하지 않고 ValueError**(정책이 호출자마다 몰래 달라지지 않게).
    - 중복 ts_utc(값 불일치)는 결정론적으로 (o,h,l,c,v) 최대 튜플 채택(입력 순서 무관).
    - 반환은 (ts_utc, symbol, session) 오름차순. look-ahead 없음(각 버킷은 자기 구간 1분봉만).
    """
    if evaluation_time is not None and not closed_only:
        raise ValueError(
            "evaluation_time 은 closed_only=True 에서만 쓴다(무시되는 인자 금지).")
    if min_bars < 1:
        raise ValueError(f"min_bars 는 1 이상이어야 한다(got {min_bars}).")
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

    # 2) 완료 경계(시간축). dedup 결과 기준 — 중복 입력이 frontier 를 바꾸지 않는다.
    frontiers = _frontiers(list(dedup.values()), evaluation_time) if closed_only else {}

    # 3) (symbol, session, 버킷 시작) 그룹핑. session 을 키에 포함 → 세션 경계 구조적 분리.
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
        if closed_only:
            # 버킷 종료는 UTC 산술(DST 전환에도 Δ분 고정). frontier 는 **그 symbol** 기준.
            frontier = frontiers.get(first.symbol)
            if frontier is None or bstart_utc + timedelta(minutes=bucket_minutes) > frontier:
                continue                          # 진행 중(또는 증거 없음) → 방출하지 않음
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
                min_bars: int = 1, closed_only: bool = False,
                evaluation_time: datetime | None = None) -> list[Candle]:
    """1분봉을 지원 버킷(5·15분)으로 결정론 파생. bucket_minutes 검증 후 `_resample_1m` 위임."""
    if bucket_minutes not in SUPPORTED_BUCKETS:
        raise ValueError(
            f"지원하지 않는 버킷 {bucket_minutes} — 허용: {SUPPORTED_BUCKETS}")
    return _resample_1m(candles, bucket_minutes, min_bars=min_bars,
                        closed_only=closed_only, evaluation_time=evaluation_time)


def resample_1m_to_5m(candles: list[Candle], *, min_bars: int = 1,
                      closed_only: bool = False,
                      evaluation_time: datetime | None = None) -> list[Candle]:
    """1분봉 → 5분봉(계약 §2.5). 상세는 `_resample_1m`."""
    return _resample_1m(candles, 5, min_bars=min_bars,
                        closed_only=closed_only, evaluation_time=evaluation_time)


def resample_1m_to_15m(candles: list[Candle], *, min_bars: int = 1,
                       closed_only: bool = False,
                       evaluation_time: datetime | None = None) -> list[Candle]:
    """1분봉 → 15분봉(계약 §2.5, ORB primary 후보 지평). 상세는 `_resample_1m`."""
    return _resample_1m(candles, 15, min_bars=min_bars,
                        closed_only=closed_only, evaluation_time=evaluation_time)


def opening_range(candles: list[Candle], *, minutes: int = 15, market: str = "US",
                  require_complete: bool = True, closed_only: bool = False,
                  evaluation_time: datetime | None = None) -> list[Candle]:
    """정규장 개장 opening range 봉을 (symbol, 거래일) 단위로 만든다(ORB 후보).

    - 창 = [정규장 개장, 개장+minutes) 거래소-로컬(US 09:30~09:44 ET, minutes=15).
      **REGULAR 세션의 그 창 안 완료 1분봉만** 사용한다 — 개장+minutes 이후 봉(미래 봉)은
      **구조적으로 제외**(계약 §2.5: '미래 봉 사용 금지', '완료된 1분봉만').
    - require_complete(**커버리지축**)=True: 창의 모든 분(minutes 개)이 있어야 opening range 를
      만든다(불완전이면 해당 (symbol,거래일)은 생략 — 조작 없이 '미완'으로 둔다).
      False 면 존재하는 분만으로 집계.
    - closed_only(**시간축**, #76)=True: 창이 **끝났다는 증거**가 있을 때만 방출한다
      (`창 종료 <= frontier`, frontier = `evaluation_time` 또는 입력 최대 ts_utc).
      마지막 분 봉이 형성 중인 상태에서 opening range 를 확정하지 않으려는 호출자용.
      `_resample_1m` 과 **같은 계약**이다(축을 호출자마다 다르게 두지 않는다).
    - open=존재하는 첫 분 open, high=max, low=min, close=창의 **마지막 분** close, volume=합.
      **ts_utc/ts_exchange = 개장 시각(창 시작)** — 첫 분이 결측이어도 개장 시각으로 라벨한다
      (`_resample_1m` 의 그리드 시작 규칙과 동일). require_complete=False 일 때 라벨이 존재하는
      첫 봉으로 밀리던 문제를 #76 에서 문서와 일치시켰다. interval 라벨 = f"{minutes}m_or".
    - 결정론(입력 순서/중복 무관): ts_utc dedup(최대 튜플) 후 시간순 집계.
    """
    if evaluation_time is not None and not closed_only:
        raise ValueError(
            "evaluation_time 은 closed_only=True 에서만 쓴다(무시되는 인자 금지).")
    if minutes < 1:
        raise ValueError(f"opening range 길이는 1분 이상이어야 한다(got {minutes}).")
    open_min, _close_min = regular_session_window(market)
    lo, hi = open_min, open_min + minutes

    # frontier 는 **필터 전 전체 입력** 기준(창 밖 봉도 시간 진행의 증거다). symbol 별.
    frontiers = _frontiers(candles, evaluation_time) if closed_only else {}

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
        # 창 시작(=그 거래일의 개장 시각) — 거래소 로컬로 확정한 뒤 UTC 로 환산한다.
        # 첫 분이 결측이어도 라벨은 개장 시각이다(`_resample_1m` 의 그리드 시작 규칙과 동일).
        window_start_ex = first.ts_exchange.replace(hour=lo // 60, minute=lo % 60,
                                                    second=0, microsecond=0)
        window_start_utc = window_start_ex.astimezone(timezone.utc)
        if closed_only:
            # 창 종료 = 시작 + minutes 를 UTC 산술로 — 끝났다는 증거가 없으면 방출 안 함.
            frontier = frontiers.get(first.symbol)
            if frontier is None or window_start_utc + timedelta(minutes=minutes) > frontier:
                continue
        # 방어: 창 밖 분이 섞이지 않았음을 재확인(look-ahead 불변식). 평문 assert 는 `python -O`
        # 에서 제거되므로 명시적 raise 로 둔다(불변식 '검증' 의도 보존).
        if not all(lo <= (m.ts_exchange.hour * 60 + m.ts_exchange.minute) < hi
                   for m in members):
            raise RuntimeError(
                "opening_range look-ahead 불변식 위반: 창 밖 봉 혼입(내부 버그).")
        out.append(Candle(
            symbol=first.symbol, interval=label,
            ts_utc=window_start_utc, ts_exchange=window_start_ex,
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
