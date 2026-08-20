# P0-US30 Gross-Edge Screen — OOS identity 접근 정정서

상태: **APPEND-ONLY POST-RESULT ERRATUM**  
대상: `PREREG_P0_US30_GROSS_EDGE_SCREEN_V1.{md,json}` / PR #125  
발견 단계: 결과 커밋 후, 병합 전 독립 검토

## 발견 사실

최초 공식 실행의 identity 검사기는 frozen SQLite의 semantic content hash를 다시 계산했다.
이 계산은 전략 성과와 무관한 무결성 검사였지만, 파일에 물리적으로 포함된 blind OOS
(2026-01-21~2026-07-21) OHLCV 행을 프로세스가 디코딩했다. 따라서 사전등록의
“blind OOS 가격 접근 금지”를 문자 그대로 완전히 지켰다고 기록할 수 없다.

다음은 발생하지 않았다.

- blind OOS forward return 또는 전략 성과 계산
- blind OOS 값을 로그·JSON·Markdown에 출력
- 사람이 blind OOS 가격 또는 성과를 열람
- OOS 값에 따른 후보·문턱·통계·비용 규칙 변경

그러므로 이는 **정보가 전략 선택에 유입된 증거는 없지만, 기술적 가격 접근 계약은 위반한
절차 편차**다. 기존 결과를 pristine preregistration으로 표현하지 않는다.

## 교정

전체 semantic hash 재계산을 제거했다. 교정 실행은 다음만 검증한다.

1. snapshot 파일 SHA-256
2. 추적 snapshot receipt의 file/content SHA-256
3. 추적 intraday manifest의 data SHA-256
4. raw content-addressed index SHA-256

교정된 실행은 blind OOS OHLCV를 읽지 않고 development 구간만 `research_load`로 로드한다.
후보 8개, 신호·진입·청산 시각, 일별 동일가중, fold, 통계, 문턱과 비용은 변경하지 않았다.
재실행 결과는 `NO_GROSS_EDGE`로 동일하고 최선 후보 수치도 동일하다.

## 판정 범위

- `NO_GROSS_EDGE`는 **교정된 코드로 재현된 development-only 음성 결과**로 보존한다.
- 원래 실행을 “blind OOS 완전 무접촉”이라고 주장하지 않는다.
- 이 정정은 blind OOS unlock, 전략 선택, Paper 또는 live 승격 근거가 아니다.
- 양성 후보를 보존하거나 승격하는 상황이었다면 새 사전등록과 깨끗한 실행 경계가 필요했을
  것이다. 이번에는 모든 후보가 gross gate 자체에서 탈락했으므로 결과 후 선택 편향으로 양성
  결론을 만든 경로가 없다.

## 추가 비용 증거 주의

사전등록의 `published_floor=0.204%p`는 문서에 “2026-08-10 비용 감사”에서 고정했다고
기록되어 있으나, 저장소에서 그 수치를 독립적으로 재구성할 별도 추적 비용감사 artifact는
찾지 못했다. 이 결함은 모든 후보가 비용 적용 전 gross gate에서 탈락했으므로 최종 판정에는
영향이 없다. `conservative=0.340%p`와 `stress=0.680%p`는 기존 공식 연구 계약과 일치한다.
