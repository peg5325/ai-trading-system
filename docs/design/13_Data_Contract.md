# Canonical Data Contract

Backtest 와 Live 의 train/live skew 를 원천 차단하기 위한 단일 데이터 계약.

## Canonical Candle
fields:
- symbol
- timestamp_utc        # 내부 저장 기준
- timestamp_exchange   # 거래소 로컬(ET)
- interval
- session              # PRE / REGULAR / POST (extended hours 구분)
- open, high, low, close
- volume
- adjusted             # split/corporate-action 보정 여부

## Rules
- 내부 저장은 UTC. 표시(KST/ET)는 화면 변환만.
- 모든 Provider 는 내부 Canonical Candle 형식으로 변환해 반환.
- 지표 계산(VWAP/EMA/RSI 등)은 Provider 무관하게 내부 IndicatorEngine 에서만.
- Backtest 와 Live 는 동일 IndicatorEngine 코드 사용 (100% parity 목표).

## Corporate Actions (중요)
- SOXL 은 역분할(reverse split) 이력 있음.
- 과거 분봉이 split-adjusted 인지 명시 (candle.adjusted).
- 병합일 경계에서 백테스트가 깨지지 않도록 adjusted 정책 통일.

## Multi-source Alignment
- 실거래 시세(Toss)와 백테스트 데이터(KIS/Polygon)의 봉 집계 방식·타임스탬프 정합성 사전 검증.
- 불일치 시 canonical 변환 단계에서 정규화.
