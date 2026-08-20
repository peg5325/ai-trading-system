# P0-US30 장마감 과잉압력 반전 — OOS identity 접근 정정서

상태: **APPEND-ONLY POST-RESULT ERRATUM**

대상: `PREREG_P0_US30_CLOSE_PRESSURE_REVERSAL_V1.{md,json}` / PR #131

발견 단계: 결과 커밋 후, 병합 전 최신 독립 감사

## 발견 사실

최초 공식 실행기는 일봉 identity 확인을 위해 canonical DB manifest를 blind OOS 종료일
(2026-07-21)까지 다시 계산했다. 전략 성과 로더는 사전등록 development 종료일
(2025-07-18)까지만 읽었지만, identity 계산 과정에서 swing blind OOS OHLCV를 프로세스가
디코딩했다. 따라서 최초 결과를 문자 그대로 “blind OOS 가격 미접근”이라고 표현할 수 없다.

다음은 발생하지 않았다.

- blind OOS 전략 성과·forward return 계산
- blind OOS 값을 로그·결과 산출물에 출력
- 사람이 blind OOS 가격 또는 성과를 열람
- OOS 값에 따른 신호·보유기간·손절·비용·게이트 변경

이는 정보가 전략 선택에 유입됐다는 증거는 없지만 기술적 가격 접근 계약은 위반한 절차
편차다. 사전등록 본문을 개서하지 않고 이 정정서를 별도로 보존한다.

## 교정

전체 일봉 OOS 실측 해시를 제거하고 다음으로 분리한다.

1. full daily identity는 추적 `P0_US30_DAILY_10Y_MANIFEST.json` receipt로 인증
2. live DB OHLCV hash는 development 종료일 2025-07-18까지만 계산
3. development content SHA-256과 행 수를 고정 회귀로 검증

신호, ATR, 다음날 시가 진입, 3일/1일 보유, 손절, 포트폴리오·비용·게이트는 변경하지 않는다.

## 판정 범위

- 교정 결과는 **development-only 음성 결과**로만 보존한다.
- 원래 실행을 pristine blind-OOS 사전등록 증거로 표현하지 않는다.
- OOS unlock, Paper 또는 live 승격 근거가 아니다.
- 양성 후보를 승격하는 상황이었다면 새 사전등록과 깨끗한 실행 경계가 필요했을 것이다.
