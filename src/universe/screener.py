"""UniverseScreener — 사용자 지정 유니버스에서 매매 후보를 Rule 기반으로 선별.

MVP 원칙:
- AI 가 종목을 직접 고르지 않는다. 결정론적 Rule 점수화로 후보를 압축한다.
- Provider/Broker 에 접근하지 않는다. 입력은 IndicatorEngine 결과(+캔들)뿐.
- 최종 BUY 가 아니라 candidate list 만 생성 (Rule Engine / AIDecisionEngine 으로 전달).

점수 요소(각 0~100, 가중합): trend, momentum, volume, vwap_position, volatility.
리스크: 레버리지 ETF 인식, 유동성 부족/변동성 과다/마감 임박/연장시간 스프레드 → 제외 또는 감점.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.models import (
    MARKET_US,
    SESSION_CLOSED,
    SESSION_OVERNIGHT,
    SESSION_POST,
    SESSION_PRE,
    SESSION_REGULAR,
    classify_session,
    exchange_tz,
)

# ---- 상수 ----
CANDIDATE_LONG = "LONG_SETUP"
CANDIDATE_EXCLUDED = "EXCLUDED"

FLAG_LEVERAGED = "LEVERAGED_ETF"
FLAG_HIGH_VOL = "HIGH_VOLATILITY"
FLAG_EXTREME_VOL = "EXTREME_VOLATILITY"
FLAG_LOW_LIQUIDITY = "LOW_LIQUIDITY"
FLAG_NEAR_CLOSE = "NEAR_MARKET_CLOSE"
FLAG_EXT_HOURS = "EXTENDED_HOURS_SPREAD"
FLAG_INSUFFICIENT = "INSUFFICIENT_DATA"
FLAG_BELOW_MIN = "BELOW_MIN_SCORE"

_REGULAR_CLOSE_MIN = 16 * 60  # 16:00 ET

# 대표 레버리지 ETF (leverage_factor 미지정 시 자동 인식). 확장 가능.
KNOWN_LEVERAGE: dict[str, int] = {
    "SOXL": 3, "SOXS": 3, "TQQQ": 3, "SQQQ": 3, "TECL": 3, "TECS": 3,
    "SPXL": 3, "UPRO": 3, "SPXS": 3, "TNA": 3, "TZA": 3, "FAS": 3, "FAZ": 3,
    "LABU": 3, "LABD": 3, "NVDL": 2, "TSLL": 2, "QLD": 2, "SSO": 2,
}


def leverage_for(symbol: str, override: int | None = None) -> int:
    if override is not None:
        return override
    return KNOWN_LEVERAGE.get(symbol.upper(), 1)


# ---- 입출력 구조 ----
@dataclass
class SymbolData:
    """스크리너 입력: 한 종목의 IndicatorEngine 결과 + 메타."""
    symbol: str
    indicators: pd.DataFrame
    leverage_factor: int = 1
    market: str = MARKET_US          # US | KR (세션/타임존 판정)

    @classmethod
    def create(cls, symbol: str, indicators: pd.DataFrame,
               leverage_factor: int | None = None,
               market: str = MARKET_US) -> "SymbolData":
        return cls(symbol, indicators, leverage_for(symbol, leverage_factor), market)


@dataclass
class Candidate:
    """스크리너 출력. Rule Engine / AIDecisionEngine 으로 전달 가능한 구조."""
    symbol: str
    score: float
    rank: int
    candidate_type: str          # LONG_SETUP | EXCLUDED
    reasons: list[str]
    risk_flags: list[str]
    features: dict = field(default_factory=dict)

    def to_dict(self, include_features: bool = False) -> dict:
        d = {
            "symbol": self.symbol,
            "score": self.score,
            "rank": self.rank,
            "candidate_type": self.candidate_type,
            "reasons": self.reasons,
            "risk_flags": self.risk_flags,
        }
        if include_features:
            d["features"] = self.features
        return d


@dataclass(frozen=True)
class ScreenerConfig:
    weights: dict = field(default_factory=lambda: {
        "trend": 0.30, "momentum": 0.25, "volume": 0.20,
        "vwap_position": 0.15, "volatility": 0.10,
    })
    volume_lookback: int = 20
    min_avg_volume: float = 300.0       # 유동성 하한(평균 거래량)
    high_vol_bb_width: float = 0.08     # BB폭/mid > 8% → 과다 변동성 감점
    extreme_vol_bb_width: float = 0.15  # > 15% → 후보 제외
    near_close_minutes: int = 10        # 마감 N분 전 → 감점
    min_score: float = 45.0             # 후보 최소 점수
    near_close_penalty: float = 15.0
    extended_hours_penalty: float = 10.0
    high_vol_penalty: float = 10.0


# 인디케이터가 유효해야 평가 가능한 필수 컬럼
_REQUIRED = ("close", "ema7", "ema20", "rsi14", "vwap", "bb_mid", "bb_upper", "bb_lower")


class UniverseScreener:
    def __init__(self, config: ScreenerConfig | None = None):
        self.cfg = config or ScreenerConfig()

    # ---- public ----
    def screen(self, inputs: list[SymbolData]) -> list[Candidate]:
        """후보(LONG_SETUP)만 점수 내림차순으로 랭킹해 반환.
        동점은 symbol 오름차순으로 결정론적 처리."""
        cands = [c for c in self.screen_all(inputs) if c.candidate_type == CANDIDATE_LONG]
        cands.sort(key=lambda c: (-c.score, c.symbol))
        for i, c in enumerate(cands, start=1):
            c.rank = i
        return cands

    def screen_all(self, inputs: list[SymbolData]) -> list[Candidate]:
        """제외된 종목까지 포함한 전체 평가 결과(진단/투명성용). 랭킹 없음."""
        return [self._evaluate(sd) for sd in inputs]

    # ---- evaluation ----
    def _evaluate(self, sd: SymbolData) -> Candidate:
        risk: list[str] = []
        if sd.leverage_factor > 1:
            risk.append(FLAG_LEVERAGED)

        ind = sd.indicators
        if ind is None or len(ind) == 0:
            return self._excluded(sd, [FLAG_INSUFFICIENT] + risk, "no data")

        last = ind.iloc[-1]
        if any((c not in ind.columns) or pd.isna(last.get(c)) for c in _REQUIRED):
            return self._excluded(sd, [FLAG_INSUFFICIENT] + risk, "indicators warming up")

        # 유동성
        avg_vol = self._avg_volume(ind)
        if avg_vol < self.cfg.min_avg_volume:
            return self._excluded(sd, [FLAG_LOW_LIQUIDITY] + risk, "low liquidity")

        # 변동성 (극단 → 제외)
        bb_mid = float(last["bb_mid"])
        bb_width = (float(last["bb_upper"]) - float(last["bb_lower"])) / bb_mid if bb_mid else 0.0
        if bb_width > self.cfg.extreme_vol_bb_width:
            return self._excluded(sd, [FLAG_EXTREME_VOL] + risk, "extreme volatility")

        penalties = 0.0
        if bb_width > self.cfg.high_vol_bb_width:
            risk.append(FLAG_HIGH_VOL)
            penalties += self.cfg.high_vol_penalty

        # 세션 기반 리스크 (마지막 봉의 거래소 로컬 시각으로 market 별 판정)
        ts_ex = ind.index[-1].tz_convert(exchange_tz(sd.market))
        session = classify_session(ts_ex, sd.market)
        if session in (SESSION_PRE, SESSION_POST, SESSION_OVERNIGHT, SESSION_CLOSED):
            risk.append(FLAG_EXT_HOURS)
            penalties += self.cfg.extended_hours_penalty
        elif session == SESSION_REGULAR:
            close_min = _REGULAR_CLOSE_MIN if sd.market == MARKET_US else 15 * 60 + 30
            mins_to_close = close_min - (ts_ex.hour * 60 + ts_ex.minute)
            if 0 <= mins_to_close <= self.cfg.near_close_minutes:
                risk.append(FLAG_NEAR_CLOSE)
                penalties += self.cfg.near_close_penalty

        # 점수 요소
        reasons: list[str] = []
        f_trend, r = self._trend(ind); reasons += r
        f_mom, r = self._momentum(ind); reasons += r
        f_vol, r = self._volume(ind, avg_vol); reasons += r
        f_vwap, r = self._vwap(ind); reasons += r
        f_volat, r = self._volatility(bb_width); reasons += r

        w = self.cfg.weights
        base = (w["trend"] * f_trend + w["momentum"] * f_mom + w["volume"] * f_vol
                + w["vwap_position"] * f_vwap + w["volatility"] * f_volat)
        score = max(0.0, min(100.0, base - penalties))

        features = {
            "trend": f_trend, "momentum": f_mom, "volume": f_vol,
            "vwap_position": f_vwap, "volatility": f_volat,
            "bb_width": round(bb_width, 4), "avg_volume": round(avg_vol, 1),
            "session": session, "penalties": penalties,
            "leverage_factor": sd.leverage_factor,
        }

        if score < self.cfg.min_score:
            return Candidate(sd.symbol, round(score, 1), 0, CANDIDATE_EXCLUDED,
                             reasons, [FLAG_BELOW_MIN] + risk, features)
        return Candidate(sd.symbol, round(score, 1), 0, CANDIDATE_LONG,
                         reasons, risk, features)

    # ---- helpers ----
    def _excluded(self, sd: SymbolData, risk: list[str], reason: str) -> Candidate:
        return Candidate(sd.symbol, 0.0, 0, CANDIDATE_EXCLUDED, [reason], risk, {})

    def _avg_volume(self, ind: pd.DataFrame) -> float:
        vols = ind["volume"].dropna()
        tail = vols.iloc[-self.cfg.volume_lookback:]
        return float(tail.mean()) if len(tail) else 0.0

    def _trend(self, ind: pd.DataFrame) -> tuple[float, list[str]]:
        last = ind.iloc[-1]
        close, ema7, ema20 = float(last["close"]), float(last["ema7"]), float(last["ema20"])
        score, reasons = 0.0, []
        if close > ema20:
            score += 50.0
            recovered = (len(ind) >= 2
                         and float(ind["close"].iloc[-2]) <= float(ind["ema20"].iloc[-2]))
            reasons.append("5m trend recovered above EMA20" if recovered else "price above EMA20")
        if ema7 > ema20:
            score += 50.0
            reasons.append("EMA7 above EMA20 (uptrend)")
        return score, reasons

    def _momentum(self, ind: pd.DataFrame) -> tuple[float, list[str]]:
        last = ind.iloc[-1]
        rsi = float(last["rsi14"])
        score, reasons = 50.0, []
        if 45.0 <= rsi <= 68.0:
            score += 20.0
            reasons.append("RSI in healthy zone")
        if len(ind) >= 2:
            prev_rsi = ind["rsi14"].iloc[-2]
            if not pd.isna(prev_rsi) and rsi > float(prev_rsi):
                score += 15.0
                reasons.append("RSI rising")
        if rsi < 30.0:
            score -= 20.0
        k, d = last.get("stochrsi_k"), last.get("stochrsi_d")
        if not pd.isna(k) and not pd.isna(d) and float(k) > float(d):
            score += 15.0
            reasons.append("StochRSI %K above %D")
        return max(0.0, min(100.0, score)), reasons

    def _volume(self, ind: pd.DataFrame, avg_vol: float) -> tuple[float, list[str]]:
        last_vol = float(ind["volume"].iloc[-1])
        if avg_vol <= 0:
            return 30.0, []
        ratio = last_vol / avg_vol
        if ratio >= 1.5:
            return 90.0, ["volume well above recent average"]
        if ratio >= 1.0:
            return 70.0, ["volume above recent average"]
        if ratio >= 0.7:
            return 50.0, []
        return 30.0, []

    def _vwap(self, ind: pd.DataFrame) -> tuple[float, list[str]]:
        last = ind.iloc[-1]
        close, vw = float(last["close"]), float(last["vwap"])
        if vw == 0:
            return 50.0, []
        dist = (close - vw) / vw
        if abs(dist) <= 0.005:
            return 80.0, ["price near VWAP"]
        if dist > 0:
            return 70.0, ["price above VWAP"]
        return 40.0, []

    def _volatility(self, bb_width: float) -> tuple[float, list[str]]:
        if bb_width < 0.01:
            return 45.0, []                       # 너무 잠잠 → 매력 낮음
        if bb_width <= self.cfg.high_vol_bb_width:
            return 80.0, ["healthy volatility"]
        if bb_width <= self.cfg.extreme_vol_bb_width:
            return 55.0, []                       # 높음(HIGH_VOLATILITY 플래그됨)
        return 30.0, []                           # 실제로는 extreme 은 이미 제외
