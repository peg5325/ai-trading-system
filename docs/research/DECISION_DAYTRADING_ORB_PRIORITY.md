# 결정 — 데이트레이딩(당일청산) 우선 + 첫 검증 전략 = ORB

> 작성: 2026-07-23 (Claude) · 이슈 #75 · Phase 1B
> 성격: **의사결정 문서.** 이번 달 전략 검증의 **우선순위**를 기록한다. 구현·백테스트·성과 열람은 범위 밖.
> 근거 계약(무완화): `DATA_CONTRACT_4STRATEGY_V1.md`(FROZEN) · 근거 결정: `DECISION_MEMO_AB.md` · 근거 결과: `RESULT_screener_ranking_h4.md`
> 정본 계획: `.omx/plans/one-month-strategy-selection-ai-shadow.md`(이 결정으로 실행순서 현행화).

---

## 0. 한 문장

**이번 달 첫 검증 전략을 "VCP-lite 일봉 스윙"에서 "오버나잇 없는 당일청산 데이트레이딩(Liquid-universe Opening Range Breakout, ORB)"으로 교정한다. 이는 데이터 기간·비용·OOS 경계·품질 기준을 바꾸지 않는 우선순위 교정이며, 전략 성과를 열람하기 전에 내린다.**

---

## 1. 왜 지금, 무엇을 바꾸나 (성과 열람 전 교정)

- `DATA_CONTRACT_4STRATEGY_V1.md` §1 은 "이번 달 연구 실행을 **VCP-lite 일봉 스윙 1개**에 우선 집중"으로 적었다. 이 문장은 **전략 우선순위**이지 데이터 계약(기간·세션·adjusted·OOS·품질)이 아니다.
- 사용자 목표가 **오버나잇 없는 데이트레이딩**으로 확정됐다. 스윙은 정의상 며칠~몇 주 보유(오버나잇 포함)라 이 목표와 어긋난다. 따라서 **첫 검증 대상을 당일청산 intraday 전략(ORB)으로 교정**한다.
- **중요 — 이 교정은 GO 를 쉽게 만드는 변경이 아니다.** 어떤 성과·수익률·forward return·blind OOS 도 열람하지 않은 상태에서, 오직 "무엇을 먼저 검증할지"만 바꾼다. 데이터 계약의 불변식(§6)·기간·비용·OOS 경계·품질 기준은 **그대로 유지**한다(§4).

### 무엇이 바뀌고, 무엇이 안 바뀌나

| 항목 | 이전(v1 원문) | 이번 교정 | 완화 여부 |
|---|---|---|---|
| 이번 달 첫 검증 전략 | VCP-lite 일봉 스윙 | **Liquid-universe ORB(당일청산)** | — (우선순위) |
| 데이터 유니버스 | P0-US30 30종목 | 동일 | 무변경 |
| 1m 기간 요건 | ≥3.0년, 세션 PRE/REG/POST | 동일 | **무완화** |
| 1d 기간 요건 | ≥5년(≥10년 확보) | 동일 | 무변경 |
| blind OOS(1m) | 2026-01-21 ~ 2026-07-21 | 동일 | **무완화** |
| 비용 민감도 | 0.14 / 0.34 / 0.68 %p | 동일 | **무완화** |
| 품질 게이트 | 3.0년 미달 = INCONCLUSIVE_DATA | 동일 | **무완화** |
| VCP-lite 스윙 | 이번 달 우선 | **후속 연구 후보로 보류(폐기 아님)** | — |

---

## 2. 전략 우선순위 (초안)

### 2.1 후보 1 — Liquid-universe Opening Range Breakout (ORB) ★이번 달 첫 검증

당일청산형 데이트레이딩. 방향성 **초안**(정식 임계·예외·tie-break 은 사전등록에서 확정):

- **Opening range**: **15분(09:30–09:44 ET)을 primary 후보**로 한다. **5분(09:30–09:34 ET)은 sensitivity 후보로만** 남긴다.
- **진입**: opening range 상단/하단 돌파. **신호봉 체결 금지 — 다음 거래 가능 봉 가격**(계약/plan §3 공통 체결).
- **청산**: **모든 포지션 당일 청산(EOD flat)**. 오버나잇 금지.
- **재진입·물타기·scale-in**: **같은 종목 당일 재진입·물타기·scale-in 기본 금지**.
- **유동성 조건**: gap/RVOL 유동성 필터는 사전등록에서 수치로 고정(P0-US30 고유동성 표본 범위).

> 이 §2.1 은 **방향 초안**이다. ORB 신호 구현·백테스트·성과 계산은 이 PR 및 이 문서의 범위 밖이며, 사전등록(prereg) 이후에만 수행한다.

### 2.2 후보 2 — Gap + Relative Volume Continuation

