# 현재 상태와 세션 인계

갱신일: 2026-09-22

## 정본

1. [목표·평가 기준](PROJECT_CHARTER.md)
2. [단계별 계획](ROADMAP.md)
3. 이 문서: 최신 증거와 다음 작업

## 확인된 기준점

- 문서 작업 시작 시 HEAD: `fdacaaf` (PR #155, 월간 FRED 참고값이 장전 판단을 막지 않도록 교정).
- PR #151: LONG/SHORT/CASH 관찰. PR #153: 장전 증거와 시장 데이터 정규화.
- 2026-09-21 FREEZE 로컬 보고서 확인: `NO_TRADE`, 사유 `NO_APPROVED_STRATEGY_TEMPLATE`,
  watchlist MU/TSLA/AAPL 전부 LONG_WATCH, 주문 0, executable=false.
- 증거: `reports/premarket-daily-brief.20260921.freeze.json` 및 같은 이름 `.md`.
  로컬 reports는 Git 배포와 별개이며 없는 환경에서는 재검증 불가라고 표시한다.
- snapshot SHA-256: `b05e876fd115429db9629fd787b1aa693247ae1971eccf0e315b59e6509f5cdb`.
- 첫 정상 사례는 전략의 초과수익 입증이 아니다. 승인된 실거래 전략이 있다는 주장도 하지 않는다.
- 기존 1개월 연구 종료: [closeout](research/RESULT_ONE_MONTH_STRATEGY_SELECTION_CLOSEOUT.md).
  데이터 불충분 판정을 전략 실패와 혼동하지 않는다.

## 이번 작업

- 목표·벤치마크·비용·관측 기간·Paper 진입 조건의 문서 정본 생성은 PR #156으로 main 병합 완료.
- 단계 2의 1차 구현으로 장전 FREEZE 판단 사후평가 자동화를 추가했다.
  `src/premarket/postclose.py`와 `scripts/evaluate_premarket_postclose.py`는 read-only로
  FREEZE Daily Brief를 고정 입력으로 받아 정규장 마감 이후에만 평가하고, JSON에서 Markdown과
  일별/누적 장부를 파생한다. 주문·브로커·Paper 체결·승격 판정은 생성하지 않는다.
- 평가 항목: FREEZE artifact/input-file SHA-256, 후보 stage/score/template metadata,
  정규장 첫 1분봉 open→마지막 1분봉 close 방향성 proxy, 0.34%p official 및 0.68%p stress
  왕복 비용 민감도, SPY 동시간대 진단, 공식 S&P 500 Total Return 미연결 시
  `INCONCLUSIVE_BENCHMARK`, 결측/중복/장전 실행 차단 verdict.
- 현행 FREEZE 파일을 임시 read-only 출력 경로에서 재실행한 결과, 로컬 DB에 2026-09-21
  정규장 1분봉이 없어 `INCOMPLETE_MARKET_DATA`로 닫혔다. 해당 결측은 수익률 0으로
  대체하지 않으며, 마감 이후 재실행의 일별 JSON/Markdown은 byte-identical을 확인했다.
  임시 산출물과 gitignore reports는 저장소 증거로 추적하지 않는다.
- 이슈 #157, 브랜치 `codex/157-premarket-postclose-ledger`에서 구현했다.
  독립 code-reviewer는 Critical/High/Medium 0, LOW 1(기존 장부의 중복 거래일 방어)을
  보고했고 로드·누적 집계 양쪽에 fail-closed 검증과 회귀 테스트를 반영했다.
  독립 architect는 단계 2 전체가 아닌 FREEZE-only 1차 구현으로 범위를 제한하는 조건에서 승인했다.
- 리뷰 보완: 장 마감 전 또는 PRIMARY/VERIFICATION 입력은 exit code 2로 끝내고
  일별 immutable 결과·ledger를 쓰지 않는다. 불완전 일자가 하나라도 있으면 누적 기간
  수익률은 `INCOMPLETE_PROXY_COVERAGE`와 함께 `null`로 보고한다. SHORT MFE/MAE도
  종가 수익률과 동일한 진입 명목 기준으로 계산한다.
- 테스트: post-close 19 passed, 관련 장전·OOS 게이트 135 passed, 전체 pytest
  2,000 passed. Ruff·compileall·`git diff --check` 통과.
- `.codex/`는 기존 사용자 소유 미추적 경로로 유지한다.
- 2026-09-24 자동화 장애를 근거로 이슈 #159에서 로컬 운영 실행기를 구현했다.
  PRIMARY는 Marketaux timeout과 Toss OAuth 403을 `FAIL_CLOSED → NO_TRADE`로 남겼고,
  VERIFICATION은 Toss 403 이후 Codex workspace credit 부족으로, FREEZE는 credit 부족으로
  중단됐다. 이 날짜는 전향적 유효 관측으로 세지 않는다.
- `scripts/run_premarket_pipeline.py`는 Codex 없이 PRIMARY/VERIFICATION/FREEZE/post-close를
  실행하며, Toss HTTP·timeout·network 오류는 표준 immutable market failure artifact로 바꾼다.
  due stage 실패는 redacted step log SHA와 immutable `INCOMPLETE` operation receipt로 남긴다.
  `scripts/install_premarket_launchd.py`는 DST-safe paired KST trigger를 설치·상태 확인한다.
  실제 launchd 설치는 main 병합 후 별도 실행이며 주문·브로커·OOS 권한은 없다.
- 테스트: 운영·장전·OOS 관련 151 passed, 전체 pytest 2,009 passed. Ruff·compileall·
  `git diff --check` 통과. 독립 code-reviewer APPROVE, architect CLEAR.

## 다음 한 가지 작업

단계 2 다음 작업: **모의거래 전용 승인 계약**을 고정한다.
먼저 #159 병합 후 launchd를 설치·상태 확인하고, 다음 정상 거래일에서 모든 stage가 완결되는지
확인한다. 그 뒤 전략 템플릿, 진입·청산·비중·손절, 체결 가격 모델, 잔고/비용/위험 제어 규칙을
문서화·테스트한 뒤 이번 사후평가 장부와 분리된 Paper 계좌 장부를 구현한다.
전략 템플릿, 진입·청산·비중·손절, 체결 가격 모델, 잔고/비용/위험 제어 규칙을 먼저 문서화·테스트한 뒤
이번 사후평가 장부와 분리된 Paper 계좌 장부를 구현한다.

## 아직 충족하지 않은 실행 게이트

- 확정한 위험 한도의 코드 강제, 공식 총수익 벤치마크 출처/배당 검증.
- 사후평가 자동화 및 미래 표본 누적, 전략 사전등록 및 Paper 게이트.
- 계좌 연결 시 초기 자금 액수·실제 비용·주문 권한·사용자 실행 승인.
- 기존 이슈의 현재 open/closed 상태와 자동화 일정은 해당 작업 시작 때 조회한다.
  이전 대화의 이슈 번호만으로 완료를 추정하지 않는다.

## 세션 종료 기록 형식

갱신일 / 실제 HEAD·PR / 완료 증거 / 미해결 사항 / 다음 한 가지 작업을 갱신한다.
테스트 실행 여부와 문서상 정책/구현 상태를 구분하고, 자격증명·계좌번호는 기록하지 않는다.
