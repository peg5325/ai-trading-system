# Roadmap (Revised)

핵심 원칙: Backtest 와 Paper Trading 은 반드시 Live Trading 보다 앞선다.
다종목 native 로 짓되, 첫 Live 검증 유니버스는 작게.

1.  데이터·API 가용성 검증 (feasibility spike — see FEASIBILITY_SPIKE.md)
2.  유니버스 데이터 확보: 섹터 종목군 과거 분봉 + 참조지표
3.  MarketData 수집·저장 (multi-symbol canonical candle)
4.  IndicatorEngine + Rule Engine (per-symbol)
5.  UniverseScreener (섹터 → tradable 후보 선별)
6.  Risk Manager: 종목별 + 포트폴리오 구현
7.  ReconciliationService (전 종목)
8.  Backtest Engine (포트폴리오 단위, execution model 포함)
9.  AI Decision Engine 연결 (entry filter only, 로그 검증)
10. Paper Trading (작은 유니버스)
11. 소액 Live Trading
12. 유니버스 확장 / KIS·Polygon Provider / Multi AI / Agent 구조

## Gate
- 1~2 실패 시 프로젝트 성립 불가 -> 재설계.
- 8(Backtest) 합격 기준 미달 시 10(Paper) 진입 금지.
