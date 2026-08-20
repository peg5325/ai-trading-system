# P0-US30 52주 고점 상대강도 돌파 — OOS identity 접근 정정서

상태: **APPEND-ONLY POST-RESULT ERRATUM**

대상: `PREREG_P0_US30_HIGH52_RS_BREAKOUT_V1.{md,json}` / PR #133

발견 단계: 결과 커밋 후, 병합 전 최신 독립 감사

## 발견 사실

최초 공식 실행기는 일봉 identity 확인을 위해 canonical DB manifest를 blind OOS 종료일
(2026-07-21)까지 다시 계산했다. 전략 성과 로더는 development 종료일(2025-07-18)까지만
읽었지만 identity 계산이 swing blind OOS OHLCV를 디코딩했다. 따라서 최초 결과를 문자 그대로
“blind OOS 가격 미접근”이라고 표현할 수 없다.

blind OOS 전략 성과·forward return 계산, 결과 출력, 사람 열람, OOS 값에 따른 규칙 변경은
발생하지 않았다. 그럼에도 기술적 가격 접근 계약을 위반한 절차 편차이므로 사전등록 본문을
개서하지 않고 이 정정서를 별도 보존한다.

## 교정

1. full daily identity는 추적 `P0_US30_DAILY_10Y_MANIFEST.json` receipt로 인증
2. live DB OHLCV hash는 development 종료일 2025-07-18까지만 계산
3. development content SHA-256과 행 수를 고정 회귀로 검증

52주 고점·상대강도·첫 돌파 규칙, 다음날 시가 진입, 5일/3일 보유, 손절, 포트폴리오·비용·
경제 게이트는 변경하지 않는다.

## 판정 범위

- 교정 결과는 **development-only 음성 결과**로만 보존한다.
- 원래 실행을 pristine blind-OOS 사전등록 증거로 표현하지 않는다.
- 경제 게이트가 모두 통과하면 승격하지 않고 `REQUIRES_NEW_PREREG`를 반환한다.
- OOS unlock, Paper 또는 live 승격 근거가 아니다.
