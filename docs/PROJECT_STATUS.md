# 현재 상태와 세션 인계

갱신일: 2026-09-30

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
  주문·브로커·OOS 권한은 없다. PR #160·#162(`.venv` 이탈 수정) 병합 후 LaunchAgent 8개를 설치했고,
  2026-09-30 `--status`로 8개 모두 plist 존재·loaded를 확인했다.
- 테스트: 운영·장전·OOS 관련 151 passed, 전체 pytest 2,009 passed. Ruff·compileall·
  `git diff --check` 통과. 독립 code-reviewer APPROVE, architect CLEAR.
- 2026-09-28 FREEZE와 post-close가 처음으로 완결됐다. AAPL/NVDA/TSLA LONG_WATCH의
  동일비중 방향성 proxy는 공식 비용 후 −1.584%였으며 실제 결정은 승인 전략 부재로
  `NO_TRADE`·주문 0이었다. 한 날짜의 결과를 전략 성과로 승격하지 않는다.
- 2026-09-29 FREEZE(META/MU/AVGO LONG_WATCH)와 post-close도 완결됐다. 동일비중 방향성 proxy는
  비용 전 +0.114%, 공식 비용 후 −0.226%, SPY 동시간대 진단 대비 +0.115%p였다. 실제 결정은
  `NO_TRADE`·주문 0이다. 누적 proxy 장부는 09-21 결측으로 `INCOMPLETE_PROXY_COVERAGE`다.
  증거: 로컬 `reports/premarket-postclose.2026092{8,9}.json`, `premarket-operation.*.json`(gitignore).
- 이슈 #163에서 첫 Paper 전용 계약 `premarket_freeze_long_intraday_v1`을 구현 전 고정했다(PR #164).
  v1은 LONG_WATCH만 대상으로 하고 SHORT_WATCH는 관찰로 남긴다.
- 2026-09-30 병합 전 독립 리뷰가 CHANGES_REQUIRED(기준 장부 미지정, 운영비 조건 누락, 입력 필드
  경로 불명확, 정수 주식에서 20% 비중 불성립 등)를 냈다. 첫 Paper 세션 전에 계약을 v1.1로 개정했다.
  사용자 결정: 종목별 20% 삭제, USD 2,000 유지, stress 왕복 0.51%p, 총 노출 75%. Paper 수수료는
  Toss 공식 FAQ(매수·매도 각 0.1%, 매도 SEC fee 0.00206%)를 따른다. Charter v1.3에 이유와 효력을 기록했다.
  익절 기준 리뷰 3회 반복 후 approve, PR #164는 2026-09-30 main에 병합됐다(`c686a6a`).
- 이슈 #165(어댑터)를 PR 3단계로 나눴다: 엔진 → 운영(runner·launchd·영수증) → 10거래일 평가기.
  2026-09-30 사용자 결정으로 계약을 v1.2로 개정했다(첫 세션 전). 지수 3배 ETF TQQQ·SOXL LONG은
  가격 명목 기준으로 거래하고, SHORT_WATCH TQQQ/SOXL은 SQQQ/SOXS 매수(인버스 롱)로 연결한다.
  개별주 SHORT_WATCH는 관찰, 개별주 레버리지 ETF는 제외한다. Charter v1.4에 노출 = 가격 명목과
  인버스 다리의 별도 보고를 기록했다.
- PR 1(엔진, 브랜치 `codex/165-freeze-paper-engine`): `src/freeze_paper/`의 binding·gates·sizing·
  costs·engine·reconcile과 청산 생성기·계좌·Paper 브로커의 추가형 확장을 구현했다. 거부된 청산의
  시도 번호 재시도, 비정상 봉 fail-closed, 재시작 결정론, 주문↔체결·현금 항등식 대조를 테스트로 고정했다.
  익절 리뷰 3회 후 approve, PR #166으로 2026-09-30 main에 병합됐다(`97faeb4`).
  2026-09-29 FREEZE `allowed_symbols`에는 TQQQ·SOXL이 있고 개별주 레버리지 ETF는 없음을 확인했다.
- PR 2(운영, 브랜치 `codex/165-freeze-paper-ops`): 세션 러너와 CLI(`scripts/run_freeze_paper_session.py`),
  immutable 세션 artifact 체인·검증된 누적 장부·운영 영수증, 첫 세션 영수증 스키마·검증기(계약·config·코드
  커밋·달력·허용 목록 결속), 템플릿 배선(allowlist가 비어 있으면 OFF, 장전 해시 불변 테스트),
  post-close와 공유하는 DB 잠금, launchd freeze-paper 05:35·06:35·07:35 KST 정의를 추가했다.
  launchd는 아직 설치하지 않았고 활성화 영수증도 없다. 전체 pytest 2,189 passed / 11 skipped.

## 다음 한 가지 작업

단계 2 다음 작업: PR 2 병합 후 이슈 #165의 **PR 3(10거래일 평가기)**를 구현한다. 공식·stress·운영비
차감 누적수익, SPY 총수익(무수정 종가+배당 재투자, 미검증 배당 시 `INCONCLUSIVE_BENCHMARK`),
LONG·인버스 다리 별도 보고, 판정 우선순위를 구현한다. 그 뒤 launchd 재설치와 첫 세션 영수증·활성화
커밋으로 첫 미래 거래일을 고정한다. 실거래 전 레버리지·인버스 ETP의 브로커·규제 사전 조건을 확인한다.

## 아직 충족하지 않은 실행 게이트

- 확정한 위험 한도의 코드 강제, 공식 총수익 벤치마크 출처/배당 검증.
- 사후평가 자동화 및 미래 표본 누적, 전략 사전등록 및 Paper 게이트.
- 계좌 연결 시 초기 자금 액수·실제 비용·주문 권한·사용자 실행 승인.
- 기존 이슈의 현재 open/closed 상태와 자동화 일정은 해당 작업 시작 때 조회한다.
  이전 대화의 이슈 번호만으로 완료를 추정하지 않는다.

## 세션 종료 기록 형식

갱신일 / 실제 HEAD·PR / 완료 증거 / 미해결 사항 / 다음 한 가지 작업을 갱신한다.
테스트 실행 여부와 문서상 정책/구현 상태를 구분하고, 자격증명·계좌번호는 기록하지 않는다.
