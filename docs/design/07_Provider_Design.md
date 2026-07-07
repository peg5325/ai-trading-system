# Provider Design

## Interfaces
- BrokerProvider      (order, account, balance, positions)
- MarketDataProvider  (candles, realtime price)
- AIProvider          (entry filter decision)

## Goal
Broker / AI / market data 를 최소 코드 변경으로 교체.

## Hard Rule
- Provider 는 데이터 fetch + 주문 실행만.
- 지표 계산 / 판단 / 리스크 로직은 Provider 밖(engines, risk)에서.
- 모든 Provider 는 Canonical Candle 형식으로 변환해 반환 (see 13_Data_Contract).

## MVP Split
- Order/Account: TossBrokerProvider
- Intraday history: KisMarketDataProvider 또는 PolygonMarketDataProvider
- Reference indicators (SOXX/SMH/QQQ/VIX): Polygon 우선 후보
- 주문은 Toss 로 시작하되, 백테스트/연구 데이터는 Toss 에 종속시키지 않음.
