# 결정 — ORB 종료 후 Gap + RVOL Continuation 독립 검증

> 작성: 2026-07-30 · 이슈 #98
> 성격: **전략 순서와 독립성 결정.** Gap+RVOL 성과·forward return·blind OOS는 미계산/미열람.
> 근거: `RESULT_P0_US30_ORB_DEVELOPMENT.md` · `DECISION_DAYTRADING_ORB_PRIORITY.md` ·
> `DATA_CONTRACT_4STRATEGY_V1.md` · `PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md`

## 0. 결정

ORB는 고정된 development gate에서 실패했으므로 규칙을 고치거나 blind OOS를 열지 않는다.
사전에 정한 두 번째 당일청산 후보인 **Gap + Relative Volume Continuation**을 별도 가설로
사전등록한다. 이번 단계는 규칙 잠금까지만 수행하며, 구현·development 실행·OOS 개봉은 각각
후속 게이트로 분리한다.

## 1. ORB에서 무엇을 배웠고 무엇을 재사용하지 않나

ORB primary는 공식 비용에서 expectancy·PF·MDD·fold 안정성·stress 손익이 development gate를
통과하지 못했다. 이 결과는 **opening-range 돌파 가설을 중단할 근거**이지, 같은 데이터에서
임계값을 바꾸며 재탐색할 권한이 아니다.

따라서 Gap+RVOL은 다음을 금지한다.

- OR high/low 돌파를 진입 조건으로 사용하지 않는다.
- OR low를 stop으로 사용하지 않는다.
- ORB의 gap/RVOL/target 이웃 결과를 보고 primary 임계값을 바꾸지 않는다.
- ORB sensitivity나 진단 변형을 새 전략으로 이름만 바꿔 재사용하지 않는다.
- ORB blind OOS를 열지 않는다.

## 2. 결과 전 고정값과 새 가설

후보 필터 `gap 0.30%`, 15분 `RVOL 1.5`, target `2R`은 ORB 결과 전에 이미 고정된 round
convention 값이다. 새 전략에서도 이 값은 그대로 둔다. 이는 ORB 성과를 이용한 임계 선택을
막기 위한 보수적 결정이다.

독립 가설은 **초기 눌림 후 재가속**이다.

1. 전일 정규장 종가 대비 당일 09:30 시가가 +0.30% 이상이고 첫 15분 RVOL이 1.5 이상이다.
2. 09:45 이후 gap의 절반 이상을 종가로 유지한 채 최소 한 번 전 봉보다 낮게 마감한다.
3. 이후 처음으로 완료 5분봉 종가가 직전 완료봉 고가를 strict 초과하고 causal session VWAP
   위에서 마감하면 continuation 신호다.
4. 다음 5분봉 시가로 진입하며, pullback 구간 최저가를 stop으로 쓴다.

여기에는 opening-range 고가 돌파가 없다. 같은 gap/RVOL 후보군을 쓰더라도 **진입 사건·stop·
보유 가설이 ORB와 다르다**.

## 3. 범위와 정직성

- 유니버스: P0-US30 고정 30종목. point-in-time 전체 시장이나 catalyst/news 전략으로
  일반화하지 않는다.
- 방향: long-only. 개별주 naked short는 없고 SQQQ/SOXS도 자기 자신의 gap-up setup만 거래한다.
- 보유: 당일청산, 오버나잇·재진입·물타기·scale-in 금지.
- 비용: 0.14/0.34/0.68%p, 공식 판정은 0.34%p.
- 개발/OOS: ORB와 같은 670일 development, 125일 blind OOS, 같은 표본·경제성·안정성 게이트.
- 이전 결과 접근: ORB development 결과는 읽었다. **Gap+RVOL standalone 신호·거래·수익률은
  계산하거나 열람하지 않았다.**

## 4. 실행 순서

1. 이 결정과 `PREREG_P0_US30_GAP_RVOL_V1.{md,json}`을 main에 병합한다.
2. 별도 issue/branch에서 구현하고 사전등록↔구현 매핑·look-ahead 음성 테스트를 만든다.
3. 독립 구현 리뷰와 snapshot 4-hash 검증을 통과한 뒤 development-only를 한 번 실행한다.
4. development gate 실패 시 `NO_SELECTION`으로 끝내고 OOS를 열지 않는다.
5. 통과한 경우에만 #78의 추적 unlock artifact로 blind OOS를 한 번 연다.

이번 문서와 사전등록 PR은 2~5를 실행하지 않는다.
