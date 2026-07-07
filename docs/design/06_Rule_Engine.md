# Rule Engine

## Indicators (internal IndicatorEngine, shared by backtest + live)
- RSI
- Stochastic RSI
- VWAP
- EMA7
- EMA20
- Volume
- Bollinger Band

## Cadence
- UniverseScreener 가 고른 후보 종목 각각에 대해 5분봉 마감 기준 평가.

## Candidate Long Setup (initial example — not final edge)
아래는 SOXL 같은 고변동 종목 기준 예시. 종목/유니버스에 따라 strategy.yaml 로 조정.
- RSI <= 30 또는 급락 후 RSI 회복
- 거래량 최근 평균 이상
- 가격이 VWAP 근처 또는 회복
- EMA7/EMA20 회복 시도
- 참조지표(SOXX/SMH/QQQ) 하락 둔화 또는 반등
- VIX 급등 구간 회피

## Rule
- Candidate 조건 충족 시에만 AI 호출 (비용/노이즈 절감).
- 파라미터 초기값은 고정, 이후 백테스트 최적화 후보로 관리.

## Externalization
- Rule trigger + parameters 는 config/strategy.yaml 로 외부화.

## Edge Hypothesis (unverified)
SOXL 과매도 후 섹터 반등 + 거래량 증가 + VWAP/EMA 회복 동시 발생 구간의 단기 반등 확률.
백테스트 검증 전까지 엣지 있다고 주장하지 않음.
