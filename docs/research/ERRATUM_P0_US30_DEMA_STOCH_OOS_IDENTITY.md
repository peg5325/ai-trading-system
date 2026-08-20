# P0-US30 DEMA/Stochastic — OOS identity 접근 정정서

상태: **APPEND-ONLY POST-RESULT ERRATUM**

대상: `PREREG_P0_US30_DEMA_STOCH_MOMENTUM_V1.{md,json}` / PR #129

발견 단계: 결과 커밋 후, 병합 전 최신 독립 감사

## 발견 사실

최초 공식 실행의 identity 검사는 frozen SQLite의 semantic content hash를 다시 계산했다.
전략 성과와 무관한 무결성 검사였지만, 그 과정에서 blind OOS
(2026-01-21~2026-07-21) OHLCV 행을 프로세스가 디코딩했다. 따라서 최초 결과를 문자 그대로
“blind OOS 가격 미접근”이라고 표현할 수 없다.

다음은 발생하지 않았다.

- blind OOS forward return 또는 전략 성과 계산
- blind OOS 값을 로그·결과 산출물에 출력
- 사람이 blind OOS 가격 또는 성과를 열람
- OOS 값에 따른 신호·보유기간·비용·게이트 변경

이는 정보가 전략 선택에 유입됐다는 증거는 없지만, 기술적 가격 접근 계약은 위반한 절차
편차다. 사전등록 본문을 조용히 수정하지 않고 이 정정서를 별도로 보존한다.

## 교정

전체 semantic hash 재계산을 제거하고 다음 증거만 검증한다.

1. snapshot 파일 SHA-256
2. 추적 snapshot receipt의 file/content SHA-256
3. 추적 intraday manifest의 data SHA-256
4. raw content-addressed index SHA-256

교정 실행은 blind OOS OHLCV를 읽지 않고 development 구간만 `research_load`로 로드한다.
DEMA/Stochastic 산식, 동시 교차, 다음 봉 진입, 12봉 고정보유, 포트폴리오·비용·게이트는
변경하지 않는다.

## 판정 범위

- 교정 실행 결과는 **development-only 음성 결과**로만 보존한다.
- 원래 실행을 pristine blind-OOS 사전등록 증거로 표현하지 않는다.
- OOS unlock, Paper 또는 live 승격 근거가 아니다.
- 양성 후보를 승격하는 상황이었다면 새 사전등록과 깨끗한 실행 경계가 필요했을 것이다.
