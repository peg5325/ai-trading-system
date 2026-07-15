# Design Delta + #006R/#007 — 구현 계약서 (v2.1, Claude↔GPT 수렴·조건부 승인)

> 초안 2026-07-10 · v2 수렴 2026-07-12 · **v2.1 (GPT 조건부 승인 + 데이터 계약 보강) 2026-07-12**
> 상태: **구현 착수 가능(조건부).** 아래 §2 "4대 필수 명문화"가 PR에 반영되는 조건.
> 한 줄: **A1(Market Context) 계측 + 무결성/버전/조인 계약을 심어, 다음 세션부터 국면 신호를 반복 검증 가능하게. 이번 주말은 증명이 아니라 "측정 장비 + 데이터 계약" 완성.**

<!--
  이 문서는 v2.1 구현 계약서의 저장소 tracked 정본이다.
  원본은 gitignore 산출물(`reports/design_delta_review.md`)에 있어 원격 계약 문서가 아니었으므로,
  Phase 0 · PR 0-B 에서 docs/ 아래로 보존했다. 이후 PR(Session Isolation / Instrumentation / Shadow)의 참조 기준.
  실행 순서 계획은 `.omx/plans/v2-1-claude-code-execution-order.md`(작업용, gitignore).
-->

---

## 0. 검증 범위 (정직 고지)

