# System Architecture

다종목 native. UniverseScreener 가 섹터/유니버스에서 후보 종목을 자율 선별하고,
각 후보 종목에 대해 진입/청산 파이프라인이 병렬로 돈다.

## Universe Layer (자율 종목 선별)
UniverseConfig (사용자: 섹터/유니버스 + 필터) -> UniverseScreener -> tradable candidates[]

- 유동성/거래대금/가격/변동성 필터 + 지표 기반 랭킹으로 tradable set 선정 (top-N).
- 동적: 사이클/일 단위 갱신. 사용자는 경계만 정하고 종목은 시스템이 고른다.
- see 15_Universe_Screener.

## Entry Path (per candidate symbol, AI may gate)
symbol -> IndicatorEngine -> RuleEngine -> AIDecisionEngine (entry filter)
       -> RiskManager (종목별 + 포트폴리오) -> OrderManager -> BrokerProvider

- 주문은 Rule 후보 + AI confidence >= threshold + 종목별 Risk + 포트폴리오 Risk 를 모두 통과할 때만.
- AI can BLOCK a new entry. AI can never FORCE an order.

## Exit Path (모든 보유 종목, AI 독립, 순수 코드)
PositionMonitor (전 종목) -> RiskManager (종목별 + 포트폴리오) -> ExitOrder -> BrokerProvider

- stop-loss, daily loss limit, circuit breaker, kill switch, EOD flatten.
- OpenAI 장애/타임아웃/rate limit 과 무관하게 작동.

## Portfolio Risk Manager (신규, 종목별 위에)
- 동시 포지션 수 한도, 전체 실질 노출 한도, 섹터 집중도, 상관관계, 포트폴리오 드로다운.
- 개별 종목이 통과해도 포트폴리오 한도를 넘으면 신규 진입 거부.

## Cross-cutting Services
- ReconciliationService: 전 종목 내부 position DB vs 브로커 실잔고 대조 (source of truth = broker).
- MarketCalendarService: 세션 판정(PRE / REGULAR / POST) + 휴장, 레버리지 종목 Flatten Window.
- IndicatorEngine: 지표 계산 단일 모듈 (backtest + live 공유), 종목별 실행.
- Alerting: 텔레그램/Slack/이메일 중 하나.

## Sessions (extended hours 포함)
- PRE 04:00–09:30 ET / REGULAR 09:30–16:00 / POST 16:00–20:00 (ET).
- 세 세션 모두 주문 가능. 연장시간은 지정가 우선 + 유동성 게이트 (see 11).
- 오버나잇: leverage_factor==1 허용, >1 은 세션 마감 전 Leverage Flatten (see 11).

## Providers
Broker:        TossBrokerProvider (MVP), KisBrokerProvider (future)
MarketData:    TossMarketDataProvider (realtime), KisMarketDataProvider / PolygonMarketDataProvider (history / universe / reference)
AI:            OpenAIProvider (MVP), ClaudeProvider / GeminiProvider (future)

## Hard Rule
Provider 는 데이터 fetch + 주문만. 종목 선별 / 지표 / 판단 / 리스크 로직은 Provider 밖에서.
