"""과거 캔들 → **후보 단위 스트림** 추출기 (read-only 연구 경로).

목적: H2/H2-b 처럼 "어떤 컴포넌트가 예측력을 갖는가"를 묻는 분석에는 거래/손익이 아니라
**(종목, 봉) 시점마다의 candidate + score_components + forward return** 이 필요하다.

기존 backtest 경로로는 이걸 못 얻는다: `src/backtest/engine.py` 의 `ScreenerEntryStrategy`
는 live 와 동일한 IndicatorEngine/UniverseScreener 를 쓰지만 `Candidate` 를 버리고
`reasons` 만 반환한다. 그래서 score/features/score_components 가 사라진다.

★ 자본 안전: 이 모듈은 **캔들 read + 계산만** 한다. 주문/broker/AI/risk 를 import 하지 않고,
   매매 로직·스코어 가중치·runtime config 를 일절 건드리지 않는다.

--------------------------------------------------------------------------------
live 재현 규칙 (us-phase6-s001 실측으로 확정)
--------------------------------------------------------------------------------
live 경로(`src/live/paper_loop.py`)는 매 사이클
    store.query(sym,"1m") → resample(...,"5min", drop_partial=True) → IndicatorEngine → screener
를 수행한다. 지표는 causal 이므로 **전체 5m 시리즈에 대해 한 번 계산한 뒤 행 t 를 쓰는 것**과
**t 까지만 주고 계산한 것**이 같다. 이 모듈은 그 성질을 이용해 지표를 1회만 계산한다.

주의 — 저널의 `cycle_ts` 는 **봉 라벨이 아니라 예정된 wall-clock 시각**이다. 루프 지연에 따라
그 시점에 "마지막 완결 5m 버킷"이 달라진다(s001: 267 사이클은 [T,T+5), 9 사이클은 [T-5,T)).
따라서 라이브 대조는 시계가 아니라 **봉 지문(indicator snapshot)** 으로 정렬해야 한다
(`align_live_outcome`). 이 규칙을 지키면 s001 재현 오차가 0 이 된다.

--------------------------------------------------------------------------------
forward return 규약
--------------------------------------------------------------------------------
- 앵커 = 결정 봉(라벨 T)의 **종가**. 이 종가는 실제로 T+bar_minutes 에 확정된다.
- 지평 h 분의 목표 봉 라벨 = T + h  (그 봉의 종가는 앵커 종가로부터 정확히 h 분 뒤 확정).
- **strict look-ahead**: 목표 봉 라벨 > T 인 봉만 사용한다.
- **세션 경계 clip**: 앵커와 같은 거래일(ET date)을 벗어나면 미완결.
- 데이터 공백으로 목표 봉이 없으면 미완결. **조용한 drop 도, 0 대입도 하지 않는다** —
  `complete=False` 로 표기하고 집계에서 제외하는 것은 소비자 책임(`completed_returns`).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from src.engines.indicator_engine import (IndicatorEngine, candles_to_frame,
                                          frame_to_candles as _frame_to_candles,
                                          resample)
from src.models import ET, Candle, classify_session
from src.universe.screener import Candidate, ScreenerConfig, SymbolData, UniverseScreener, leverage_for

# live(paper_loop)와 동일한 결정 타임프레임.
DECISION_RULE = "5min"
DECISION_BAR_MINUTES = 5
# 결정봉 interval 라벨. 라이브는 "5m" 를 쓰는데 이 모듈만 rule 문자열("5min")을 라벨로 써서
# 두 경로의 Candle 이 달랐다 — canonical 라벨로 통일한다(이슈 #76 parity).
DECISION_INTERVAL = "5m"

# 컴포넌트 이름(스크리너 score_components 키와 동일).
COMPONENT_NAMES = ("trend", "momentum", "volume", "vwap_position", "volatility")

# 지표 지문 — 라이브 outcome 을 어느 봉에서 나온 것인지 식별하는 데 쓴다.
# stochrsi 는 NaN 이 흔해 제외하고, 결정적으로 채워지는 값만 쓴다.
FINGERPRINT_KEYS = ("close", "ema7", "ema20", "rsi14", "vwap", "bb_upper", "bb_lower")


@dataclass(frozen=True)
class ForwardReturn:
    """단일 지평의 forward return. complete=False 면 집계에서 제외되어야 한다."""
    horizon_min: int
    complete: bool
    return_pct: float | None = None
    anchor_price: float | None = None
    exit_price: float | None = None
    exit_ts_utc: str | None = None
    incomplete_reason: str | None = None   # session_end | no_bar | gap_too_large | bad_anchor
    # 실제로 실현된 지평(분). 목표 라벨에 봉이 없으면 그 이후 첫 봉을 쓰므로
    # 요청 지평보다 최대 한 봉(DECISION_BAR_MINUTES) 길 수 있다. 소비자가 필요하면
    # `realized_horizon_min != horizon_min` 인 관측을 걸러낼 수 있게 기록한다.
    realized_horizon_min: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CandidateObservation:
    """한 (종목, 결정봉) 관측. candidate 가 없으면 생성하지 않는다."""
    symbol: str
    market: str
    bar_ts_utc: str            # 결정 봉 라벨(UTC ISO)
    bar_ts_exchange: str       # 거래소 로컬(ET) ISO
    et_date: str               # 거래일(YYYY-MM-DD) — LOSO/세션 clip 기준
    session: str
    leverage_factor: int
    close: float
    candidate_score: float
    candidate_type: str
    rank: int
    risk_flags: list[str] = field(default_factory=list)
    # score 분해
    weighted_component_sum: float | None = None
    penalty_score: float | None = None
    pre_clamp_score: float | None = None
    components: dict = field(default_factory=dict)   # name -> {normalized_score, weight, weighted_score}
    # volume 계측
    volume: float | None = None
    volume_sma20: float | None = None
    volume_ratio: float | None = None
    # forward
    forwards: dict = field(default_factory=dict)     # horizon_min(str) -> ForwardReturn dict

    def to_dict(self) -> dict:
        return asdict(self)

    def feature_value(self, name: str) -> float | None:
        """분석용 feature 접근자. 컴포넌트는 normalized_score 를 쓴다."""
        if name == "candidate_score":
            return self.candidate_score
        if name == "volume_ratio":
            return self.volume_ratio
        comp = self.components.get(name)
        return None if comp is None else comp.get("normalized_score")

    def forward(self, horizon_min: int) -> dict | None:
        return self.forwards.get(str(horizon_min))


def build_decision_bars(candles_1m: list[Candle], symbol: str) -> list[Candle]:
    """1m 캔들 → live 와 동일한 5m 결정 봉.

    live(`paper_loop`)는 매 사이클 `resample(..., drop_partial=True)` 로 **진행 중 버킷을
    버린다.** 재구성도 같은 규칙을 쓴다.

    이전 구현은 `drop_partial=False` 로 만들고 "마지막 부분 버킷은 forward return 의
    미완결 처리로 흡수된다"고 적었으나 **사실이 아니었다.** 그 버킷은 (a) 스크리너에
    들어가 라이브가 결코 만들지 않는 관측을 1건 만들고, (b) 다른 앵커의 `exit_price` 로
    쓰이면서 `complete=True` 로 표기됐다(`compute_forward_returns` 는 목표 봉의 존재만
    보고 그 봉이 완결 버킷인지는 보지 않는다).

    시리즈 전체의 마지막 버킷 하나뿐이라 통계적 영향은 작지만, 이 모듈이 내세우는
    "미완결은 0 대입도 조용한 drop 도 하지 않는다" 계약이 정확히 그 지점에서 샜다.
    live 와 같은 규칙으로 맞춰 근본에서 없앤다.
    """
    if not candles_1m:
        return []
    df5 = resample(candles_to_frame(candles_1m), DECISION_RULE, drop_partial=True)
    return _frame_to_candles(df5, symbol, interval=DECISION_INTERVAL)


def compute_forward_returns(bars: list[Candle], idx: int,
                            horizons_min: tuple[int, ...]) -> dict:
    """결정 봉 idx 기준 forward return 들. strict look-ahead + 세션 clip.

    목표 봉 라벨 = bars[idx].ts_utc + horizon. 정확히 그 라벨이 없으면 **그 이후 첫 봉**을
    쓰되, 한 봉 간격(DECISION_BAR_MINUTES)을 넘게 벌어졌으면 데이터 공백으로 보고 미완결.
    """
    anchor = bars[idx]
    anchor_price = anchor.close
    anchor_date = anchor.ts_exchange.date()
    ts_list = [b.ts_utc for b in bars]
    out: dict = {}
    for h in horizons_min:
        target = anchor.ts_utc + timedelta(minutes=h)
        # strict look-ahead: 앵커보다 뒤(>)인 봉만 후보. bisect_right(ts, anchor) 이후 구간.
        lo = bisect_right(ts_list, anchor.ts_utc)
        j = bisect_left(ts_list, target, lo=lo)
        if j >= len(bars):
            out[str(h)] = ForwardReturn(h, False, anchor_price=anchor_price,
                                        incomplete_reason="no_bar").to_dict()
            continue
        exit_bar = bars[j]
        if exit_bar.ts_exchange.date() != anchor_date:
            # 세션 경계 clip — 다음 거래일 봉으로 수익률을 만들지 않는다(오버나이트 혼입 금지).
            out[str(h)] = ForwardReturn(h, False, anchor_price=anchor_price,
                                        incomplete_reason="session_end").to_dict()
            continue
        drift = (exit_bar.ts_utc - target).total_seconds() / 60.0
        if drift > DECISION_BAR_MINUTES:
            out[str(h)] = ForwardReturn(h, False, anchor_price=anchor_price,
                                        incomplete_reason="gap_too_large").to_dict()
            continue
        if not anchor_price:
            # 종가 0/None — 수익률 정의 불가. 다른 미완결 경로와 같이 사유를 남긴다.
            out[str(h)] = ForwardReturn(h, False, anchor_price=anchor_price,
                                        incomplete_reason="bad_anchor").to_dict()
            continue
        realized = int(round((exit_bar.ts_utc - anchor.ts_utc).total_seconds() / 60.0))
        ret = (exit_bar.close - anchor_price) / anchor_price * 100.0
        out[str(h)] = ForwardReturn(
            h, True, return_pct=ret, anchor_price=anchor_price,
            exit_price=exit_bar.close, exit_ts_utc=exit_bar.ts_utc.isoformat(),
            realized_horizon_min=realized).to_dict()
    return out


def _components_from_features(feats: dict) -> dict:
    comps = (feats or {}).get("score_components") or {}
    out = {}
    for name in COMPONENT_NAMES:
        c = comps.get(name)
        if not c:
            continue
        out[name] = {"normalized_score": c.get("normalized_score"),
                     "weight": c.get("weight"),
                     "weighted_score": c.get("weighted_score")}
    return out


def _observation(symbol: str, market: str, bar: Candle, cand: Candidate,
                 leverage: int, forwards: dict) -> CandidateObservation:
    feats = cand.features or {}
    return CandidateObservation(
        symbol=symbol, market=market,
        bar_ts_utc=bar.ts_utc.isoformat(),
        bar_ts_exchange=bar.ts_exchange.isoformat(),
        et_date=bar.ts_exchange.date().isoformat(),
        session=bar.session, leverage_factor=leverage, close=bar.close,
        candidate_score=cand.score, candidate_type=cand.candidate_type, rank=cand.rank,
        risk_flags=list(cand.risk_flags or []),
        weighted_component_sum=feats.get("weighted_component_sum"),
        penalty_score=feats.get("penalty_score"),
        pre_clamp_score=feats.get("pre_clamp_score"),
        components=_components_from_features(feats),
        volume=feats.get("volume_last"), volume_sma20=feats.get("volume_sma20"),
        volume_ratio=feats.get("volume_ratio"),
        forwards=forwards)


class CandidateStreamExtractor:
    """과거 1m 캔들에서 후보 스트림을 뽑는다. live 와 동일한 지표/스크리너를 재사용."""

    def __init__(self, screener: UniverseScreener | None = None,
                 engine: IndicatorEngine | None = None,
                 horizons_min: tuple[int, ...] = (30, 60)):
        self._screener = screener or UniverseScreener()
        self._engine = engine or IndicatorEngine()
        self._horizons = horizons_min

    # -- 내부: 5m 봉 + 전체 지표(causal 이므로 1회 계산) --
    def prepare(self, candles_1m: list[Candle], symbol: str):
        bars = build_decision_bars(candles_1m, symbol)
        ind = self._engine.compute(bars) if bars else pd.DataFrame()
        return bars, ind

    def extract(self, symbol: str, candles_1m: list[Candle], *,
                market: str = "US", leverage_factor: int | None = None,
                include_excluded: bool = False) -> list[CandidateObservation]:
        """모든 결정 봉에 대해 스크리너를 돌려 후보 관측을 만든다.

        include_excluded=False(기본)면 live 와 동일하게 LONG_SETUP 후보만 남긴다.

        ⚠ 현재 US 전용이다. **봉 생성 경로의 ET 고정은 이슈 #76 에서 해소됐다**(세션/거래소
        시각/market 은 원본 1분봉에서 상속한다 — `tests/test_resampler_parity.py` 의 KR 케이스).
        게이트를 유지하는 이유는 이제 **봉이 아니라 그 위층**이다: 스크리너 파라미터·레버리지
        분류·거래일 경계(`et_date` 기반 forward return clip)가 US 기준으로만 검증됐고,
        KR 캘린더(휴장·조기폐장)와 유동성 가정도 미검증이다. 조용히 틀린 값을 내느니 크게
        실패시킨다. KR 지원은 별도 검증과 함께 연다.
        """
        if market != "US":
            raise NotImplementedError(
                f"market={market!r} 미지원 — 봉 생성은 market-aware 로 통일됐으나(#76) "
                "스크리너·거래일 경계·KR 캘린더가 미검증이다(조용한 오분류 방지).")
        bars, ind = self.prepare(candles_1m, symbol)
        if not bars or ind.empty:
            return []
        lev = leverage_for(symbol) if leverage_factor is None else leverage_factor
        out: list[CandidateObservation] = []
        for i in range(len(ind)):
            cand = self.screen_at(symbol, ind, i, lev, include_excluded=include_excluded)
            if cand is None:
                continue
            fwd = compute_forward_returns(bars, i, self._horizons)
            out.append(_observation(symbol, market, bars[i], cand, lev, fwd))
        return out

    def screen_at(self, symbol: str, ind: pd.DataFrame, i: int, leverage: int,
                  *, include_excluded: bool = False) -> Candidate | None:
        """봉 i 시점의 후보. **i 이하 행만** 넘겨 look-ahead 를 구조적으로 차단한다."""
        window = ind.iloc[: i + 1]
        sd = SymbolData(symbol, window, leverage)
        if include_excluded:
            res = self._screener.screen_all([sd])
        else:
            res = self._screener.screen([sd])
        if not res:
            return None
        cand = res[0]
        if cand.symbol != symbol:
            return None
        return cand


# ---------------------------------------------------------------------------
# 라이브 대조(패리티) 지원
# ---------------------------------------------------------------------------

def fingerprint_distance(row: pd.Series, snapshot: dict) -> float:
    """지표 행 ↔ 라이브 indicator_snapshot 의 최대 절대차. 못 비교하면 inf."""
    diffs = []
    for k in FINGERPRINT_KEYS:
        sv = snapshot.get(k)
        if sv is None or k not in row:
            continue
        rv = row[k]
        if rv is None or (isinstance(rv, float) and rv != rv):   # NaN
            continue
        diffs.append(abs(float(rv) - float(sv)))
    return max(diffs) if diffs else float("inf")


def align_live_outcome(ind: pd.DataFrame, snapshot: dict, *,
                       tolerance: float = 1e-9) -> int | None:
    """라이브 outcome 이 **어느 봉에서 나왔는지** 지표 지문으로 찾는다.

    저널 cycle_ts 는 예정 시각일 뿐이고 루프 지연에 따라 실제 사용 봉이 달라지므로
    시계 기반 매칭은 재현에 실패한다(s001 에서 9건). 지문 매칭은 그 지연과 무관하다.

    **모호하면 고르지 않는다.** 2등 후보도 tolerance 이내면 어느 봉인지 단정할 수 없으므로
    None 을 돌려준다. 패리티 전체가 "정렬이 옳다"에 얹혀 있는데, 최소거리 하나만 고르면
    잘못된 봉에 확신을 갖고 붙어도 드러나지 않는다. 애매한 건 정렬 실패로 세는 편이
    "정렬 실패 0" 이라는 지표를 훨씬 강하게 만든다.
    """
    best_i, best_d = None, float("inf")
    second_d = float("inf")
    for i in range(len(ind)):
        d = fingerprint_distance(ind.iloc[i], snapshot)
        if d < best_d:
            best_i, best_d, second_d = i, d, best_d
        elif d < second_d:
            second_d = d
    if best_i is None or best_d > tolerance:
        return None
    if second_d <= tolerance:
        return None      # 모호 — 두 봉이 똑같이 잘 맞는다
    return best_i


def completed_returns(observations: list[CandidateObservation], horizon_min: int,
                      feature: str) -> tuple[list[float], list[float]]:
    """(feature 값, forward return) 쌍 — **완결된 지평만**. 미완결은 자동 제외.

    0 대입이나 조용한 drop 없이, complete=True 이고 feature 값이 존재하는 관측만 남긴다.
    """
    xs: list[float] = []
    ys: list[float] = []
    for o in observations:
        fwd = o.forward(horizon_min)
        if not fwd or not fwd.get("complete"):
            continue
        v = o.feature_value(feature)
        r = fwd.get("return_pct")
        if v is None or r is None:
            continue
        xs.append(float(v))
        ys.append(float(r))
    return xs, ys


def load_1m_candles(db_path: str, symbol: str, *, table: str = "candle_history",
                    start: datetime | None = None,
                    end: datetime | None = None) -> list[Candle]:
    """market_history / 세션 market DB 양쪽에서 1m 캔들을 읽는다(read-only).

    세션 DB 는 테이블명이 `candle`, 통합 히스토리는 `candle_history` 로 다르다.
    """
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = (f"SELECT symbol, ts_utc, ts_exchange, session, open, high, low, close, "
               f"volume, currency, adjusted FROM {table} "
               f"WHERE symbol=? AND interval='1m'")
        args: list = [symbol]
        if start is not None:
            sql += " AND ts_utc >= ?"
            args.append(start.isoformat())
        if end is not None:
            sql += " AND ts_utc < ?"
            args.append(end.isoformat())
        sql += " ORDER BY ts_utc"
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    out = []
    for sym, ts, tse, sess, o, h, l, c, v, cur, adj in rows:
        out.append(Candle(symbol=sym, interval="1m",
                          ts_utc=_parse_iso(ts), ts_exchange=_parse_iso(tse),
                          session=sess, open=o, high=h, low=l, close=c, volume=v,
                          currency=cur, adjusted=bool(adj)))
    return out


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
