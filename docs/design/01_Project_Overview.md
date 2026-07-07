# Project Overview

## Vision
사용자의 큰 개입 없이 **AI + 코드가 자율적으로 판단·운용하는 다종목 자동매매 시스템.**
사용자는 "무엇을 살 수 있는가(섹터/유니버스)"와 "얼마나 위험을 감수하는가(리스크 한도)"만 정한다.
종목 선별 · 진입 · 사이징 · 청산 · 타이밍은 모두 자동.

## Human vs Machine
- Human decides: 섹터/유니버스 경계, 리스크 한도, on/off · kill switch.
- Machine decides: 그 안에서 어떤 종목을, 언제, 얼마나, 어떻게 진입/청산할지.

## SOXL Note
SOXL은 사용자의 **대표 단타 종목(flagship example)**일 뿐, 시스템의 범위가 아니다.
설계는 처음부터 다종목(multi-symbol) + 포트폴리오 native.

## Core Principles
- Code owns final control. 생존이 수익보다 우선.
- AI is an **entry filter + explanation layer only**. Never an executor.
- Exit is always code-owned (see 05, 11).
- 종목 선택은 UniverseScreener 가 자율 수행 (섹터 → 후보 선별).
- 리스크는 종목별 + 포트폴리오 두 층에서 관리.
- Provider-based architecture (broker / market data / AI 교체 가능).
- Deterministic execution before AI sophistication.
- 다종목 native 로 설계하되, 첫 Live 검증은 작은 유니버스부터.

## Session / Instrument Scope
- Trading window: 프리마켓 + 정규장 + 애프터마켓 (extended hours 포함). 장 열린 동안 상시 감시·진입.
- Overnight: **레버리지 차등.** 일반주(leverage_factor=1)는 오버나잇 보유 허용,
  레버리지 ETF(leverage_factor>1)는 갭·감쇠 리스크로 당일 청산.
- Extended hours 는 유동성이 얇음 → 지정가 우선 + 유동성 게이트 + 사이즈 축소.
- Instruments: 레버리지 ETF(SOXL 등) 포함. 종목별 leverage_factor 로 실질 노출·오버나잇·갭 리스크 관리.

## MVP Building Blocks
- Broker: Toss (order/account), KIS/Polygon (data)
- AI: OpenAI (entry filter only)
- UniverseScreener (섹터 → tradable 후보)
- Rule Engine + IndicatorEngine
- Risk Manager (종목별 + 포트폴리오) — Live 이전 필수
- Backtest Engine (포트폴리오 단위) — Live 이전 필수
- ReconciliationService
- Trade / AI Decision Logger

## Biggest Risk
AI 모델 선택이 아니라 **검증되지 않은 전략 + 불완전한 리스크 관리 + 데이터 정합성**.
Risk Manager와 Backtest는 반드시 Live Trading보다 앞선다.
