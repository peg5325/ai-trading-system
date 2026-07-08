# AI Trading System

AI 보조 **다종목 자동매매 시스템** — 사용자는 섹터/유니버스와 리스크 한도만 정하고,
종목 선별·진입·사이징·청산은 코드와 AI가 자율 결정한다.

> ⚠️ **현재 Paper Trading 전용.** 실주문(TossBrokerProvider)은 구현하지 않았고,
> 모든 체결은 `PaperBrokerProvider`(모의)로만 이루어진다. 실거래 자문/권유 아님.

미국(SOXL·QQQ·NVDA 등)과 국내(005930·000660 등) 종목을 함께 다루며, 토스증권 오픈 API로
시세를 수집한다.

---

## 핵심 원칙

- **코드가 최종 통제권을 가진다.** 생존이 수익보다 우선.
- **AI는 진입 필터 + 설명 레이어일 뿐**, 절대 실행자가 아니다.
- **청산·손절·EOD flatten은 AI와 독립된 순수 코드**가 수행한다.
- 리스크는 **종목별 + 포트폴리오** 두 층에서, 레버리지 상품은 **실질 노출** 기준으로 관리.
- **Backtest·Paper는 반드시 Live보다 앞선다.**

## 아키텍처

```
MarketData → CandleStore → IndicatorEngine → UniverseScreener → AIDecisionEngine(entry filter)
           → RiskManager → OrderManager → PaperBrokerProvider
                    ↑ Reconciliation        ↓ TradingJournal → ReplaySystem → AIReviewEngine
           ─ TradingPipeline(run_once) ─ PaperTradingLoop(tick) ─ PaperRuntime(run/stop/health/report) ─
```

- **진입 경로**: Screener 후보 → AI confidence 게이팅 → Risk 통과 → Paper 주문
- **청산 경로**: PositionMonitor → Risk → ExitOrder (AI 완전 독립)
- **회고 루프**: Journal(기록) → Replay(재생) → Review(GPT 회고 → 개선안)

## 저장소 구조

```
src/
  providers/   Toss 클라이언트 + MarketData (fetch만)
  storage/     CandleStore (SQLite)
  engines/     IndicatorEngine (EMA/RSI/VWAP/Bollinger/StochRSI)
  universe/    UniverseScreener (섹터 → 후보 선별)
  ai/          AIDecisionEngine (OpenAI entry filter, FakeAIProvider)
  risk/        RiskManager (종목별+포트폴리오, 실질 노출)
  reconcile/   ReconciliationService (내부 vs 브로커 대조)
  backtest/    BacktestEngine + ExecutionModel
  order/       OrderManager + PaperBrokerProvider (+ TossBrokerProvider skeleton)
  pipeline/    TradingPipeline (한 사이클 오케스트레이션)
  live/        PaperTradingLoop (5분봉 마감마다 실행)
  journal/     TradingJournal (SQLite 감사 로그)
  replay/      ReplaySystem (cycle 재생, read-only)
  review/      AIReviewEngine (사후 회고)
  runtime/     PaperRuntime (운영 모드: 상태관리/헬스/복구/리포트)
config/        runtime*.yaml (운영 설정)
scripts/       run_paper.py (운영 진입점), ai_entry_filter_manual.py
spikes/        feasibility 검증 스크립트 (Toss API 실사)
tests/         전 계층 pytest
docs/          design/ (설계 문서 15개 + FEASIBILITY_SPIKE), IMPLEMENTATION.md (구현 현황)
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 토스 키 (.env — git 에 커밋되지 않음)
cp .env.example .env         # 편집해서 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 채우기

# 테스트
python -m pytest -q

# Paper Runtime 운영 (실주문 없음, Ctrl+C = graceful shutdown)
pip install PyYAML
python scripts/run_paper.py config/runtime_mixed.yaml   # KR/US 혼합 유니버스

# 조회 CLI (read-only, 가동 중에도 안전) — 4대 SSoT + 실현손익
python scripts/inspect_cli.py summary
python scripts/inspect_cli.py pnl          # 실현손익(FIFO): 승률/PF/Expectancy/보유시간
```

