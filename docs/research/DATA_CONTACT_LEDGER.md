# 데이터 접촉 원장

_2026-10-16 시작(MINIMAL_PATCH_PLAN P3-d). 기간·종목·데이터 종류별로 무엇을 어떻게 봤는지 적는다. 새 접촉은 아래에 덧붙이고, 기존 줄은 고치지 않는다.
근거 문서의 문장을 고치지 않고 여기서 한정한다._

노출 유형: `performance_seen`(전략 손익·수익률을 계산함) · `price_only`(가격을 썼지만 전략 성과는 계산 안 함) · `metadata_only`(날짜·봉 수·출처만) · `unknown`(내용 미확인).
"설계로 전달"은 그 접촉이 이후 전략 설계에 쓰였는지다. 기록이 없으면 `미확인`이며 추정하지 않는다. 기록이 없다는 것이 접근이 없었다는 뜻은 아니다.

| 기록일 | 접촉 | 기간·종목 | 데이터 | 노출 유형 | 설계로 전달 | 근거 |
|---|---|---|---|---|---|---|
| 2026-10-16 | R1 그림자 재생(PR #186) | 2023-05~2026-10-05, QQQ·TQQQ·SQQQ, **일중 봉인 포함** | 1분 가격으로 v1.0 판단·손익 계산 | performance_seen | 미확인 | `PROJECT_STATUS.md` 274-275; `PREREG_DAYTRADE_V1_1_INTRADAY_REVERSAL.md` 137 |
| 2026-10-16 | Stage-2 데이터 점검 | 2016-03-18~2026-01-20 일봉, **스윙·일봉 봉인 2025-07-21~2026-01-20 포함** | Toss÷Yahoo 가격 비율(수익률 미계산) | price_only | 미확인 | `DATA_AUDIT_STAGE2_20261002/README.md` 4-5, 38, 102. `PROJECT_STATUS.md` 427 "스윙 봉인은 한 번도 열지 않았다"는 "성과로 연 적은 없고 가격 비율로 접촉"으로 읽는다 |
| 2026-10-16 | Stage-2 2026-01-21 이후 | Yahoo·Cboe ~2026-10-01 | 날짜 존재만 | metadata_only | 해당 없음 | 같은 README 103-104 |
| 2026-10-16 | 봉 존재·충돌 확인 | 일중 봉인 | 봉 수, 충돌 2건 volume 차이 | metadata_only | 해당 없음 | `PROJECT_STATUS.md` 121, 125-126 |
| 2026-10-16 | 백테스트 감사(2026-10-16) | QQQ 2026-07-07(일중 봉인) | 봉 시각·출처 | metadata_only | 해당 없음 | 감사 `EXPERIMENT_INVENTORY.json` `audit_metadata.audit_contacts` |
| 2026-10-16 | 2026-07 초기 실험(세션 001~007, H1~H4 등) | 일중 봉인 안 | 실험 실행(내용 미열람) | unknown | 미확인 | 감사 원장 `runs[early-2026-07-sealed]` |
| 2026-10-16 | H4 스프레드 인용 | 일중 봉인 안의 실험 | 봉인 구간 측정치가 봉인 밖 문서에 인용(수치 생략) | unknown | 미확인 | `ANALYSIS_DAYTRADING_FAILURES_AND_TOSS_DATA_QUALITY_20261001.md` 56 |
| 2026-10-16 | QQQ·SPY 1분봉 보충 2021-12~2023-03 | QQQ·SPY | 1분봉 성과: 미계산(봉 수·거래일 수만). 같은 기간 일봉 지수 수익률: 계산함(Charter v2.5 근거 등) | 1분봉 metadata_only, 일봉 performance_seen | 예(5년 규칙의 기간 선택 근거) | `PROJECT_STATUS.md` 139; `PROJECT_CHARTER.md` v2.5 이력. 독립 최종시험으로 선언하지 않는다 |
| 2026-10-16 | 연구 사본 `v13_engine_repro_20261016` 생성 | QQQ·TQQQ·SQQQ 1분봉 2023-04-11~2026-01-20 | 허용 목록 행만 복사(사본 SHA `378a1f42…`). B 구간 날짜 일부가 일봉 봉인 날짜와 겹침(1분봉, 일봉 성과 아님) | price_only(복사 단계) | 해당 없음 | `config/research_allowlist_v13_engine_repro_20261016.json`, 사본 매니페스트 |
| 2026-10-16 | v1.3 엔진 재현(수정 전후, CM020 진단) | 같은 사본, v1.1 마스크 A·B 채점일 | 기각된 v1.0+·v1.3 팔의 손익 재계산(이미 본 개발 구간) | performance_seen(재노출) | 아니오(엔진 검증, 전략 판정 없음) | `docs/research/RUNSPEC_V13_ENGINE_REPRO_20261016.md`, 실행 원장 |
| 2026-10-20 | 탐색용 Paper·그림자 로더 `store.load_bars`(PR #219 이전 코드) | 2026-10-05부터 PR #219가 사용자 체크아웃(launchd 실행 경로)에 배포되기 전까지의 실행일마다 120일 창(실행일 기준 약 4개월 전부터), QQQ·TQQQ·SQQQ, **일중 봉인 2026-06~07-21 일부 포함** | 1분 가격을 `prior`로 메모리에 읽음. v1.0 계산(SMA50·ATR15·RVOL20)에는 봉인 뒤 세션만 쓰임(실행일마다 봉인 뒤 세션 ≥ 50, `--backfill-check`로 10-05~10-16 확인) | price_only | 아니오(계산 입력 아님) | PR #219 리뷰. 배포 뒤 로더는 봉인 행을 읽지 않음 |
