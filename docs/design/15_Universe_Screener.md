# Universe Screener

자율 종목 선별 층. 사용자는 섹터/유니버스 경계만 정하고, 어떤 종목을 실제로 매매할지는
시스템이 스캔·랭킹·선정한다. 이것이 "큰 개입 없는 자동 운용"의 핵심.

## Input (사용자가 정하는 경계)
- sector / universe (예: 반도체, 레버리지 ETF 그룹, 특정 티커 리스트)
- hard filters: 최소 거래대금/유동성, 가격 범위, 변동성 범위
- 최대 동시 후보 수 (top-N)
- 제외 목록 (blacklist)

## Process
1. Universe 후보 수집 (섹터 구성 종목 or 유니버스 리스트)
2. Hard filter 적용 (유동성/가격/변동성 부적합 제거)
3. 지표/모멘텀 기반 랭킹 (IndicatorEngine 재사용)
4. tradable set 선정 (top-N)
5. 각 종목에 leverage_factor 태깅 (레버리지 ETF 식별)

## Output
tradable_candidates[]  # symbol, rank, leverage_factor, liquidity, refreshed_at

## Cadence
- Universe refresh: 일 단위(장 시작 전) 기본.
- Intraday: 후보 셋 안에서 Rule Engine 이 5분봉 신호 평가.

## Guards
- 최대 동시 후보/포지션 수 제한 → 포트폴리오 리스크와 연동.
- 레버리지 ETF 는 실질 노출 기준으로 별도 카운트.
- 유동성 급감/거래정지 종목 자동 제외.
- 데이터 결측 종목은 후보에서 제외 (매매 금지).

## Mode (사용자 선택)
- SECTOR_AUTO: 섹터만 주면 시스템이 종목 선별 (기본).
- FIXED_LIST: 사용자가 티커 리스트 지정 시 그 안에서만. (SECTOR_AUTO 의 특수 케이스)

## Config
config/universe.yaml 로 외부화.