운영 산출물: `status*.json`(실시간 대시보드), `logs*/`(rotation), `reports/`(종료 리포트),
`data/`(SQLite). — **모두 `.gitignore` 처리(계좌/시세 데이터 보호).**

### 성능 평가 — Realized PnL Engine (FIFO)
BUY/SELL 을 **FIFO 로 매칭**해 실현손익을 계산하는 **단일 엔진**(실거래/Paper/Backtest 공용).
부분청산 지원, `net_pnl == gross_pnl - slippage - commission` 불변식, 결정론적. 청산이 완료돼야
실제 승률/기대수익이 나온다(진입만 있으면 미청산 포지션으로 집계). `scripts/inspect_cli.py pnl` 로 조회.

### US 본장 Paper 운영 (자금관리)
`PortfolioAccount` 가 현금/포지션/실현손익을 FIFO 엔진으로 관리해 Paper Trading 이
**실제 자금관리 기준으로** 성과를 측정한다(고정 계좌값이 아니라 체결마다 cash 가 갱신됨).

- 초기자본 3,000,000, 종목당 최대 20% 명목 노출, **동시 보유 2종목**, **현금부족 시 진입 거절**.
- 실행: `./.venv/bin/python scripts/run_paper.py config/runtime_us.yaml`
- 통화 주의: 자본은 KRW 명목, 종목은 USD 가격 — v1 은 통화를 분리하지 않는다(알려진 한계).
  수익률/승률/Profit Factor/Sharpe/MDD 등 **상대 성과 지표는 단위가 상쇄되어 유효**하지만,
  절대 금액(손익/현금)은 단위가 혼재되므로 참고용으로만 사용할 것.

## 시장 (US / KR)

Canonical Candle은 `market` 필드로 시장을 구분하고, 세션/타임존을 시장별로 분류한다.

| 시장 | 통화 | 타임존 | 세션 |
|---|---|---|---|
| US | USD | ET | PRE / REGULAR / POST / OVERNIGHT |
| KR | KRW | KST | PRE(08–09) / REGULAR(09–15:30) / POST(15:30–18) / CLOSED |

혼합 유니버스에서 한 시장의 세션이 다른 시장 종목에 적용되지 않는다.

## Module Guide

`src/` 하위 각 폴더에 README.md 가 있다. 폴더 단위 역할·입출력·**금지사항**·테스트 위치를 담았다.
(계층별 사용법과 구현 현황은 [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) 참고)

### 데이터 → 판단 → 실행

| 모듈 | 역할 | 핵심 금지사항 |
|---|---|---|
| [providers](src/providers/README.md) | Toss 시세 fetch + Canonical 변환 | 지표 계산 금지, 주문 메서드 금지 |
| [storage](src/storage/README.md) | CandleStore (SQLite) | 지표 컬럼 저장 금지 |
| [ingest](src/ingest/README.md) | 과거 캔들 백필 | 주문 금지, rate-limit 가드 제거 금지 |
| [engines](src/engines/README.md) | IndicatorEngine (EMA/RSI/VWAP/BB/StochRSI) | Provider 접근 금지, 미완결 봉 사용 금지 |
| [universe](src/universe/README.md) | UniverseScreener (후보 선별) | AI 가 종목 직접 선정 금지, 최종 BUY 금지 |
| [ai](src/ai/README.md) | AIDecisionEngine (진입 필터) | **AI 가 청산 판단 금지**, 주문 실행 금지 |
| [risk](src/risk/README.md) | RiskManager (최종 게이트) | **RiskManager 가 Broker 호출 금지**, 청산 차단 금지 |
| [exits](src/exits/README.md) | ExitSignalGenerator (SL/TP/Time/EOD) | AI 관여 금지, 우선순위 변경 금지 |
| [order](src/order/README.md) | OrderManager + PaperBroker | `risk_decision.allowed` 없이 브로커 호출 금지, 실주문 금지 |
| [reconcile](src/reconcile/README.md) | 내부 vs 브로커 정합성 대조 | 상태 변경 금지 · ⚠️ 현재 미배선 |

