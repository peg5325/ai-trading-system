# MVP Scope

목표: 사용자 지정 섹터/유니버스 안에서 **자율적으로 종목을 고르고 운용하는 다종목 시스템**을
안정적으로 작동시키는 것. 다종목 native 로 설계하되, 첫 Live 검증은 작은 유니버스부터.

## Autonomy (자율 범위)
- 사용자: 섹터/유니버스 경계 + 리스크 한도 + on/off/kill.
- 시스템 자동: 종목 선별(Screener) · 진입(AI filter) · 사이징 · 청산 · 타이밍.

## Constraints (MVP)
- 단일 계좌
- 다종목 (UniverseScreener 자율 선별) — 첫 검증 유니버스는 작게 (예: 3~5 종목)
- 동시 포지션 상한 N (초기 작게, 포트폴리오 리스크와 연동)
- 5분봉 기준 의사결정
- 프리 + 정규장 + 애프터마켓 (extended hours 포함, 연장시간은 지정가+유동성 게이트)
- 오버나잇 레버리지 차등 (일반주 허용, 레버리지 ETF 당일 청산)
- 종목별 분할매수 2~3회
- 헤징/숏 미지원 (MVP)
- AI 는 Entry Filter 만, Exit 는 항상 코드
- Backtest 와 Live 는 동일 IndicatorEngine (포트폴리오 단위 백테스트)

## SOXL Note
SOXL 은 대표 검증 종목이자 레버리지 ETF 케이스일 뿐, 범위 제한이 아니다.

## Architecture Principles
- Autonomy: 사용자는 경계만, 종목/타이밍/사이징은 시스템이.
- Multi-symbol & portfolio native from day one.
- Deterministic execution before AI sophistication.
- 작은 유니버스에서 검증 → 유니버스 확대.

## Expansion (검증 후)
유니버스 확대 → 다전략 → Multi AI → 다 Broker → 프리/애프터마켓 · 숏 · 헤징.