- **#006R 원천 데이터 — Claude 아직 미보유**(환경엔 #004 것만). GPT 재집계 #006R 수치는 Claude 독립 검증 불가. 제공 시 #005와 동일 절차로 재현 검증. *(원본 전달 후: "재현 검증 완료"로 갱신 예정.)*
- **검증한 것:** ① 비용 논리 정합(#004 거래당 727 KRW·슬리피지 71.4%=config 5/7·#006R cost/gross 25.2%). ② coverage 근거 정합(정규장 6.5h/5m=**78봉**, #006R 43봉=**55.1%**).

---

## 1. Claude↔GPT 합의 (그대로 진행)

우선순위:
```
Session Isolation → Versioned Instrumentation → Market Context Shadow
→ Backfill/Historical Eval → 그 후에만 Dynamic Universe / SOXL-SOXS / AI
```
- **A1 먼저 계측·예비검증**, A2/A3/A6 런타임 미투입(설계만). *(※ "A1 먼저 증명"이라는 초안 표현은 폐기 — H3는 이번 주말 PRELIMINARY.)*
- P0 Session Isolation Guard(무결성 실패 재발 방지) · H3 PRELIMINARY 유지 · 국면 관측(매 cycle)/행동 결정(세션 1회, warm-up 후) 분리 · Cooldown 기본 OFF · A5는 replay 아닌 counterfactual · SOXL/SOXS 승격 5조건 · Instr. ON/OFF 주문시퀀스 동일 · random CV 공식경로 배제.

**SOXL/SOXS 승격 5조건**(비용반영·OOS 동시): ① SOXL-only 우월 ② 하락국면 CASH 우월 ③ 시장/국면 OOS 재현 ④ unlevered(QQQ) 대비 위험조정 우월 ⑤ turnover/cost가 성과 잠식 안 함. (인버스 3x는 왼쪽꼬리 두꺼움 → worst-session·연속손실 프로파일 필수.)

---

## 2. ★ 4대 필수 명문화 (구현 착수 전제)

**GPT 조건부 승인의 핵심. 이 넷이 PR에 반영돼야 함.**

### 2-1. Session 상태를 직교 필드로 분리
`RESTARTED`는 완료상태가 아니라 시작방식. #006R = RESTART(시작) ∧ PARTIAL(완료).
```yaml
session_id: "006"
run_id: "006R"
parent_run_id: "006"
start_mode: RESTART          # FRESH | RESUME | RESTART
completion_status: PARTIAL   # FULL | PARTIAL | ABORTED | FAILED
termination_reason: NETWORK_SCHEDULE
regular_expected_bars: 78
regular_observed_bars: 43
regular_missing_bars: 35
coverage_pct: 55.13
manual_flatten_count: 3
# [Claude 보강] 결측 위치 편향 판별
missing_bar_zones: {open: true, mid: false, close: true}
```
> **Claude 보강:** 결측을 개수만 저장하면 편향 방향을 못 읽는다. #006R은 정보량 큰 개장·마감이 빠졌다 → `missing_bar_zones`(또는 결측 bar index 목록)로 분석이 편향을 판단하게.

### 2-2. 공통 조인키 = candidate_id (trade_id는 nullable)
기존 `cycle_id+symbol+trade_id`는 미체결 후보에 trade_id가 없어 공통키 불가.
```yaml
candidate_id: hash(session_id, run_id, decision_timestamp, cycle_id, symbol)  # 항상 존재
session_id / run_id / cycle_id / symbol: 항상 존재
trade_id: 체결 시에만, 아니면 null
# 관계: candidate_id →(optional) trade_id → exit_id / followup_id
```

### 2-3. Market Context는 trailing·as-of 명시 + 완료봉만
`ret30/60m`은 미래수익률로 오독 위험 → look-ahead 사고 유발.
```yaml
benchmark_return_trailing_30m:
benchmark_return_trailing_60m:
benchmark_context_asof:
benchmark_source_bar_end:
benchmark_timeframe:
benchmark_ema_period_bars:
# 불변식: benchmark_source_bar_end <= candidate_decision_time
#         market_context_asof     <= candidate_decision_time
# 진행 중 현재 봉 사용 금지(완료봉만).
```
> **Claude 보강:** "완료봉만" 규칙을 **Paper·Backtest 양쪽이 동일**하게 적용(패리티). 안 그러면 shadow 평가와 향후 live 결정이 어긋난다.

### 2-4. Shadow 결정(온라인) ↔ Outcome(오프라인) 분리
avoided_loss·missed_opportunity·forward_return은 판단시점 미지.
```yaml
# Runtime(온라인) — hypothetical decision만
shadow_policy_version / would_allow / shadow_reason_codes /
decision_time / market_context_snapshot / candidate_ids   # 사이클 단위 리스트(아래 조인 규약)
# Offline batch(세션 후, read-only) — 결과 부착
forward_return_30m/60m / avoided_loss / missed_opportunity /
outcome_complete / horizon_end
```
> **조인 규약(PR 4-A 구현 반영):** 정책 결정은 벤치마크 국면 단위(사이클당 A/B 1쌍)라 사이클 내 모든 후보에 동일 적용된다. 따라서 온라인 레코드는 단수 `candidate_id` 대신 **사이클 단위 `candidate_ids` 리스트**를 담고, **오프라인 batch(4-B)가 리스트를 per-candidate 로 fan-out** 해 각 candidate_id 에 동일 국면 결정을 부여한다(국면-단위 결정을 후보마다 복제하지 않아 컨텍스트 중복 회피). 조인키 자체는 여전히 candidate_id(§2-2).
> **Claude 보강:** 오프라인 batch는 기존 exit-followup의 **엄격 look-ahead 윈도잉(bisect_right, `> decision_time`) 재사용**. 세션말 잘린 미완결 horizon은 `outcome_complete=false`로 **집계 자동 제외**(조용히 drop도, 0 포함도 금지).

---

## 3. GPT 7개 보강 — 전부 수용

| # | 보강 | 상태 | Claude 보강 |
|---|---|---|---|
| 1 | session 상태/시작방식 직교 분리(§2-1) | 수용 | 결측 위치(open/mid/close) 저장 |
| 2 | candidate_id 상시·trade_id nullable(§2-2) | 수용 | — |
| 3 | trailing/as-of 명시(§2-3) | 수용 | Paper=Backtest 완료봉 규칙 통일 |
| 4 | shadow/outcome 온·오프라인 분리(§2-4) | 수용 | 미완결 horizon 자동 제외 |
| 5 | **resume=true 허용조건 엄격** | 수용 | 아래 |
| 6 | **계측의 간접 행동변경 방지** | 수용 | 아래 |
| 7 | **random CV 금지 범위 정의** | 수용 | 아래 |

**#5 resume 조건:** 기존 DB 존재만으로 fail-fast하되 정상 크래시 복구는 지원. resume은 `session_id·run_id·config_hash·producer_commit·schema_version·market·currency·initial_equity·broker_mode·ai_provider` 전부 일치 시에만. 코드/config 변경 이어달리기는 별도 `allow_resume_with_code_change: false`(실험세션 항상 false).

**#6 간접 행동변경:** 주문 시퀀스 동일만으론 부족 — benchmark fetch가 API예산·지연·candle freshness를 바꿀 수 있음.
- benchmark fetch = **거래 데이터와 분리된 경로·별도 예산·best-effort**(실패해도 매매 무중단).
- context 결측 → null 기록 후 진입 허용(**관측이 거래를 게이팅 금지**).
- **US부터(QQQ 이미 universe, 추가호출 0)** 시작, **KRX benchmark(새 종목·추가호출)는 격리 fetch 경로 준비 후**로.

**#7 random CV:** 공식 평가/모델선택 경로 금지, 단 진단모드 `--unsafe-random-cv`(+"INVALID FOR MODEL SELECTION" 워터마크)로 누수 시연용 보존. (H0에서 실제 사용한 진단.)

> **Claude 추가 제안:** 위 불변식들을 산문이 아니라 **실행 가능한 테스트**로 박을 것 — candidate_id 상시존재, trade_id nullable, `source_bar_end <= decision_time`, `Σcomponent + adjustment == candidate_score`, 미완결 horizon 제외. 계약이 CI가 강제하는 보증이 된다.

---

## 4. 최종 PR 구조 & 의존관계

```
PR 1  Session Isolation Guard            (독립)   ← §2-1, resume(§3-5)
PR 2  Versioned Instrumentation + MC     (독립)   ← §2-2, §2-3, 간접행동방지(§3-6)
PR 3  Shadow Decision + Offline Outcome  (PR2 의존) ← §2-4
PR 4  Cooldown 수정 + A/B                 (독립)   ← trading_bars|elapsed_minutes, reset_on_new_session
```
- **behavior-change = false** 전제. A2·A3·A5·A6은 런타임 코드 미투입(설계·I/O 계약만).
- **Cooldown 기본값:** EOD flatten 전략이라 오버나이트 포지션 없음 → `reset_on_new_session: true`가 사실상 옳은 기본(단 명시 필수).

---

## 5. 이번 주말 완료 기준 (확장)

```
[ ] 새 세션이 과거 DB를 실수로 재사용할 수 없다 (fail-fast)
[ ] #006 namespace 충돌이 자동 테스트에서 재현되고 fail-fast 된다
[ ] 동일 session의 정상 crash/resume 은 성공한다
[ ] session completion 상태와 start_mode 가 분리 저장된다
[ ] candidate_id 는 모든 후보에 존재하고 trade_id 는 nullable 이다
[ ] market_context source timestamp 가 decision timestamp 보다 미래가 아니다
[ ] trailing return 은 완료봉만 사용한다
[ ] Instrumentation ON/OFF 에서 주문/후보/AI/risk/position 시퀀스가 동일하다
[ ] context 누락/benchmark API 실패가 live order 를 차단하지 않는다
[ ] shadow outcome 은 세션 종료 후 batch 에서만 생성된다
[ ] incomplete forward horizon 은 성과 집계에서 자동 제외된다
[ ] PARTIAL/MANUAL_FLATTEN 세션을 분석 리포트에서 필터링할 수 있다
[ ] 공식 평가 runner 는 GroupKFold/LOSO/chronological/embargo 만 허용한다
[ ] random CV 결과는 모델 선택·승격 판정에 사용할 수 없다 (진단모드만·워터마크)
[ ] 핵심 불변식이 unit test 로 강제된다
[ ] A2/A3/A5/A6 은 runtime 코드에 들어가지 않는다
```

---

## 6. 문서/타이밍 메모

- **#007:** 본 문서 #007 권고는 2026-07-10 기준 → 현재 7-12이므로 **"Session #007 실행 권고(historical)"**로 읽을 것. 산출물 확보 시 baseline에 추가.
- **성공의 정의:** 기능 개수가 아니라 위 체크리스트 충족. *국면 신호가 shadow/replay에서 값을 증명하기 전까지 Universe·방향전환·2단계 AI는 설계만. 이번 주말은 A1을 계측하고 H3 예비관측을 반복 검증 가능하게 만드는 "측정 장비 + 데이터 계약"의 완성이다.*