### 오케스트레이션 · 운영

| 모듈 | 역할 | 핵심 금지사항 |
|---|---|---|
| [pipeline](src/pipeline/README.md) | TradingPipeline (한 사이클) | 비즈니스 로직 금지, 청산에 AI 태우기 금지 |
| [live](src/live/README.md) | PaperTradingLoop (5분봉 마감마다) | 실주문 금지, 미완결 봉 거래 금지 |
| [runtime](src/runtime/README.md) | PaperRuntime (상태/헬스/복구/리포트) | 주문 금지, 관측 실패로 매매 중단 금지 |
| [cli](src/cli/README.md) | inspect CLI (조회 전용) | 쓰기 금지 (`mode=ro` + `query_only`) |

### 성과 · 회고 루프

| 모듈 | 역할 | 핵심 금지사항 |
|---|---|---|
| [pnl](src/pnl/README.md) | 실현손익 단일 엔진 (FIFO) | 두 번째 손익 계산기 금지, 불변식 파괴 금지 |
| [portfolio](src/portfolio/README.md) | PortfolioAccount (현금/포지션/성과) | 자체 PnL 매칭 금지, 성과 산식 변경 금지 |
| [backtest](src/backtest/README.md) | BacktestEngine + ExecutionModel | look-ahead 금지, 신호봉 종가 체결 금지 |
| [journal](src/journal/README.md) | TradingJournal (감사 로그) | 매매 판단 금지, 주문 없는 사이클 누락 금지 |
| [replay](src/replay/README.md) | ReplaySystem (cycle 재생) | **상태 변경 금지**, look-ahead 금지 |
| [review](src/review/README.md) | AIReviewEngine (전략 회고 + 개선 제안) | 주문 금지, strategy 자동 수정 금지 |
| [system_review](src/system_review/README.md) | 운영 품질 회고 (A–F 등급) | 전략 평가 금지, Rule Engine 영향 금지 |
| [dataset](src/dataset/README.md) | AI 학습 데이터셋 빌더 | 원본 변경 금지, look-ahead 금지 |

### 권한의 순서 (전 계층에서 중복 강제)

```
AI 는 자문 → RiskManager 가 결정 → OrderManager 가 실행 → Broker 가 체결
청산은 AI 를 완전히 우회한다.
```

- `src/ai/engine.py` 는 브로커에 닿을 수 없다 (import 자체가 없음).
- `src/order/manager.py:41` — `risk_decision.allowed` 가 False 면 브로커를 호출하지 않는다.
- `src/risk/risk_manager.py:81` — `RISK_REDUCING` 주문은 모든 차단 검사보다 먼저 통과한다.
  **kill switch 가 켜져 있어도 탈출은 가능하다.**

## 문서

- **설계**: [docs/design/](docs/design/) — 01~15 + `FEASIBILITY_SPIKE.md`, `design_overview.html`
- **구현 현황 / 계층별 사용법**: [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md)
- **모듈별 상세**: 위 [Module Guide](#module-guide)

## 상태

- 데이터→지표→선별→AI→리스크→정합성→백테스트→주문(Paper)→파이프라인→실시간루프→저널→Replay→Review→Runtime **구현 완료**
- **실주문 없음** (Paper only), OpenAI는 옵션(기본 FakeAI)
- 전 계층 pytest 통과

## Disclaimer

교육·연구용 개인 프로젝트다. 투자 자문이 아니며, 실거래 손실에 대한 책임을 지지 않는다.
실주문을 붙이기 전 충분한 백테스트·페이퍼 검증과 통화별 계좌 분리가 필요하다.
