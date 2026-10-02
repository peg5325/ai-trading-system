# 장전 Daily Brief v2 계약 (read-only shadow)

## 목적

미국장 개장 전에 정규화된 point-in-time 증거를 읽어 최대 3개의 `LONG_WATCH` 또는
`SHORT_WATCH` 후보와 현금 대기인 `NO_TRADE`를 결정론적으로 만든다. 이 산출물은 조사와
일지 작성용이며 주문 권한이 없다.

v2는 v1의 `LONG_OR_CASH` 출력을 조용히 확장하지 않고, `SHORT_WATCH`와
`LONG_SHORT_OR_CASH`를 새 출력 스키마 `premarket-daily-brief/v2`로 명시한다.
입력 `premarket-input/v1`과 설정 `premarket-config/v1`은 변경하지 않는다.

## 시간 계약

- NYSE 정규장 개장 기준 T-90분 primary run, T-30분 재검증, T-20분 freeze.
- 고정 KST 시각을 사용하지 않고 `America/New_York`와 추적 NYSE 캘린더를 사용한다.
- `as_of` 이후에 published/available/ingested된 정보는 미래 정보로 보고 전체 run을
  `NO_TRADE`로 닫는다.
- 휴장일 또는 추적 캘린더 범위 밖 날짜는 Brief를 만들지 않는다.

## 입력 계약

CLI는 네트워크에 접속하지 않고 `premarket-input/v1` 로컬 JSON만 읽는다. 입력에는 다음이
필수다.

- source receipt: source ID/kind/status, observed/available/ingested 시각, 증거 URL, content SHA-256
- event: event ID/source ID, published/available/ingested 시각, URL/hash, 관련 종목과 sentiment
- candidate: 종목, 선택적 strategy template ID, 5개 고정 점수, source/event ID 조인
- market regime: `RISK_ON`, `RISK_OFF`, `UNCERTAIN`과 근거 source ID

실제 뉴스·거시지표·공시 수집은 별도 `premarket-provider-bundle/v1` 계층에서 수행한다.
해당 bundle은 후보 점수와 market regime을 만들지 않으므로 Daily Brief에 직접 입력할 수 없다.
후속 정규화 단계가 source/event 조인과 MARKET 근거를 완성해야 한다. 세부 계약은
`PREMARKET_EVIDENCE_PROVIDER_CONTRACT.md`를 따른다.

## 판정 계약

- 방향 정책은 `LONG_SHORT_OR_CASH`다. `RISK_ON`에서는 `LONG_WATCH`, `RISK_OFF`에서는
  `SHORT_WATCH`, `UNCERTAIN`에서는 `NO_TRADE`만 허용한다.
- 후보는 P0-US30 정본 안에서만 허용하며 inverse ETF는 직접 방향과의 이중 해석을 막기 위해
  계속 제외한다.
- required source뿐 아니라 event/candidate/regime이 실제 참조한 source도 각각 `OK`이고 최신이어야
  하며, 누락/비정상/오래됨, 시간 역전, 미래 정보, ID 불명, 상충 revision은 fail-closed.
- 데이터 품질이 정상이고 regime이 `RISK_ON` 또는 `RISK_OFF`일 때만 점수를 매긴다.
- LONG 점수는 기존 5개 점수의 고정 가중합이다. SHORT 점수는 catalyst의 부호를 반전하고
  trend·relative-strength를 `1-score`로 반전하며 volume·liquidity는 그대로 사용한다.
- LONG은 catalyst가 양수, SHORT은 catalyst가 음수여야 하며 유동성 문턱은 동일하다.
- 유동성 및 catalyst 최소치를 통과한 후보를 고정 가중합으로 정렬하고 symbol로 tie-break한다.
- LONG/SHORT strategy template 승인은 별도 allowlist이며 한 방향의 승인이 반대 방향으로
  상속되지 않는다. 승인 template이 하나 이상 있는 경우에도 상태는 `SHADOW_READY`일 뿐이며
  `executable=false`, `promotion_allowed=false`, `orders=[]`는 불변이다.
- 현재 config의 LONG·SHORT 승인 template 목록은 모두 비어 있으므로 정상 후보가 있어도
  `NO_TRADE`다. 정규화 단계는 승인 목록에 `premarket_freeze_long_intraday_v1`이 있을 때만 그 ID를
  후보에 붙이며(새 config 필드 없음), 목록이 비어 있으면 입력·snapshot·Brief 해시가 그대로다.
- 승인 목록을 채우는 활성화는 검증된 첫 세션 영수증
  (`PREREG_PREMARKET_FREEZE_LONG_PAPER_V1_FIRST_SESSION_RECEIPT.json`)과 함께만 허용된다.
  영수증 없이 목록이 채워지면 테스트가 실패한다.

## 증거 및 변경 경계

- 정규화 입력, 고정 policy config, NYSE 상대 스케줄, quality defect, run ID, snapshot SHA-256을
  결과에 포함한다.
- `news_title_relevance_required`(v1.3, 2026-10-02 시행)는 켜졌을 때만 `policy_config`에 기록된다.
  꺼져 있거나 없으면 키 자체를 생략해 이전 config의 `policy_config` 해시가 그대로다. 규칙은
  `PREMARKET_NORMALIZATION_CONTRACT.md`를 따른다.
- JSON과 Markdown은 원자적으로 write-once하며 동일 재실행만 허용한다.
- `src/order`, broker, live account, 주문 제출, 외부 HTTP 호출을 import하거나 실행하지 않는다.
