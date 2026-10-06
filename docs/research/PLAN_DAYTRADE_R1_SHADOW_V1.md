# R1 그림자 운용 계획 v1 (주문 없음)

작성 2026-10-06 · 상태: **구현 완료(2026-10-06), 첫 실시간 세션 전 — §6** · 근거: [실시간 설계 §6·§8](DESIGN_DAYTRADE_REALTIME_EXECUTION_V1.md),
규칙 정본 [탐색용 Paper 계약 v1.0](CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1.md) · 사용자 결정 2026-10-05: "R1 준비 시작".

이 문서는 주문을 승인하지 않는다. R1 코드는 Toss 주문 엔드포인트를 부르지 않고 `personal:order`를 구독하지 않는다.

## 1. 목표와 통과 조건

장중에 실시간으로 Paper와 **같은 규칙 함수**를 불러 판단·가상 체결을 기록하고, 다음 날 Paper(장 마감 뒤 계산) 결과와 맞춘다.

통과(설계 §6 R1): 같은 날 Paper와 **상황(S0~S5)·진입 시각·수량**이 일치. 불일치는 원인을 기록한다.
비교 대상은 Paper의 5세션 점검과 같은 기간부터 시작할 수 있다(Paper 판정에는 영향 없음, 서로 입력을 공유하지 않는다).

## 2. 재사용(규칙 정본 1곳)

`src/daytrade_paper`에서 그대로 부른다. 모두 "그 시각 전 봉만" 쓰는 함수다(`before(bars, T)`).

| 용도 | 함수 |
| --- | --- |
| 장전 입력(전일 SMA50·ATR14·갭·RVOL 기준) | `core.preopen_inputs`, `store.load_bars`(전일까지), `external.fetch_external`(VIX·VIX9D·GEX 전일 값), `events_for` |
| 판정 | `situation.opening_signals`, `classify_open`(S1/S2 09:45, 지표일 10:15), `classify_s3`(10:00/10:15), `classify_s5`(14:45) |
| 크기·체결 | `simulate.size_notional`, `_open_trade`(T 봉 시가 + 0.05%), `_mark_pnl`, `closed_by`, `vwap_through` |

장전 입력은 모두 개장 전에 준비된다: 전일 1분봉(history-collect 10:xx KST), 전일 Paper 장부(11:07 KST), VIX 등 전일 값.

## 3. 새로 만들 것

1. **봉 시각 변환 공용 함수.** `store._bar`의 "표시 − 1분"을 `core`로 빼서 store와 실시간 공급이 같이 쓴다(설계 §8 요구).
   `scripts/measure_toss_realtime.bar_start`도 이것으로 바꾼다.
2. **실시간 봉 공급.** 판단 시각 T + 3초에 `TossClient.get_candles(sym, "1m", 200)` → 표시 − 1분 변환 → `t < T`인 완성 봉만 판단 입력,
   T 봉은 진입 시가용. R0에서 끝난 봉의 사후 수정 0건이었다. 3초 대기는 진행 중 봉을 완성 봉으로 오인하지 않기 위한 여유다.
3. **청산 스테퍼.** `walk_exit`(한 번에 15:55까지 도는 함수)를 "봉 하나 받으면 상태 갱신"하는 형태로 나눈다. Paper도 같은 스텝 함수를 쓰게 바꿔
   두 경로가 갈라지지 않게 한다(기존 Paper 테스트 그대로 통과가 조건).
4. **장중 데이터 품질 검사.** `data_quality`는 하루 전체 390봉을 요구한다. T까지의 봉만 보는 변형을 둔다(빠진 분 → 그 판단은 대기, 사유 기록).
5. **웹소켓 감시(기록만).** R0 측정 코드를 재사용해 체결·호가를 받고, 손절가 도달 시각을 기록한다. 판단·가상 청산은 REST 봉 기준(Paper와 같게)이다.
   같은 연결로 매일 `analyze`를 돌려 누락률·`STALE` 후보 숫자를 쌓는다(사용자 결정: STALE은 하루 더 측정 후 결정).
6. **가상 체결 저널.** append-only JSONL(`reports/daytrade_shadow/<date>.jsonl`, git 밖): 판단 시각, 입력 요약, 상황, 진입·청산(시각·가격·수량·사유), 웹소켓 손절 감지 시각.
7. **대조 보고.** D+1 Paper 실행 뒤 `reports/daytrade_paper/<date>.json`과 비교: 상황·진입 시각·명목·손절가·청산 시각·사유. 결과를 일지에 붙인다.
8. **실행.** launchd 1개(22:20 KST 시작, 장 마감 뒤 종료), `caffeinate`. 실패해도 Paper에 영향 없음.

## 4. 갈라질 수 있는 곳(미리 정한 처리)

| 원인 | 처리 |
| --- | --- |
| 개장 경계 1분의 거래량 배정(R0: SQQQ 8,675주가 09:29↔09:30) | REST 봉만 쓰므로 Paper DB와 같은 출처. 수집 DB와 실시간 REST가 다르면 불일치로 기록 |
| 너무 일찍 받은 봉 | T + 3초 대기, 맨 앞 봉이 T 이후 표시인지 확인 |
| 장부(equity_start)가 늦음 | 전일 Paper 장부가 없으면 마지막 장부 잔고로 계산하고 `equity_source: UNVERIFIED`로 기록(구현 2026-10-06). 대조 때 수량 차이의 원인으로 본다 |
| 장전 PRE 봉 누락 → 갭 출처 바뀜 | Paper와 같은 대체 규칙(09:30 시가). 사용한 출처를 저널에 적음 |
| 웹소켓 손절 감지 vs 봉 저가 | 판단은 봉 저가. 차이는 "실계좌였으면 몇 초 빨랐나"로만 기록 |
| 14:45 평가(14:44 종가) | 14:45:03에 조회 |

