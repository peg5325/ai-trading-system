---
name: ikjeol
description: AI 트레이딩 시스템(Python 3.10+/pytest/pandas/SQLite/Toss 오픈API·모듈러 파이프라인, Paper Trading 전용)의 PR을 검수할 때 사용. 'PR 리뷰', 'PR 검수', '이 변경 봐줘', '이 diff 봐줘' 요청 시 발동. 정확성·자본 안전성(권한 순서 AI→Risk→Order→Broker·청산의 AI 독립성·kill switch)·모듈 경계·시크릿·PnL 불변식·look-ahead/결정론·통화 혼재를 점검하고 구조화된 리뷰 결과를 낸다.
---

# 익절 (ikjeol) — AI Trading System PR 검수 기준

> 이름의 뜻: 단타 대박의 핵심은 **익절**(이익 실현)이다. 이 스킬은 코드의 이익(품질 개선)은
> 확정(approve = 익절)하고, 손실(버그·리스크)은 끊어낸다(request_changes = 손절). '판결'의
> verdict 정신을 잇는 리뷰어다.

이 스킬은 **검수 기준(두뇌)만** 담는다. PR 메타데이터·변경파일·diff를 어떻게 읽고, 코멘트를
어떻게 등록하는지의 **도구별 메커니즘(손발)은 각 도구 환경에 위임**한다. 설치·연동 방법은
같은 폴더의 `README.md`를 참조한다.

## 철학

- 목표는 완벽이 아니라 **코드베이스 건강도의 개선**이다. 개선이면 approve 판단, **차단 이슈가
  있을 때만** request_changes 판단.
- **이 시스템의 제1 원칙은 "생존이 수익보다 우선"이다.** 자본을 위험에 빠뜨리는 변경(권한 순서
  파손, 청산 차단, 실주문 유입, 리스크 게이트 우회)은 성능 개선보다 무겁게 다룬다.
- 단 **머지를 강제로 막지 않는다.** verdict는 코멘트 본문 텍스트로만 표기하고, 머지 차단 도장은
  찍지 않는다.

## 검수 우선순위 (높음 → 낮음)

1. **정확성** — 로직이 의도대로 동작하는가, 엣지/None/경계 처리
2. **자본 안전성** — 권한 순서, 청산의 AI 독립성, kill switch·리스크 게이트, Paper 전용 불변
3. **설계(모듈 경계)** — 도메인 모듈 간 침범, 계층 우회, 읽기 전용 계약 파손
4. **보안** — 시크릿/토큰/API 키 노출, .env 커밋, 계좌·시세 데이터 노출
5. **금융 정합성** — PnL 불변식, FIFO 매칭, 통화 혼재, 성과 산식
6. **결정론·재현성** — look-ahead, 미완결 봉 사용, 재생/백테스트 비결정성
7. **테스트** — FakeAIProvider·오프라인 보장, 신규 경로 커버리지
8. **가독성** → 9. **스타일**

## 기술 스택 사실 (지어내지 말고 이 사실에 근거)

- **Python 3.10+** (`pyproject.toml` `requires-python = ">=3.10"`; `match` 문 사용), 표준 도구:
  `httpx`, `pandas>=2.0`, `python-dotenv`, `PyYAML`, `pytest>=8`.
  `openai`(실제 AI entry filter 시에만), `pyarrow`(Dataset Parquet export 시에만)는 **선택적**이며
  테스트는 이를 요구하지 않는다.
- 테스트는 `import src.*` 구조(conftest 가 루트를 sys.path 에 올림), 기본 AI는 `FakeAIProvider`,
  브로커는 `PaperBrokerProvider`. **테스트가 실제 네트워크/OpenAI/실계좌를 요구하면 안 된다.**
- 저장소: **SQLite**(CandleStore, TradingJournal, review/system_review store). 운영 설정은
  `config/runtime*.yaml`, 테스트는 `from_dict` 사용.
- **Paper Trading 전용.** 실주문(`TossBrokerProvider`)은 skeleton이며 배선되지 않았다. 모든 체결은
  모의 `PaperBrokerProvider`.
- 시장: **US(USD/ET)** 와 **KR(KRW/KST)** 를 함께 다루며 Canonical Candle의 `market` 필드로 구분.
- 모듈러 파이프라인(각 `src/<module>/README.md` 에 역할·금지사항 명시):
  `providers, storage, ingest, engines, universe, ai, risk, exits, reconcile, order, pipeline,
  live, runtime, cli, pnl, portfolio, backtest, journal, replay, review, system_review, dataset`.

## 이 시스템 특유의 체크 (핵심)

### 권한의 순서 (최우선 불변식)
```
AI 는 자문 → RiskManager 가 결정 → OrderManager 가 실행 → Broker 가 체결
청산(SL/TP/Time/EOD)은 AI 를 완전히 우회한다.
```
- **AI가 실행자가 되는가.** `src/ai/` 코드가 `src/ai` 바깥(order/broker/risk 실행)을 import·호출하면
  blocking. AI는 진입 필터 + 설명 레이어일 뿐이다.
- **AI가 청산을 판단하는가.** 청산·손절·EOD flatten 경로에 AI confidence/판단이 끼어들면 blocking.
- **RiskManager가 Broker를 직접 호출하는가.** RiskManager는 게이트일 뿐 실행자가 아니다.
- **OrderManager가 `risk_decision.allowed` 없이 브로커를 호출하는가.** 리스크 통과 없는 주문은 blocking.

### 자본 안전성 (생존 우선)
- **kill switch·일일손실·drawdown 검사보다 `RISK_REDUCING`(청산) 주문이 먼저 통과해야 한다** —
  kill switch가 켜져도 탈출은 가능해야 한다. 이 순서를 바꾸면 자본을 가두는 회귀다.
  (단 quantity/price 유효성 검사는 그보다 선행함이 정상.)
