# 장전 Daily Brief v1 계약 (read-only shadow)

## 목적

미국장 개장 전에 정규화된 point-in-time 증거를 읽어 최대 3개의 `LONG_WATCH` 후보 또는
`NO_TRADE`를 결정론적으로 만든다. 이 산출물은 조사와 일지 작성용이며 주문 권한이 없다.

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

실제 뉴스·거시지표·공시 공급자 adapter와 자격증명은 다음 PR 범위다. 공급자 추가 전에는
별도 라이선스·재현성·point-in-time 의미를 검토한다.

## 판정 계약

- 방향은 `LONG_OR_CASH`뿐이다. SHORT은 v1 범위 밖이다.
- 후보는 P0-US30 정본 안에서만 허용하며 inverse ETF는 경제적 숏 노출이므로 제외한다.
- required source뿐 아니라 event/candidate/regime이 실제 참조한 source도 각각 `OK`이고 최신이어야
  하며, 누락/비정상/오래됨, 시간 역전, 미래 정보, ID 불명, 상충 revision은 fail-closed.
- 데이터 품질이 정상이고 regime이 `RISK_ON`일 때만 점수를 매긴다.
- 유동성 및 catalyst 최소치를 통과한 후보를 고정 가중합으로 정렬하고 symbol로 tie-break한다.
- 승인 strategy template이 하나 이상 있는 경우에도 상태는 `SHADOW_READY`일 뿐이며
  `executable=false`, `promotion_allowed=false`, `orders=[]`는 불변이다.
- 현재 config의 승인 template 목록은 비어 있으므로 정상 후보가 있어도 `NO_TRADE`다.

## 증거 및 변경 경계

- 정규화 입력, 고정 policy config, NYSE 상대 스케줄, quality defect, run ID, snapshot SHA-256을
  결과에 포함한다.
- JSON과 Markdown은 원자적으로 write-once하며 동일 재실행만 허용한다.
- `src/order`, broker, live account, 주문 제출, 외부 HTTP 호출을 import하거나 실행하지 않는다.
