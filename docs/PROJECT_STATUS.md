# 현재 상태와 세션 인계

갱신일: 2026-10-05

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
  익절 리뷰 3회 후 approve, PR #167로 2026-09-30 main에 병합됐다(`5ebeb76`). launchd는 아직 재설치하지 않았다.
- PR 3(평가기, 브랜치 `codex/165-freeze-paper-evaluator`): 10거래일 판정기와 CLI를 추가했다. 기록 TWR·공식·
  stress·운영비 차감 수익, 장중 최대낙폭, LONG·인버스 다리 분리(다리 합 = 계좌 FIFO 불변식), 판정 우선순위,
  INTERIM은 ELIGIBLE 불가, FINAL은 10세션 전부 검증 시에만 write-once.
- 벤치마크 근거: 저장소에 무수정 공식 일봉이 없다(Toss 1d·1m 모두 adjusted=True, 배당 조정 미검증). 따라서
  SPY 종가는 15:59(조기폐장 12:59) ET 1분봉을 쓰되, 그날 마감~자정 ET 사이 최초 기록되고 덮어쓰지 않았음을
  `candle_history.created_at/updated_at`으로 검증한다. 검증 불가·배당락 미기재(분기 패턴 대조)·가격 불연속은
  `INCONCLUSIVE_BENCHMARK`다. 공식 S&P 500 TR은 계속 미연결이다. 러너는 매 세션 SPY를 별도 비치명 단계로 수집한다.
  전체 pytest 2,224 passed / 11 skipped. PR 3은 #168로 main에 병합됐다(`160bbf4`).