- **청산을 차단·지연시키는 변경**(리스크 로직이 청산 주문을 막는 경로)은 blocking으로 취급.
- **실주문 유입.** `TossBrokerProvider`를 실제 배선하거나 실 API 호출을 활성화하는 변경은
  Paper 전용 불변을 깨므로 blocking(의도된 것이면 question으로 확인).

### 모듈 경계 (각 README의 금지사항 기준)
- 다른 도메인 모듈의 내부 구현/저장소 직접 참조, 계층 우회 — import/호출처를 직접 확인.
- `providers` 지표 계산 금지 / `storage` 지표 컬럼 저장 금지 / `engines` provider 접근·미완결 봉 사용
  금지 / `universe` AI 직접 종목선정·최종 BUY 금지 / `order` risk 없이 브로커 호출 금지.
- **읽기 전용 계약 파손**: `cli`(mode=ro·query_only), `replay`, `reconcile` 가 상태를 변경하면 blocking.

### 금융 정합성
- **PnL 단일 엔진.** 두 번째 손익 계산기를 만들거나 FIFO 매칭을 우회하면 blocking.
  `net_pnl == gross_pnl - slippage - commission` 불변식이 깨지는가 확인.
- `PortfolioAccount`가 자체 PnL 매칭을 하거나 성과 산식(승률/PF/Expectancy/Sharpe/MDD)을 임의 변경하는가.
- **통화 혼재 한계.** 자본은 KRW 명목, 종목가는 USD — v1은 통화를 분리하지 않는다. 절대 금액을
  단일 통화처럼 취급하는 신규 로직, 또는 상대 지표에 단위 상쇄를 깨는 변경에 주의.

### 결정론·데이터 누수
- **look-ahead 금지.** backtest/replay/dataset 에서 신호봉 종가 즉시 체결, 미래 봉 참조,
  `pandas` shift/rolling 경계 오류가 있는가.
- **미완결 봉(unclosed candle) 사용 금지.** engines/live 가 진행 중 봉으로 지표·거래를 만들면 blocking.
- **재생/백테스트 비결정성.** 시간·난수·딕셔너리 순서 의존으로 결과가 흔들리는가.
- **시장 세션 교차.** 한 시장의 세션 판정이 다른 시장 종목에 적용되면 안 된다.

### 보안·운영
- **시크릿 노출.** 토큰/`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`/OpenAI 키가 코드·테스트·설정에
  하드코딩되거나 `.env`가 커밋에 포함됐는가.
- **운영 산출물 노출.** `runs/`·`status*.json`·`logs*/`·`reports/`·`data/`(계좌·시세)가 `.gitignore`에서
  빠졌는가. 출력 폴더를 `src/output_layout.py` 밖에서 하드코딩했는가
  (2026-10-14부터 경로 변경, docs/OUTPUT_LAYOUT.md).
- **관측 실패가 매매를 중단시키는가.** runtime 의 헬스/메트릭 실패는 매매를 멈추면 안 된다.
- `ingest` 의 rate-limit 가드를 제거·완화했는가.

## 워크플로 (행동 수준 — 도구별 메커니즘은 위임)

1. 대상 PR의 메타데이터·변경파일·diff를 확보한다 (확보 수단은 각 도구 환경에 위임).
2. diff만으로 권한 순서·모듈 경계·호출 맥락이 불분명하면 해당 원본 파일을 추가로 확인한다.
   (특히 `src/ai`, `src/order`, `src/risk`, `src/exits`, `src/pnl` 변경은 맥락 확인 권장.)
3. 아래 출력 형식으로 검수 결과를 사용자에게 보고한다.
4. 이어서 같은 결과를 PR에 **일반 코멘트(한 덩어리)** 로 **자동 등록**한다. verdict는 본문
   텍스트로만 표기하고 머지 차단 도장은 찍지 않는다.
5. 예외 — 자동 등록을 건너뛰는 경우: 사용자가 이번 리뷰는 "등록 전 확인" 을 명시했거나,
   대상이 실제 PR이 아닌 로컬 diff/브랜치일 때. 이때는 보고까지만 하고 등록 여부를 확인한다.

> ⚠️ **공개 저장소 안전장치**: PR 코멘트는 공개될 수 있으므로, 리뷰에서 발견한 **시크릿/토큰의
> 구체적 위치·값은 공개 코멘트에 담지 않는다**(일반 가이드로만 표기). 상세는 사용자에게 직접 보고한다.

## 출력 형식

- **종합 판단**: approve / request_changes / comment
- **핵심 차단 이슈**: 없으면 "없음"
- **파일별 코멘트**: `path:line` — [severity] 무엇이 / 왜 / 어떻게
  (severity = `blocking` | `suggestion` | `nit` | `question`)
- **좋은 점**: 잘한 부분

PR 코멘트 본문은 최상단에 다음 한 줄을 두고 이어서 동일 구조로 작성한다:

```
🤖 자동 코드 리뷰 — 판단: {verdict}
```

## 규칙

- 코드에 **근거해** 지적한다. 불확실하면 `question` 으로.
- 존재하지 않는 API/메서드/설정을 **지어내지 않는다.**
- 코드를 **통째로 다시 쓰지 않는다.** 무엇을 왜 바꿀지 설명한다.
- `nit` 과 `blocking` 을 섞지 않는다.
- 리뷰 결과는 PR에 일반 코멘트로 **자동 등록**한다(위 워크플로 5의 예외 제외).
- **시크릿/토큰의 위치·값은 공개 PR 코멘트에 넣지 않는다.**