ORB 다음 후보. premarket/정규장 첫 유효 gap + RVOL 지속. catalyst 데이터가 없으면 `price-volume continuation`으로만 명명. 당일청산.

### 2.3 후보 3 — VWAP/ATR Extreme Mean Reversion

세 번째 후보. **churn·거래비용 위험**(회전↑ → 비용 드래그↑)이 커서 우선순위 후순위. VWAP 이탈을 ATR/실현변동성으로 정규화, 강추세일 차단, 당일청산.

### 2.4 보류 — VCP/Cup-style Swing Breakout (VCP-lite)

**폐기하지 않는다.** 오버나잇을 포함하는 시간축이라 이번 달 당일청산 목표와 어긋나므로 **후속 연구 후보로 보류**한다. 일봉 ≥10년 데이터는 이미 확보(#70)돼 있어 후속 착수 비용은 낮다.

---

## 3. H4 NULL 의 범위 (오독 방지)

`RESULT_screener_ranking_h4.md` §4 를 그대로 인용한다:

> 현재 **고정 유니버스·롱온리·기술점수·intraday 구조**와, 검증한 **30m~5일 지평**의 진입/선별/국면 정의에는 사용 가능한 엣지가 없다.

- **H4 NULL 은 "기존 기술점수 기반 intraday 틀"의 NULL 이다.** 스크리너의 candidate_score 랭킹 선별력이 미입증이라는 뜻이다.
- **H4 NULL 은 "모든 데이트레이딩 전략"의 NULL 이 아니다.** ORB/Gap/VWAP-MR 는 candidate_score 랭킹이 아니라 **가격구조(opening range 돌파·gap 지속·VWAP 이탈)** 기반의 **별개 가설**이며, H4 가 검증한 대상이 아니다.
- 따라서 ORB 를 첫 검증 대상으로 두는 것은 H4 NULL 과 모순되지 않는다. 이는 `DECISION_MEMO_AB.md` §5 의 "경로 A(현 기술점수 intraday 틀) 종료" 위에서, **새 가설(가격구조 기반 당일 데이트레이딩)**을 검증하는 것이다.

> 경로 분류 주의: `DECISION_MEMO_AB.md` 는 B-1 을 "시간축 전환(스윙)"으로 적었다. 본 결정의 ORB 는 시간축을 늘리는 스윙이 아니라 **회전을 유지하되 재료를 가격구조로 바꾸는** 접근이다. 이는 경로 B 의 "방식 전환"에 가깝고, 스윙(B-1)과는 별개다. 스윙(B-1)은 §2.4 로 보류된다.

---

## 4. 변경 감사 (계약 규율)

- **변경 시점**: ORB/VCP/Gap/VWAP **어떤 전략의 성과·수익률·forward return·blind OOS 도 열람하기 전**이다.
- **완화하지 않은 것**: 데이터 기간(1m ≥3.0년·1d ≥5년), 비용(0.14/0.34/0.68%p), OOS 경계(1m 2026-01-21~07-21·1d 최근 12개월), 품질 기준(3.0년 미달 INCONCLUSIVE_DATA). 전부 `DATA_CONTRACT_4STRATEGY_V1.md` 원문 그대로.
- **성격**: GO 를 쉽게 만드는 변경이 아니라, **사용자 목표(당일청산 데이트레이딩)에 맞춘 전략 우선순위 교정**이다.
- **원문 보존**: `DATA_CONTRACT_4STRATEGY_V1.md` v1 원문은 **조용히 다시 쓰지 않는다.** 우선순위 교정 사실은 그 문서 말미에 **append-only AMENDMENT**(A1)로만 부기하고, v1 본문은 Git 이력으로 보존한다.
- **정본 계획 현행화**: `.omx/plans/one-month-strategy-selection-ai-shadow.md` 에 revision history 를 추가하고 실행 순서를 intraday 우선으로 현행화한다(전략 게이트·비용·OOS 는 무변경).

---

## 5. 이 결정이 여는 다음 작업 (참고 — 이 PR 범위 아님)

1. **Phase 1B 데이터 안전장치**(이 PR): 세션 필터·as_of·immutable raw·거래소 캘린더·coverage manifest·blind OOS 보호·1m→5m/15m 파생. 실제 bulk 미실행.
2. (후속) P0-US30 3년 1분봉 bulk 수집 — Phase 1B 선행 해소 후.
3. (후속) ORB 사전등록(primary 15m / sensitivity 5m, 임계·예외·tie-break·비용·합격기준) — blind OOS 개봉 전.
4. (후속) walk-forward → blind OOS 단일 개봉 → SELECT_ONE / EXTEND_PAPER / NO_SELECTION.

> 이 문서는 **우선순위**만 고정한다. 성과가 좋을 것이라는 주장이 아니며, ORB 가 합격 게이트를 통과하지 못하면 `NO_SELECTION` 이 정상 결과다.