- 첫 세션 활성화(브랜치 `codex/freeze-paper-first-session-activation`, PR #169로 2026-10-01 main 병합 `ee372c7`): 2026-10-01 사용자 결정으로
  첫 세션 2026-10-01, 구간 시작 종가일 2026-09-30, 예정 10세션 2026-10-01~10-14(검증기가 NYSE 달력에서 유도).
  `config/premarket_daily_brief.yaml`의 LONG·SHORT 승인 목록에 `premarket_freeze_long_intraday_v1`만 추가했고,
  첫 세션 영수증 `docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1_FIRST_SESSION_RECEIPT.json`을 작성했다.
  - SPY 배당·배당락 출처: State Street SPDR S&P 500 ETF Trust(SPY) 공식 펀드 페이지. 구간 내 배당락일은
    명시적으로 없음(`[]`), 배당 금액은 기록하지 않았다(`SPY_DIVIDENDS.json` 빈 목록 유지).
  - 운영비 0 USD(로컬 Mac 실행, Toss Open API 무료, Marketaux 무료 플랜; 공유 구독·개인 시간 제외).
  - 계좌 이벤트 수수료: 2026-10-01 Toss 앱 확인, 미국주식 0.1%(체결 총액 USD 10 이하 무료), 이벤트 할인 없음.
    계약 요율을 구간 전체에 적용한다.
  - 구간 시작 SPY 종가 증거: 2026-09-30 15:59 ET 봉 종가 763.75, 최초 기록 2026-09-30T20:20:22Z, 덮어쓰기 없음.
  - 코드 커밋 `160bbf4`. 운영 DB를 읽기 전용으로 연결해 `load_receipt`가 통과했다.
  - 정책 해시: 승인 목록 변경만으로 `policy_config` 해시가 `8cb5995d…` → `285e994b…`로 바뀐다(다른 필드 동일).
  - launchd freeze-paper 05:35·06:35·07:35 job이 설치·로드되어 있다(`--status` 확인). 2026-09-30 실행은
    영수증이 없어 `NOT_ACTIVATED` 운영 영수증만 남겼다(정상).
- 별도 데이트레이딩 탐색 준비(2026-10-01, read-only, 진행 중 Paper와 무관): 과거 intraday 시도 9건의
  실패 원인과 Toss 1분봉 종목별 D9 품질을 분석했다
  ([분석](research/ANALYSIS_DAYTRADING_FAILURES_AND_TOSS_DATA_QUALITY_20261001.md)). 과거 판정은 재판정하지 않았다.
  - 시도 9건의 내역: P0-US30 역산 7건, 보고값 2건(gross-edge screen, H4 랭킹).
  - 역산 gross는 −0.095~+0.119%/거래다. 수수료만 본 Toss 왕복 0.20%(슬리피지 포함 약 0.30%)에 닿은 데이 시도가 없다.
  - VWAP/ATR(후보 3)은 사전등록조차 되지 않았다.
  - 공식 D9를 저장소 코드로 재현했다(Gap+RVOL 1,296/20,100, ORB 1,277/20,100). 결손의 46%는 2024-07~08 수집 공백과 lookback 전파다.
  - 핵심 20종은 개별 ≤ 5%이고, 2024-09-03~2026-01-20 구간에서는 0.26%다.
  - blind OOS 구간은 봉 존재 여부로만 접촉했다(가격·수익률 미열람). 이 coverage 정보는 "이미 본 정보"로 기록한다.
  - US 1분봉은 2026-07-22~09-24에 공백이 있고, 이후 수집은 하루 4~5종목뿐이다.
- US 1분봉 수집 보강(2026-10-01, 진행 중 Paper 계약·운영 체크아웃 코드 비접촉):
  - 공백 복구: P0-US30 30종을 공식 backfill 경로(`--tail-repair`, PRE/REGULAR/POST, raw snapshot 보존)로
    2026-09-30까지 다시 읽었다. 운영 job과 같은 `data/.market_history.lock` 아래 종목별로 실행했고,
    시작 전 DB 백업은 `data/backups/market_history.pre-1m-gap-backfill.20261001.sqlite`(quick_check ok)이다.
  - 결과: 2026-07-22~09-30 50거래일에서 핵심 20종 REGULAR 50/50·결측 0분. SMH·XLE·XLU·VST도 50/50.
    DIA·XLK·XLF·XLV·XLI·XLY는 1~4분 결측이다(체결 없는 분 추정, 미검증).
  - 충돌은 2건뿐이다. XLV·XLU 2026-05-06 한 봉의 volume 차이였고, 둘 다 `kept_existing`이다.
    해당 봉은 blind OOS 구간이지만 기존 봉 재조회의 중복 처리일 뿐 가격·수익률을 분석에 쓰지 않았다.
  - 매일 수집: `scripts/run_history_collection.py`와 launchd `history-collect` 10:40·13:40 KST를 추가했다
    (브랜치 `claude/us-1m-collection-backfill`).
    - 수집 대상은 POST가 닫힌 마지막 거래일의 30종 40페이지 tail-repair다.
    - 종목별 backfill 오류가 있으면 COMPLETE 영수증을 쓰지 않는다. 독립 리뷰는 APPROVE_WITH_NITS였고 지적을 반영했다.
    - **launchd 미설치.** 운영 체크아웃 이동과 재설치는 사용자 결정 사항이다(권장: 10-14 Paper 평가 이후).
    - 그 사이 날짜는 첫 실행 전 `--max-pages`를 넓혀 따라잡는다.

## 다음 한 가지 작업

(2026-10-06 갱신 2) **R1 그림자 엔진 구현 완료(주문 없음), 첫 실시간 세션은 다음 세션(10-06 미장).** 기록: [R1 계획 §6](research/PLAN_DAYTRADE_R1_SHADOW_V1.md).
- 10-05 Paper 첫 자동 실행(10-06 11:07 KST): S4, 거래 0, 잔고 USD 2,000. 사용자 결정: 1일차로 판단하지 않고 지켜본다.
- 엔진: `src/daytrade_shadow/`(Paper 함수를 그대로 부르고 `walk_exit`만 봉 단위로 나눔) + `scripts/run_daytrade_shadow.py`(`replay`·`live`·`compare`).
  Paper 5세션 점검 중이라 Paper 코드는 바꾸지 않았다(계획과 달라진 점, 계획 §6에 이유 기록).
- 검증: 합성 13개 시나리오 재생 = `run_day`, Toss 모양 봉 경로 4개 = `run_day`, 실제 DB 800거래일(2023-05~2026-10) 모두 일치. 실시간 스모크(장외) 정상.
  `analyze`에 연결 단위 무음(10초·15초 초과 횟수) 추가 — 10-05 정규장 최대 1.38초.
- **다음(다음 세션):** 22:15 KST 무렵 `run_daytrade_shadow.py live` 시작 → 장 마감 뒤 `analyze`로 `STALE` 결정 → 10-07 11:07 Paper 뒤 `compare`.

(2026-10-06 갱신) **국장 웹소켓 수집 안정성 확인(사용자 요청, 시세만).** 09:47~10:47 KST, 005930·069500·122630·252670. 기록: [실시간 설계 §8](research/DESIGN_DAYTRADE_REALTIME_EXECUTION_V1.md).
- 분별 거래량 232개 분 모두 REST와 일치(누락 0%), 재연결 0.11초, REST 봉 끝 분 표시. 주의: 인버스2X 체결 공백 최대 88초, 삼성전자 호가 12.7%가 매도1 = 매수1(역전 0),
  체결 시각이 약 2초 앞섬, REST 429 3회(수집 작업과 한도 공유 추정). 국장은 여전히 보류 시장이다(Charter §3).
- 사용자 결정(2026-10-06): 탐색용 Paper 1일차 결과로 판단하지 않고 계속 지켜본다.

(2026-10-05 갱신 3) **실시간 엔진 R0 완료: 미장 정규장 웹소켓 실측(22:19~23:20 KST, 개장 뒤 50분).** 기록: [실시간 설계 §8](research/DESIGN_DAYTRADE_REALTIME_EXECUTION_V1.md).
- 결과: 웹소켓 체결 합계가 REST 1분봉 거래량과 개장 5분 뒤부터 172개 분 모두 정확히 일치(누락 0%), 개장 첫 5분은 ±0.1%(개장 단일가 체결이 경계 봉 하나를 옮겨 감).
  재연결 0.15초(1회, 깨끗한 종료), 그 분 누락 ≤ 0.26%. 호가는 정규장에서 약 2초마다 1건. 체결 지연 상한 중앙값 0.41초(초 단위 시각이라 상한값, 개장 첫 1분 최대 6.3초).
  진행 중 REST 봉은 정규장에서도 끝 분 표시이고 실시간으로 늘어난다(972회 중 969회). 거래량 급증 판정(20세션 같은 시각 평균 대비 1.5·2배)은 웹소켓과 REST가 4종목 × 49분 모두 일치.
- 도구: `scripts/measure_toss_realtime.py`(시세만, 주문·`personal:order` 없음) + 테스트 6개. 원자료는 git 밖.
- 제안(사용자 결정 전, 설계 §4는 그대로): `STALE` = 어떤 프레임도 10초 없음(지금 15초, 체결만 기준이면 장전 오경보). REST 대체 1초 유지(1초 연속 조회는 R1에서 확인).
- 한계: 1세션·평범한 날·미장만. 급변 날 누락과 국장은 미측정.
- 사용자 결정(2026-10-05): `STALE`은 하루 더 측정한 뒤 정한다(§4 15초 유지). R1 그림자 운용 준비를 시작한다(주문 코드 없음).
- **다음:** 10-06 11:07 KST 첫 Paper 자동 실행 확인 → 일지 회고. 10-06 미장 정규장 재측정 → `STALE` 결정. R1 구현은 [R1 계획](research/PLAN_DAYTRADE_R1_SHADOW_V1.md) §5 순서(공용 시각 함수·청산 스테퍼 → 실시간 공급·재생 테스트 → 첫 그림자 세션).

(2026-10-05 갱신 2) **Toss 1분봉 시각은 봉이 끝나는 분이다. 탐색용 Paper는 첫 세션 전에 표시 − 1분으로 읽도록 정정했다(사용자 결정).**
- 근거: QQQ 10-02 일봉 시가 751.31 = "09:31" 표시 봉 시가, "09:30" 봉 거래량 1.2만 주 대 "09:31" 47만 주, 실시간 REST 조회에서 "HH:MM+1" 봉이 HH:MM 동안 쌓임.
- 영향: 수집기 `session` 열의 정규장이 1분 앞당겨져 있다(프리마켓 마지막 1분이 정규장 첫 봉, 마지막 정규장 1분이 POST). Paper는 이 열을 쓰지 않고
  날짜별 마감 달력으로 다시 나눈다([계약 §6](research/CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1.md)). 10-02 재확인: 세 종목 390봉, QQQ 첫 봉 시가 751.31.
- **바꾸지 않은 것(별도 점검 대상, 이슈 #182):** 수집기 분류·수집 영수증의 분 수, 과거 연구(P0-US30·D9·데이트레이딩 분석 등)의 1분봉 사용. 지난 판정은 재판정하지 않는다.
- 웹소켓 시험(16:33 KST, 장외 2분): 체결·호가는 실시간으로 오고 봉 채널은 없다. REST 1분봉은 진행 중인 봉까지 준다. 기록: [실시간 설계 §8](research/DESIGN_DAYTRADE_REALTIME_EXECUTION_V1.md).

(2026-10-05 갱신) **탐색용 Paper 운영 안정화: launchd 자동 실행, 5세션 정상 주행 판정 고정, 실시간 엔진 설계.** PR #180.
- 정리: PR #178·#179 병합 확인(2026-10-04). `stage3-swing-run` 워크트리와 `claude/stage3-swing-*` 브랜치는 로컬·원격 모두 이미 없었다.
- 운영 점검: `premarket-operation.20261002.paper.json` = `NOT_ACTIVATED`(사유 `RECEIPT_INVALID:ACTIVATION_CONFIG_SHA256`, 승인 목록을 비워 설정 해시가 바뀐 결과로 의도대로).
  2026-10-03 10:40 KST history-collect(거래일 10-02) = `COMPLETE`, 30종 중 XLV·XLY만 389/390분.
- 자동 실행: `run_daytrade_paper.py --auto`(밀린 세션을 순서대로 따라잡기, history-collect 영수증 `COMPLETE` 전에는 exit 3, 사람이 필요하면 exit 4)와
  launchd `daytrade-paper-11`(11:07 KST)·`daytrade-paper-14`(14:07 KST 재시도). 테스트 67(관련)·전체 2,453 passed(리뷰 반영 전 기준) / 11 skipped, ruff 통과.
  실제 DB 스모크: `UP_TO_DATE`(마지막 마감 10-02). 일지 회고는 세션에서 쓴다.
- 판정: 사용자 결정(2026-10-05)으로 20세션 대신 **5세션(10-05~10-09) 정상 주행 점검**. 기준은 [계약 §7](research/CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1.md)에
  첫 세션 전에 고정했다. "진입→청산 1회 이상"을 넣어, 5일 내내 대기면 소액 실계좌로 가지 않고 다음 버전으로 계속한다.
- 실시간 엔진: [설계 v1](research/DESIGN_DAYTRADE_REALTIME_EXECUTION_V1.md)(설계만). R0 웹소켓 실측 → R1 그림자 운용(주문 없음) →
  R2 극소량 주문 시험 → R3 USD 500~800. R2부터 주문 코드라 별도 승인 전에는 만들지 않는다. 웹소켓 시험 결과는 아직 없다(10-05 22:47 KST 예정).
- v1.1 검토: 일지 0개(첫 세션 전)라 아직 제안하지 않는다. 5세션 판정 때 함께 본다.
- **다음:** 10-06 11:07 KST 첫 자동 실행 결과 확인 → 일지 회고 작성. 10-09 세션 뒤(10-10 KST) 5세션 판정. R1 시작 여부는 사용자 결정.

(2026-10-04 갱신 2) **탐색용 Paper(Charter v2.3 §4.6) 준비 완료. 2026-10-05 미국 세션부터 TQQQ/SQQQ, 장 마감 후 1분봉 가상 체결.**
- 규칙: [CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1](research/CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1.md) v1.0(§6 구현 해석 포함). 사용자 결정 2026-10-04: 10/5 시작, TQQQ/SQQQ 한 쌍.
- 코드: `src/daytrade_paper/`, `scripts/run_daytrade_paper.py`, `config/daytrade_events_v1.yaml`(FOMC·CPI·NFP·PCE·조기폐장·휴장, 공식 출처 확인). `e5d58c6` → 독립 리뷰 APPROVE(M3·L7)
  → 수정 `578dae3`(진입가 < 손절 거부, NYSE 달력 체인, 이전 세션 품질, 조기 실행 거부, `--out-root`, 실패 정리). 테스트 46개(전체 관련 83개) 통과.
- 시험 실행(판단에 쓰지 않음, 9/28~10/2): 4일 S4, 10/2 S1 판정이나 손절폭 1.58% > 1.5%로 진입 없음. 규칙이 엄격해 무거래일이 많을 수 있다.
- 실행: 10/6 11:07 KST 예약(세션 한정). 수동 실행: `scripts/run_daytrade_paper.py --date 2026-10-05 --db <checkout>/data/market_history.sqlite --out-root <checkout>`
  (먼저 `--backfill-check`). 결과는 원래 체크아웃 `reports/daytrade_paper/`, 일지는 `docs/journal/`.
- 참고 조사: 레버리지 지수 ETF 쌍 거래대금(3개월) — SOXL/SOXS $11.3B, TQQQ/SQQQ $5.9B, SPXL/SPXS $0.74B, UPRO/SPXU $0.55B, TNA/TZA $0.52B, 나머지 ≤ $0.24B.

(2026-10-04 갱신) **데이트레이딩 "그날 상황 판단" 설계안 정리(확정 전).** 근거: [fable 설계 리뷰](research/REVIEW_DAYTRADE_SITUATION_LAYER_20261004.md) `ee7eed1`,
[추가 입력 조사](research/RESEARCH_DAYTRADE_EXTRA_INPUTS_20261004.md) `026e163`.
- 방식: 하루 0~2번, TQQQ/SQQQ 한 쌍, 대기가 기본. 판단은 규칙, AI는 장 마감 후 일지·개선 제안만. 뉴스는 일지 기록용(판단 제외).
- 입력(무료): 전일 이동평균·ATR14, 장전 갭, 개장 15분 범위·VWAP·RVOL, VIX·VIX9D/VIX, 경제 일정(금지 신호). 추천 추가: 딜러 감마(GEX, SqueezeMetrics 일별),
  QQQ 상위 종목 폭(09:45), 옵션 만기·조기폐장 달력(모두 진입을 막거나 줄이는 필터).
- 상황 규칙 v1(판단값, 버전 관리): S0 이벤트 금지 · S1 상승 추세(TQQQ) · S2 하락 추세(SQQQ) · S3 갭다운 반등 · S4 조용한 날(대기) · S5 14:45 ±2%.
  위험 0.5%/거래, 명목 ≤ 35%, 최대 2포지션, 하루 −3% 중단, 15:55 청산. 손절은 Toss 서버 조건주문(OCO/OTO, 지정가) 1차 + 프로그램 감시 2차.
- 확인된 사실: Toss 웹소켓 실시간 체결·호가(us/kr) 있음, 조건주문 있음, PDT 규칙 2026-06-04 폐지.
- 일정(제안): 준비 약 2주 → Paper 2026-10-19 시작 → 20세션 뒤 한 번 판정(소액 실계좌 / 계속 / 중단 기준은 리뷰 §5).
- **사용자 결정 대기:** ① Charter v2.3 "탐색용 Paper 트랙" 추가 ② 추가 입력 3개 포함 ③ 판단 시각(09:45/10:00)·S3 포함·SOXL 추가 시점·과거 점검 여부·Paper 규모.
- 예약: Toss 웹소켓 장중 점검 미장 10/5 22:47, 국장 10/6 09:17(세션 한정 예약).

(2026-10-03 갱신 2) **사용자 결정: 스윙 폐기, 범위를 스캘핑~데이트레이딩 자동화로 되돌린다(Charter v2.2).**
- 이유(사용자): ① 스윙은 사용자가 직접 하고 있어 자동매매가 필요 없다(필요하면 별도 프로젝트). ② 처음 목적은 장을 계속 볼 수 없는 동안의
  스캘핑~데이트레이딩 자동화다. ③ 숏·상황별 전략 선택은 당일 매수·청산을 전제로 한 제안이었다.
- 스윙 증거(v1·v2·v3 점검·상황별 v1)는 그대로 보존한다. 스윙 숏(인버스 보유)은 v2 3i·5i·6i와 상황별 v1에서 모두 현금보다 나빴다.
- **다음 한 가지 작업: 데이트레이딩 연구 방향 정하기.** 잠근 [데이트레이딩 사전등록](research/PREREG_STAGE3_DAYTRADE_V1.md)은 2026-10-05부터 수집할
  1분봉으로 판정한다. 첫 데이터 전에 (1) 판정 기준의 검정력 점검(결과를 모르는 상태에서 가능), (2) 상황별 전략 선택·인버스를 일중 설계에 넣을지,
  (3) 데이터 확보(앞으로 수집 / Alpaca 무료 7년 IEX 1분봉 품질 점검 / 유료)를 정한다.

(2026-10-03 갱신) **상황별 전략 v1: 두 지수 모두 `FAIL`. 다음 방향은 사용자 결정.** 브랜치 `claude/stage3-swing-v3`.
- 경위: v3 기준 3 검정력 점검(결과 미열람 에이전트, [설계](research/DESIGN_STAGE3_SWING_V3_CRITERION3.md) `8ffebaf`)에서
  "10년 안 4,000 도달 확률"로는 어떤 보정 방식도 검정력 80%에 못 미쳤다(v1 방식 0%, 최고 16.7%, 이론 최선 5.6%). 선택 없음.
  사용자 결정(2026-10-03): 스윙 v3 대신 상황별 전략을 기한을 정해 한 번 시도. 오래된 데이터·장기 Paper는 쓰지 않는다.
- [사전등록](research/PREREG_STAGE3_REGIME_V1.md) `fd7feba`(사용자 확인) → 코드 `be5923f`(테스트 37 passed) → 독립 리뷰 APPROVE(Medium 3 반영 `6eb4bb7`)
  → 탐색 2006-07~2019 → [선택](research/SELECTION_STAGE3_REGIME_V1.json) 커밋 → 확인 2020~2025-07-18 1회 → [결과](research/RESULT_STAGE3_REGIME_V1.md) `23aef42`.
- 선택(낙폭 < 35%를 지킨 조합은 지수마다 625개 중 12개): S&P A1·B1·C현금·D현금, 나스닥 A1·B1·C현금·D2배.
- 확인 구간(USD 2,000 시작): S&P 3,156(연 8.6%, 낙폭 25.0%) vs 1배 4,185(14.3%, 33.8%). 나스닥 5,113(18.5%, 29.2%) vs 1배 5,360(19.5%, 35.1%).
  기준 1(1배보다 높은 성장) 두 지수 모두 실패. 기준 3은 나스닥 D 상황만 +0.0015(로그)로 성립, stress 비용에서는 음수.
- 참고(판정에 쓰지 않음): 같은 날 종가 체결이면 나스닥 6,706(연 24.4%, 낙폭 21.9%)으로 1배보다 높았다. 결과를 본 뒤의 관찰이므로 새 가설로만 다룬다.
- 중단 권고 조건(두 지수 모두 기준 3 실패)은 형식상 성립하지 않았다. 실질적 차이는 미미하다.
- 봉인 구간(2025-07-21~2026-07-21)은 열지 않았다. 남은 일: 2026-10-03 운영 점검, PR #178 병합(사용자).

(2026-10-02 밤 갱신 3) **사용자 결정: 데이트레이딩 보류, 스윙 집중. 작업 A(스윙 v3) → 작업 B(투표 방식 탐색) 순서.**
브랜치 `claude/stage3-swing-v3`(PR #178 위에 쌓음).
- 이유: v2의 후보 수 보정(3번) 방식은 assistant가 사전등록에서 정했고, 정하기 전에 "좋은 전략이면 통과하는가"(검정력)를 점검하지 않았다.
  최고 후보도 섞은 역사 약 99.8%에서 이겨야 하는 수준이라 통과 0개에서 배울 것이 적다. v2 판정은 그대로 두고, 고친 기준으로 새 버전을 돌린다.
- 데이트레이딩: 판정을 미룬다. history-collect 1분봉 수집은 비용이 없어 계속 둔다. 유료 1분봉 구입은 하지 않는다.
- **작업 A — 스윙 v3(30개 규칙 재시험), 2026-10-02 결정 사항:**
  - 데이터 확장: S&P 500을 1928년부터(`^GSPC` 일별, Ken French `rf`). 1988년 이전 배당은 추정한다(방식은 v3 사전등록에서 고정). 나스닥 100은 기존 구간을 유지한다.
  - 3번 기준 재설계는 결과를 모르는 에이전트가 결과 파일이 없는 시점(`d8d6f91`)의 별도 작업 공간에서 합성 데이터만으로 한다.
  - **기준 고르기 규칙(결과 전에 고정):** 진짜 우위가 없는 합성 시장에서 28개 후보 중 하나라도 통과할 확률 ≤ 5%를 만족하는 설정 가운데,
    미리 정한 "우위가 있는" 합성 시장에서 통과 확률이 가장 높은 설정을 고른다. 그 통과 확률이 80% 미만이면 고르지 않고 사용자에게 보고한다.
  - 1·2·4번 기준, 비용, 규칙 30개는 그대로다. 확장 구간의 시대 나누기(4번)와 배당 추정은 사전등록 초안에서 쉬운 말로 설명하고 사용자 확인 후 잠근다.
  - 결과에는 "결과를 본 뒤 기준을 고친 시험"이라고 적는다.
- **작업 B — 투표 방식 탐색:** 여러 규칙 중 롱 신호 수로 노출(0·1·2배)을 정하는 후보를 탐색 구간에서 찾는다.
  최종 시험 구간은 (다): 2015~2025-07-18은 이미 본 구간이므로 참고로만 보고, 판정은 blind OOS 2025-07-21~2026-07-21로 한 번 한다.
- 통과해도 Paper·실주문 승인이 아니다(Charter §4). 남은 일: 2026-10-03 운영 점검, PR #178 병합(사용자).

(2026-10-02 밤 갱신 2) **스윙 v2를 1회 실행했고 `NO_PASS`다. 스윙 30개 후보 연구는 사전등록대로 종결한다.**
브랜치 `claude/stage3-swing-run`, draft PR #178.
- v2 결정(사용자, 2026-10-02): v1 보정 실패 뒤 보정 상관만 주간(`W-FRI` 복리) 수익으로 바꾸고 나머지는 그대로 둔다.
  [v2 사전등록](research/PREREG_STAGE3_SWING_V2.md) §0에 이유와 "보정 기준을 1회 바꾼 시험"이라는 해석 조건을 적었다.
- 순서: 사전등록 `f7cd997` → 코드·테스트 `fb4d9b1`(24 passed, ruff 통과) → 독립 리뷰(opus) APPROVE(Medium 1: 원본 폴더 고정 → `--raw-dir` 명시로 반영,
  Low 2 기록만: 합격 판정이 주간 상관을 쓰는지 직접 보는 테스트 없음, 상수 수열이면 NaN이지만 실패 쪽으로 닫힘) → 1회 실행.
- 실행: `--raw-dir data/external/stage3_swing/20261002 --reps 1000`, 코드 `fb4d9b1`, dirty False. 원본 SHA-256이 v1과 같음을 확인했다.
  결과 [RESULT_STAGE3_SWING_V2](research/RESULT_STAGE3_SWING_V2.md)·`.json`을 수정 없이 커밋했다(`08fbf83`).
- 보정: 8개 모두 통과(SPY 주간 상관 0.9954, 일별 0.9847은 참고).
- 판정: 28개 후보 모두 3번 조건(부트스트랩 + Holm)에서 탈락. 가장 작은 원 p값은 나스닥 7번 0.354, S&P 5번 0.662.
  - 실제 역사에서는 1배보다 4,000 도달 확률이 높은 후보가 있었다. S&P 1배 47.3%에 비해 5번(10개월선 2배/현금) 88.0%, 8번 72.9%(중단 0%).
  - 그러나 과거를 블록으로 다시 섞은 역사 1,000개에서는 이 우위가 자주 사라졌다(S&P 5번은 66%의 역사에서 1배 이하).
  - 한계: 평균 250거래일 블록은 장기 추세를 일부 끊으므로 추세 규칙에 불리할 수 있다. 사전등록 조건이므로 판정은 그대로다.
- **다음 한 가지 작업:** 2026-10-03 운영 점검(`premarket-operation.20261002.paper.json` = `NOT_ACTIVATED`, 10:40 KST history-collect `COMPLETE`).
  그다음 데이트레이딩 1분봉 수집(2026-10-05~)을 이어간다. 스윙 쪽 다음 후보 연구 여부는 사용자가 정한다.

(2026-10-02 저녁 갱신) **단계 3 스윙 v1을 1회 실행했고 보정 점검에서 멈췄다(`CALIBRATION_FAIL`). 전략 결과는 없다.**
브랜치 `claude/stage3-swing-run`(main `d8d6f91` = PR #177 병합 commit 기준).
- 실행: `--fetch`(2026-10-02, 원본 14개, SHA-256은 결과 문서) → 계산 1회 `--reps 1000`, 코드 `d8d6f91`, dirty False.
  결과 [RESULT_STAGE3_SWING_V1](research/RESULT_STAGE3_SWING_V1.md)·`.json`을 수정 없이 커밋했다.
- 실패 내용: SPY만 일별 상관 0.9847 < 0.99. 연수익 차이는 +0.02%p. SSO·UPRO·SH·QLD·TQQQ·PSQ는 통과(차이 ≤ 1.21%p, 상관 ≥ 0.9926).
- 진단([기록](research/DIAG_STAGE3_SWING_V1_CALIBRATION_20261002.md), 읽기 전용): 오차가 1993~2009에 몰려 있고, 일별 차이가 다음 날
  되돌아간다(자기상관 −0.47). 주간 상관은 0.995. 지수와 SPY 종가 시점 불일치 잡음으로 추정하며, 레버리지 식 오류의 증거는 없다.
- v1 판정은 바꾸지 않는다. 보정 기준을 바꾸려면 새 버전(v2) 사전등록이 필요하고, 이는 사용자 결정이다.
- 2026-10-03 운영 점검은 아직 할 수 없었다(실행 시각 10-02 17:35 KST). `premarket-operation.20261002.paper.json`(10-03 05:35 KST 생성 예정)과
  10:40 KST history-collect 영수증은 다음 세션에서 확인한다. launchd freeze-paper-05/06/07·history-collect-10/13은 로드 상태, 마지막 종료 코드 0.
- **다음 한 가지 작업: 사용자가 보정 실패 후 방향을 정한다.** 선택지 ① v2(보정 상관만 주간 수익 기준으로 바꾸고 규칙·숫자는 그대로)
  ② v2(S&P 기준 수익을 `^SP500TR` 대신 SPY 실제 총수익으로) ③ 스윙 v1 종결, 데이트레이딩만 진행.

(2026-10-02 밤 갱신) **단계 3 사전등록 2개를 잠그고 스윙 시뮬레이터를 만들었다. 실제 데이터 계산은 아직 0회다.**
브랜치 `claude/stage3-prereg`(PR #176 위에 쌓음). 커밋 순서: 사전등록 → 코드·테스트 → 리뷰 반영.
- [스윙 사전등록](research/PREREG_STAGE3_SWING_V1.md): 지수 2개 × 규칙 15개, 1994-02~2015-07 시작 월 258개, 10년 안에 −35% 전 4,000 도달 확률.
  - **데이터 끝을 2025-07-18로 정정했다.** 저장소 스윙 blind OOS는 2025-07-21~2026-07-21이다(`src/history/oos_guard.py:45`).
    후보 기록의 "2026-01-20"은 일중 봉인 시작일과 혼동한 것이라 정정 표시를 붙였다. 스윙 봉인은 한 번도 열지 않았다.
  - 사용자 확인(2026-10-02): 지표 규칙은 다음 날 종가 체결로 판정한다(같은 날 종가는 참고만).
- [데이트레이딩 사전등록](research/PREREG_STAGE3_DAYTRADE_V1.md): ①②③을 2026-10-05 이후 1분봉으로만 판정한다.
  최소 거래 수 30·150·200, 최장 기다림 2028-10-04·2027-10-04·2027-10-04(assistant 제안, 첫 판정 데이터 전 변경 가능).
  - 사용자 확인(2026-10-02): ①의 S&P 다리는 수집 대상에 없는 UPRO·SPXU 대신 SPY 1분봉으로 계산한 3배 가격을 쓴다.
  - 판정 코드는 첫 판정 전에 만든다.
- 스윙 코드: `src/research/stage3_swing/`, `scripts/run_stage3_swing.py`, 테스트 22개(미래 정보·가상 레버리지·비용, 합성 데이터만).
  - 독립 리뷰(opus) 1회: Critical·High 0, Medium 1, Low 7.
  - 고친 것: Medium(7·8번이 매일 공짜로 비중을 맞추던 것을 월 1회 조정·비중 흐름 모델로), Low 1(1회 실행 보호: 회차 수·더러운 작업 트리·기존 결과가 있으면 거부).
  - 기록만 한 Low: Yahoo 시각 UTC 변환, 종가 없는 날 배당 누락 가능성, VIX 보충 일수 4일 미검사, 시대 경계 2일 누락, QQQ 보정은 구조상 항상 통과(독립 증거 아님).
- **다음 한 가지 작업: 스윙 1회 실행.** 별도 세션에서 `--fetch` → 계산 1회 → 결과 문서를 그대로 커밋한다.
  - 보정 점검이 실패하면 전략 결과 없이 `CALIBRATION_FAIL`로 끝난다. 그때는 사용자와 다음 판단을 정한다.
  - 부트스트랩 1,000회는 합성 데이터 기준 단일 프로세스로 약 7분이다.
- PR #176 병합은 사용자 몫으로 남아 있다(merge commit).

(2026-10-02 16시 이후 갱신) **단계 3 후보를 확정했다. 스윙과 데이트레이딩 둘 다 간다.** 사용자 결정이다.
근거: [후보 확정 기록](research/CANDIDATES_STAGE3_SWING_AND_DAYTRADE_20261002.md).
- 경위: assistant가 handoff·Charter 문구만 보고 스윙으로 좁혔다. 사용자가 데이트레이딩 희망이 빠졌다고 지적해 둘 다 하기로 했다.
- 스윙 후보는 30개다. 공개 규칙 15개 × S&P 500·나스닥 100.
  - 1993년 무렵~2026-01-20 전체를 가상 매매한다. 시작 월을 바꿔 반복해 "−35%보다 먼저 4,000 도달 확률"로 채점한다.
  - 사전등록 1페이지 + 프로그램 1개 + PR 1개 + 리뷰 1번으로 가볍게 한다.
- 데이트레이딩 후보는 3개다. 레버리지 ETF 장 마감 흐름, VWAP 극단 반등, Zarattini 5분 돌파.
  - 규칙을 먼저 잠그고, 앞으로 수집할 1분봉으로 판정한다. 공개 근거상 실패 가능성이 높다는 전제다.
- 유명 트레이더 전략도 조사했다. 제외 이유는 기록 문서에 있다.
- 같은 날 사용자가 네 가지를 정했다.
  - 채택 기준과 일일 손실 한도는 Charter v2.1 §2·§5에 기록했다.
  - Yahoo 사용을 승인했다(원본은 커밋하지 않는다).
  - history-collect를 설치했다.
- history-collect 설치(2026-10-02 16:40 무렵 KST, 사용자 승인):
  - 운영 체크아웃은 이미 `main` `1825d91`(origin/main과 같음)이라 이동하지 않았다.
  - 운영 체크아웃 전체 pytest는 2,377 passed였다.
  - `install_premarket_launchd.py --install` 후 `--status`에서 13개 job 모두 plist 존재·loaded를 확인했다.
    여기에 history-collect 10:40·13:40 KST가 포함된다.
  - 첫 실행은 2026-10-03 10:40 KST다. 마지막 수집(09-30) 이후 공백이 8일 미만이라 기본 페이지로 따라잡는다.
- **다음 한 가지 작업: 스윙 사전등록 1페이지 + 시뮬레이션 프로그램·테스트 + 독립 리뷰 1회.**
  - 커밋 순서는 규칙 → 코드 → 리뷰 → 1회 실행이다.
  - 데이트레이딩 3개의 사전등록도 같은 방식으로 쓴다.
  - 진행 방식(2026-10-02 사용자 승인):
    - 단계마다 세션을 나눈다.
    - 사전등록 1페이지를 그대로 구현 설명서로 쓴다.
    - 스윙과 데이트레이딩을 병렬로 진행한다.
    - 독립 리뷰는 1회다. Critical/High만 고치고, 사소한 지적은 기록만 한다.
    - 테스트는 미래 정보 사용, 레버리지 가상 계산, 비용 세 곳에 집중한다.
- 확인할 것(2026-10-03):
  - 05:35 KST 이후 `reports/premarket-operation.20261002.paper.json`이 `NOT_ACTIVATED`인지
  - 10:40 KST history-collect 영수증이 COMPLETE인지

(2026-10-02 저녁 갱신) **ROADMAP 단계 2 데이터 점검을 보고했다.** 근거: [점검 보고](research/DATA_AUDIT_STAGE2_20261002/README.md).
- 범위는 출처·기간·결측·조정 방식이다. 수익률·낙폭·신호 성과는 계산하지 않았다.
- **Toss 일봉 `adjusted=True`는 분할 조정만 뜻한다(배당 미포함).**
  - 근거: 2016-03-18~2026-01-20 공통 2,474일에서 Toss 종가 ÷ Yahoo 원종가가 SPY·QQQ·TQQQ·SQQQ·SOXL·SOXS 모두 1.0이다(변동계수 ≤ 6.3e-5).
  - 반면 Yahoo 배당 조정종가와의 비율은 움직인다(SPY 1.183 → 1.008).
  - 그러므로 기존 PROJECT_STATUS의 "배당 조정 미검증"은 "배당 미조정 확인"으로 바뀐다.
  - 기존 계약·판정은 재판정하지 않는다.
- 무료 출처는 이렇다. 기준 달력은 Ken French 일별 파일 날짜(CRSP 거래일)이고, 날짜 존재 여부만 대조했다.
  - Yahoo SPY 1993-01-29~, QQQ 1999-03-10~, `^SP500TR` 1988~, 레버리지·인버스 13종 2006~2010 상장 이후: 결측 0.
  - Cboe VIX 1990~: 결측 4일, 휴장일 행 33개.
  - Ken French 일별 RF 1926-07~2026-08: 결측 없음, 약 1개월 지연.
- 하락장 포함: 지수·SPY·VIX·금리는 2000~2002·2008·2020·2022 모두 포함이다.
  - 2배·−1배 ETF는 2006년부터, 3배 ETF는 2008-11(SPXL)~2010부터 있다.
  - 그 이전 구간은 가상 계산이 필요하다.
- 접근성 문제: FRED는 이 Mac에서 내려받기가 실패했다(연결 끊김). Stooq·Nasdaq은 키 없이 쓸 수 없다.
  Yahoo는 비공식 API라 원본을 커밋하지 않는다.
- 봉인 구간: 가격 비율은 2026-01-20 이전만 계산했다. 2026-01-21 이후는 날짜 존재 여부만 셌다("이미 본 정보"로 기록).
- 유료 공급자 가입은 하지 않았다. 원본 응답은 gitignore 경로에만 두었다.
- **다음 한 가지 작업: 단계 3 사전등록 초안.** 데이터 조합·배당 처리·가상 레버리지 계산식·개발/봉인 구간·비교 축을 고정한다.
  - 사용자 확정이 먼저 필요한 것: 전략 채택 지표 3가지, 일일 손실 한도 값과 그 전 비활성 보호 장치.
  - Yahoo(비공식, 재배포 금지)를 1차 출처로 쓰는 것도 사용자 확인 대상이다.
- 2026-10-03 05:35 KST 이후 `reports/premarket-operation.20261002.paper.json`이 `NOT_ACTIVATED`인지 확인하는 일은 그대로 남아 있다.

(2026-10-02 오후 갱신) **목표와 방향을 다시 정했다(Charter v2.0).** 사용자 결정이다.
- 1차 목표: 계좌 잔액 USD 2,000 → **4,000**(추가 입금 포함, 기한 없음, 달성 시 점검 후 다음 목표). 8,000·5,000도 검토했다.
- S&P 500 초과 목표는 보류한다. 레버리지로 쉽게 넘는 수치라 시드 성장 단계의 판단을 흐린다는 이유다.
- 계좌 중단선은 고점 대비 −35%로 바꿨다. 일일 손실 한도는 미정이다.
- 시장은 미국 상장 지수 ETF(1·2·3배, 인버스)로 정했다. 단일종목 레버리지와 국내 시장은 제외했다.
  - 근거: Toss 공지 2건(2026-05-22, 2026-07-31 시행).
  - 해외 지수 레버리지는 국내·해외 레버리지 ETP 매수 이력이 있으면 기본예탁금이 면제되고, 없으면 1,000만 원이 필요하다.
  - 국내 레버리지 면제는 약정 90일 경과와 (전월 레버리지 ETP 매수 1,000만 원 이상 또는 전월 평잔 500만 원 이상)을
    모두 요구해 현재 시드로 불가하다. 단일종목 레버리지는 2026-07-31부터 현금 3,000만 원이다.
  - 사용자의 매수 이력은 아직 확인하지 않았다(실거래 직전 확인).
- 방향: 시장 국면(롱/대기/숏)과 노출 배수 규칙으로 지수 ETF를 여러 날 보유하고, 판정이 바뀔 때만 매매한다.
  - 기존 판단 로직(`src/premarket/normalizer.py:463-466`: SPY·QQQ 200일선과 VIX 25/30)도 근거가 검증되지 않았다.
    그래서 결정 근거는 처음부터 다시 설계한다.
  - 후보 재료: 장기 추세(시계열 모멘텀, 이동평균), 변동성 기반 배수 조절.
  - 뉴스는 장기 이력이 없어 기록만 한다. LLM 판단은 과거 검증이 불가능해 매매 판단에 쓰지 않는다.
- 워크플로(수집·launchd·장부·영수증·사전등록 절차)는 재사용한다. 개별 종목 선택과 1시간 보유는 보류한다.
- 검토 후 보류한 후보: 13F 추종(GURU ETF 10년 연 12.29%로 S&P 13.67%에 뒤짐), 내부자 매수 추종, 모멘텀 종목 선택,
  돌파 매매, 실적 후 흐름, 소형 급등주 단타, 공유받은 AI 트레이딩 자료 3건(결정 근거 없음).
- **다음 한 가지 작업: ROADMAP 단계 2 데이터 점검.** 지수 장기 일봉·배당 조정, VIX, 단기 금리,
  레버리지·인버스 ETF 실제 가격의 출처·기간·결측을 확인한다.
- 미결(사용자 확정 전, assistant 제안):
  - 전략 채택 지표 3가지(비용 후 성장률, 최대 낙폭, 목표 전 중단선 도달 확률). Charter §2에 "미확정"으로 표시했다.
  - 일일 손실 한도를 정하기 전에는 새 전략을 활성화하지 않는 보호 장치.
  - 일일 손실 한도 값(논의 중 3% 제안, 미답).

이하 같은 날 앞선 기록은 보존한다.

(2026-10-02 12시 KST 갱신) **Paper를 일시 중지하고 전략을 원점에서 재검토한다.** 사용자 결정이다.
- 시점: v1.3 Paper 세션이 한 번도 실행되기 전이다(첫 freeze-paper 실행 예정은 2026-10-03 05:35 KST).
  v1.3 결과는 관측되지 않았으므로 재검토와 새 계약은 v1.3 성과의 영향을 받지 않는다.
- 이유: 청산 수치(손절 −0.75%·익절 +1.5%·60분 시간청산)가 2026-07 청산 기능 검증 설정(`e8c6d36`)에서
  근거 없이 이어졌고, 사용자가 진입·청산·비중·비용·평가 설계 전부를 처음부터 다시 보기로 했다.
- 방법: `config/premarket_daily_brief.yaml`의 LONG·SHORT 승인 목록을 다시 비웠다. 영수증이 결속한 config
  해시가 달라져 러너는 `NOT_ACTIVATED`(`RECEIPT_INVALID:ACTIVATION_CONFIG_SHA256`)로 끝난다.
  장전 FREEZE·post-close 관찰은 `NO_TRADE`로 계속 쌓인다(후보가 있으면 `NO_APPROVED_STRATEGY_TEMPLATE`,
  없으면 `NO_ELIGIBLE_CANDIDATE`).
- 계약 v1.3 JSON/MD와 영수증은 수정하지 않고 증거로 보존한다. v1.3 구간(10-02~10-15)은 0세션으로 종료한다.
  재검토 후 새 계약은 새 버전·새 영수증·새 10세션으로 시작한다.
- 뉴스 제목 관련성 필터(v1.3 ②)는 관찰 품질 개선이라 config에 그대로 둔다. 승인 목록이 빈 상태의
  policy_config 해시는 `88c317eb…`이다(테스트 고정값 갱신, 이전 `8cb5995d…`에서 이 필터만 다르다).

이하 v1.3 기록은 보존한다.

(2026-10-02 갱신) **v1.2 구간은 1일차 후 종료했고 v1.3으로 새 10세션(ET 10-02~10-15)을 시작한다.** 사용자 결정이다.
- v1.2 1일차(ET 10-01): `TRADED`, 평가액 2000 → 1995.28.
  - AMZN STOP_LOSS, MSFT TIME_EXIT, SPY는 EXPOSURE_ROOM으로 미진입. 투입은 약 38%였다.
  - 기록은 [PAPER_V1_2_CLOSED_RECORD](research/PAPER_V1_2_CLOSED_RECORD/README.md)에 보존했다. v1.3 판정에는 합산하지 않는다.
- v1.3 변경(Charter v1.5, 계약 revision_history 1.3)
  - ① 소수점 금액 매수: 6자리 내림, USD 1 미만 건너뜀, 1주 규칙 삭제.
  - ② 뉴스 제목 관련성 필터(`news_title_relevance_required`): 티커, 회사명 핵심, 고정 별칭.
  - ③ Marketaux 대기 30초, 응답 없는 재시도의 한도 미차감, 시도별 소요시간 기록.
  - 비용(1일차 손실의 약 1/3이 수수료)은 구조적 비용으로 기록만 했다.
- v1.3 영수증: 첫 세션 2026-10-02, 구간 시작 종가일 2026-10-01(SPY 763.745), 코드 `7a16f34`.
  운영 DB로 `load_receipt` 검증을 통과했다.

(대체됨, 위 v2.0 기록 참조) 다음 한 가지 작업은 사용자가 재검토 결과의 재설계 방향을 정하는 것이었다.
- 재검토 문서: [REVIEW_STRATEGY_FIRST_PRINCIPLES_20261002](research/REVIEW_STRATEGY_FIRST_PRINCIPLES_20261002.md).
  결론: 진입·청산·비중·평가 수치 대부분이 근거 없이 정해졌고, 저장소는 2026-07-21에 intraday~5일 틀(경로 A)을
  이미 종료했다. 선택지는 코어-위성, 경로 B(스윙·다개월) 연구, 일중 유지, 운영 검증 전용이다.
- 중지 변경은 21:01 KST(ET 10-02 PRIMARY) 전에 main과 운영 체크아웃에 반영한다.
- 이후 05:35 KST freeze-paper job은 `NOT_ACTIVATED` 운영 영수증만 남겨야 한다(첫 확인 2026-10-03).
- PR #171 history-collect job 설치는 Paper 일정과 무관해졌으므로 따로 정한다(공백이 8일을 넘으면 `--max-pages` 확대).
- (이전 계획, 중지로 대체됨) v1.3 10세션 실행 후 평가기 실행.
- 실거래 전 확인 사항: Toss 소수점 주문(금액 기준 시장가, 정규장~마감 1시간 전)과 레버리지 ETF의 소수점 거래 가능 여부를 확인한다.
실거래 전 레버리지·인버스 ETP의 브로커·규제 사전 조건을 확인한다.

### 병행 연구 (운영 체크아웃·Paper 계약 비접촉, ROADMAP 현재 단계 밖의 다음 후보 연구)

위 분석을 근거로 한 새 데이트레이딩 탐색은 사용자 결정으로 사전등록을 고정하고 실행했다(아래 screen v2).
- 2026-10-01 후보 탐색([문서](research/EXPLORATION_DAYTRADING_CANDIDATES_20261001.md)):
  - 조사 범위: 학술 문헌, 실전 트레이더 방법, development 구간 기회 크기(무조건부 움직임만, 손익 미계산).
  - 1차 gross-edge screen 후보:
    - A: 극단일 장 마감 레버리지 ETF 지속
    - B: Crabel NR7/Williams 변동성 돌파
    - C: 일봉 과매도 조건 장중 진입
    - D: VWAP/ATR 극단 회귀
  - E(실적 발표일)는 EDGAR 실적일 데이터 작업 후로 미룬다.
  - 2026-01-21 이후 가격은 읽지 않았다.
- 2026-10-01 gross-edge screen v2:
  - 사전등록 `PREREG_P0_US30_GROSS_EDGE_SCREEN_V2`는 r4로 FROZEN했다(`e164d20`).
    - 독립 리뷰 2회를 거쳤다.
    - 사용자 결정 사항: 8 arm, 공식 0.34%p 기준, 최근 1년 게이트 G8 추가, 단일종목 레버리지 ETF 제외.
  - 구현은 `src/research/gross_edge_v2/`이고, 독립 코드 리뷰 APPROVE_FOR_OFFICIAL_RUN을 받았다.
  - 공식 실행은 1회(`6ddd42b`)였다. **결과 `NO_GROSS_EDGE`**
    ([결과](research/RESULT_P0_US30_GROSS_EDGE_SCREEN_V2.md), payload `ee0a842f…`).
  - 실행 전 확인:
    - 조건 성립 횟수 대조에서 13개 중 12개가 독립 스크립트와 일치했다([증거](research/COUNTCHECK_P0_US30_GROSS_EDGE_SCREEN_V2.json)).
    - D 1건 차이는 조기폐장 다음 날 차단 규칙 때문이며, 구현 쪽이 계약에 맞는다.
  - arm별 거래당 gross와 거래 수:
    - A1 0.137 (185)
    - A2 −0.014 (418)
    - B1 −0.037 (1,245)
    - B2 −0.033 (626)
    - B3 0.030 (2,529)
    - C1 −0.014 (99, SAMPLE)
    - C2 0.059 (697)
    - D1 0.277 (407)
  - D1이 가장 높았다.
    - 무조건부 기준선보다 +0.256%p 높았다.
    - 하지만 G3 0.45에 미달했고, Holm p는 0.65였다.
    - 공식 0.34%p를 빼면 −0.063%p다(0.30%p −0.023, stress 0.51%p −0.233).
  - 인버스 다리 일부가 양(+)이었다: B3 inverse 0.659(n=105), C2 inverse 1.034(n=58), A1 inverse 0.422(n=90).
    다리 게이트를 전부 통과한 것은 B3 inverse뿐이다. A1·C2 inverse는 G1(n<100) 등에서 미달이다.
    Charter §4와 계약 §5.3에 따라 LONG 후보로 승인하지 않는다. 사후 관찰이며 새 arm으로 고르지 않는다.
  - 해석상 선택(구현 고정, 결과 전):
    - arm마다 독립 RNG(seed 20261001)를 쓴다.
    - `INVERSE_ONLY`는 arm 전체 통과이면서 비인버스 다리가 listing 규칙에 미달인 경우다.
    - G7 해당 없음은 등록 종목이 1개일 때만이다.
    - A1 임계 비교는 소수 9자리 반올림이다.
  - 다음:
    - 후보 A~D는 v2 범위에서 종결한다(`NO_GROSS_EDGE`, 재판정·개정 없음).
    - E(실적 발표일)는 EDGAR 실적일 데이터 작업이 먼저다.
    - 새 screen은 새 사전등록이 필요하다. blind OOS·H2는 계속 봉인한다.
    - 방향(데이 유지 또는 스윙·장기 코어 전환)은 10-14 Paper 판정 후 사용자가 정한다.

## 아직 충족하지 않은 실행 게이트

- 확정한 위험 한도의 코드 강제, 공식 총수익 벤치마크 출처/배당 검증.
- 사후평가 자동화 및 미래 표본 누적, 전략 사전등록 및 Paper 게이트.
- 계좌 연결 시 초기 자금 액수·실제 비용·주문 권한·사용자 실행 승인.
- 기존 이슈의 현재 open/closed 상태와 자동화 일정은 해당 작업 시작 때 조회한다.
  이전 대화의 이슈 번호만으로 완료를 추정하지 않는다.

## 세션 종료 기록 형식

갱신일 / 실제 HEAD·PR / 완료 증거 / 미해결 사항 / 다음 한 가지 작업을 갱신한다.
테스트 실행 여부와 문서상 정책/구현 상태를 구분하고, 자격증명·계좌번호는 기록하지 않는다.