## 5. 순서와 일정(제안)

1. 공용 시각 함수 + 청산 스테퍼 리팩터(Paper 테스트 그대로 통과) → PR.
2. 실시간 봉 공급 + 장중 품질 검사 + 저널 + 실행 스크립트(재생 테스트: 지난 날 1분봉을 실시간처럼 흘려 Paper 결과와 100% 같은지) → PR.
3. 웹소켓 감시·대조 보고 → 첫 그림자 세션.
재생 테스트(과거 날짜를 분 단위로 흘려 Paper와 비교)가 실시간 전 가장 큰 안전장치다. 이게 통과하면 실시간 불일치 원인은 데이터 출처뿐이다.

## 6. 구현 결과(2026-10-06)

**계획과 달라진 점(사용자 결정이 아닌 구현 판단, 이유 기록):** §3-1·§3-3은 Paper 코드(`store._bar`, `walk_exit`)를 고쳐 공유하자고 했다.
Paper는 지금 5세션 정상 주행 점검(10-05~10-09, 계약 §7) 중이라 **Paper 코드는 한 줄도 바꾸지 않았다.** 대신 그림자 엔진이 Paper 함수를 그대로 부르고,
`walk_exit`만 "봉의 시가 단계 → 종가 단계"로 나눠 같은 순서로 다시 구현했다. 같은 결과인지는 재생으로 증명한다(아래). 공유 리팩터는 점검이 끝난 뒤 별도 PR로 할 수 있다.

| 구성 | 파일 | 내용 |
| --- | --- | --- |
| 엔진 | `src/daytrade_shadow/engine.py` | `ShadowDay.step(now, 완성 봉, 진행 중 봉 시가, PRE 봉)`. 판단 시각(09:45/10:15, 10:00, 14:45)에 Paper 함수 호출, `ExitWatcher`가 `walk_exit` 순서대로 청산. 판단 시각을 놓치면 늦게 판단하지 않고 중단 |
| 장중 데이터 검사 | `live_quality` | 과거 세션 검사는 `data_quality` 그대로, 오늘은 개장 구간 [09:30, 판단 시각) 봉이 모두 있는지 |
| 실시간 봉 | `src/daytrade_shadow/bars.py` | Toss REST 봉(끝 분 표시) → 시작 분. `store._bar`와 같은 결과임을 테스트로 고정 |
| 재생 | `src/daytrade_shadow/replay.py` | 끝난 세션을 "그 분에 알 수 있던 것만" 흘려 `run_day`와 항목별 비교(`data_quality`만 제외) |
| 실행 | `scripts/run_daytrade_shadow.py` | `replay` / `live`(분마다 +4초 REST 3종목, 웹소켓 체결로 손절선 터치 기록) / `compare`(다음 날 Paper와 대조) |
| 측정 | `scripts/measure_toss_realtime.py` | `analyze`에 연결 단위 무음(`stale_any_frame`: 10초·15초 초과 횟수) 추가. 10-05 정규장: 최대 1.38초, 초과 0회 |

**검증**

- 합성 시나리오 13개(S0~S5, 갭 손절·VWAP·S3 목표·15:55 강제 청산, 일일 손실 중단, 합산 상한, 위험 모드 미확인, 지표일): 재생 결과 = `run_day`. Toss 모양 봉 → `LiveBars` → 엔진 경로도 4개 시나리오에서 같음.
- 실제 DB 재생(고정 VIX, 사건 달력은 2026년만): 2025-10-01~2026-10-02 249거래일 **모두 일치**(Paper 데이터 검사 불통과 4일 제외). 상황 분포 S4 212, S4+S5 18, S2 7, S3 6, S1 3, S3+S5 2, S2+S5 1.
- 실시간 스모크(2026-10-06 15:41 KST, 장외): 장부 연결(전일 10-05, USD 2,000)·DB 전 세션·VIX 조회·REST 병합·웹소켓 기록 정상.
- 아직 안 한 것: 정규장 실시간 1세션 전체. 첫 세션이 곧 R1 첫 결과다.

**실행 방법(다음 세션)**

1. `websockets` 설치: `pip install --target <job tmp>/wslib websockets`(프로젝트 의존성 아님).
2. 22:15 KST 무렵(09:15 ET, 09:25 ET 전) 원래 체크아웃에서 백그라운드로:
   `PYTHONPATH=<job tmp>/wslib caffeinate -i .venv/bin/python scripts/run_daytrade_shadow.py live`
   → `reports/daytrade_shadow/<date>/`에 `events.jsonl`(판단·가상 체결), `result.json`(장 마감 뒤), 웹소켓·REST 로그.
3. 장 마감 뒤 측정: `.venv/bin/python scripts/measure_toss_realtime.py analyze --out-dir reports/daytrade_shadow/<date> --open-utc <date>T13:30:00+00:00`
   (누락률·`stale_any_frame` → `STALE` 결정 근거).
4. 다음 날 11:07 KST Paper 실행 뒤: `.venv/bin/python scripts/run_daytrade_shadow.py compare --date <date>` → `compare.json`(통과 = 상황·진입 시각·수량 일치).

## 7. 하지 않는 것

주문·조건주문·잔고 조회, AI 판단, 규칙 수정(v1.0 그대로). R2(극소량 주문)는 별도 승인.
