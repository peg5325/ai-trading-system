# 1개월 전략 선정·AI Shadow 계획

> **추적 정본(Git).** 이 파일이 1개월 계획의 정본이다. `.omx/plans/one-month-strategy-selection-ai-shadow.md`
> 는 로컬 실행용 mirror(gitignore, 저장소 미포함)로만 취급하며, 개정은 이 파일의 Revision History 에
> 남긴다. — 이슈 #75 / PR #77 독립 리뷰(GPT) 차단 2 반영: 추적 문서들이 저장소에 존재하지 않는
> `.omx` 경로를 정본으로 참조하던 문제를 교정(새 clone 에서도 계획 복원 가능).

기간: 2026-07-21 ~ 2026-08-21  
목표: 검증된 전략 1개를 선택하거나 `NO_SELECTION`을 선언한다. 데이터 결함 때문에 선택 가능성을 판단할 수 없고 원천 복구를 중단한 경우에는 `INCONCLUSIVE_DATA`를 별도 종료 상태로 기록한다. 선택된 전략이 있을 때만 1~2주 Paper baseline과 AI shadow를 수집한다.
범위 밖: 실계좌 주문 활성화, AI의 직접 주문·청산·사이징, 결과를 본 뒤 전략 규칙 변경.

## 개정 이력 (Revision History)

| 개정 | 일자 | 이슈 | 내용 |
|---|---|---|---|
| r1 | 2026-07-21 | #66 | 최초 정본. H4 종결 → 데이터 Phase 1 → 4전략 토너먼트 → Paper/AI shadow. |
| r2 | 2026-07-23 | #75 | **실행 우선순위를 intraday(당일청산 ORB) 우선으로 현행화.** 첫 검증 전략을 VCP-lite 일봉 스윙 → **Liquid-universe ORB(당일청산)** 로 교정(사용자 목표=오버나잇 없는 데이트레이딩). VCP-lite 는 후속 보류(폐기 아님). **전략 합격 게이트·비용·OOS 경계·품질 기준은 무변경**(Phase 4 게이트 그대로). 근거: `docs/research/DECISION_DAYTRADING_ORB_PRIORITY.md`, `DATA_CONTRACT_4STRATEGY_V1.md` AMENDMENT A1. 성과 열람 전 교정. |
| r2.1 | 2026-07-23 | #75 | 계획 정본을 이 Git 추적 파일로 이관(내용 변경 없음, `.omx` 는 로컬 mirror). |
| r3 | 2026-07-30 | #98 | ORB development gate 실패(`RESULT_P0_US30_ORB_DEVELOPMENT.md`)에 따라 **ORB 규칙 수정·blind OOS 개봉 없이 종료**하고, 사전에 정한 두 번째 당일청산 후보 **Gap + RVOL Continuation**의 독립 사전등록으로 이동한다. 데이터 identity·비용·개발/OOS 게이트는 무완화. 근거: `DECISION_DAYTRADING_GAP_RVOL_NEXT.md`. |
| r4 | 2026-08-10 | #109 | Gap+RVOL D9 결손의 Toss-only 복구가 불가능하고 사용자가 외부 공급자를 사용하지 않기로 결정했다. verdict를 `INCONCLUSIVE_DATA`로 유지하고 blind OOS·후보 3을 열지 않은 채 이번 데이터 원천 기반 순차 연구를 종결한다. 이는 `NO_SELECTION` 재분류나 품질 기준 완화가 아니다. 근거: `DECISION_P0_US30_GAP_RVOL_NO_EXTERNAL_SOURCE.{md,json}`. |

> **r3 실행 순서(현행·이전 규칙 대체)**: ORB는 development-only `INCONCLUSIVE_DATA`로 중단됐고 blind OOS는 열지 않았다. 다음은 **Gap + RVOL Continuation 사전등록 → 별도 구현·독립 리뷰 → development-only 판정**이다. 이 전략도 D1~D9 development gate를 모두 통과한 경우에만 #78 unlock을 거쳐 blind OOS를 한 번 연다. D1~D8 경제·통계 gate 실패면 `NO_SELECTION`으로 규칙을 고치지 않고 세 번째 후보 VWAP/ATR Mean Reversion으로 이동할 수 있다. D9 데이터·운영 결함이면 `INCONCLUSIVE_DATA`로 중단하고 결함 교정·독립 재리뷰 전에는 OOS를 열거나 다음 후보로 이동하지 않는다. 스윙(1d·VCP-lite)은 후속 보류 상태를 유지한다. **r3는 “4개 전략 모두를 먼저 사전등록한 뒤 어떤 blind OOS도 연다”는 r1의 일괄 토너먼트 순서를 대체한다. 현행 규칙은 “각 후보의 blind OOS 전에 해당 후보 사전등록·구현·독립 리뷰를 완료”하는 순차 방식이며, 아직 열지 않을 후속 후보를 미리 사전등록하지 않는다.** 공통 데이터 identity·비용(0.14/0.34/0.68%p)·OOS 경계·품질·선택 게이트는 바뀌지 않는다.

> **r4 종료 결정(현행)**: Gap+RVOL D9는 결함 감사·Toss 재조회·dependency probe 후에도 `INCONCLUSIVE_DATA`다. 사용자가 외부 공급자를 사용하지 않기로 결정했으므로 원천 복구를 중단한다. D9 기준은 완화하지 않고, 결과를 `NO_SELECTION`으로 바꾸지 않으며, blind OOS와 #78 unlock을 열지 않는다. 동일 결손 데이터에 의존하는 후보 3 VWAP/ATR도 시작하지 않는다. 이번 계획의 종료 상태는 `INCONCLUSIVE_DATA`이며, 새 데이터 원천 또는 새 완전 스냅샷을 승인한 별도 계획 없이는 재개하지 않는다.

## 1. 요구사항 요약

- H4를 사전등록 v1.4(FINAL) 그대로 단일 패스로 실행하고 경로 A 결론을 고정한다.
- 공통 데이터·비용·체결·리스크 계약 아래 네 전략을 비교한다.
  1. 유동성 종목 Opening Range Breakout
  2. Gap + Relative Volume Continuation
  3. VWAP/ATR Extreme Mean Reversion
  4. VCP/Cup-style Swing Breakout
- 24~30개 공통 유니버스는 구현·공통 비교용으로 사용한다.
- 횡단면 선별이 핵심인 ORB/Gap/VCP의 공식 일반화 평가는 가능하면 point-in-time 100~300개 이상으로 확장한다.
- 최신 블라인드 OOS를 보기 전에 전략 정의, 비용, 통계, 합격 기준을 사전등록한다.
- AI는 선택된 전략의 Paper 주문에 영향을 주지 않는 shadow로만 연결한다.
- 한 달 종료 시 결과는 `SELECT_ONE`, `EXTEND_PAPER`, `NO_SELECTION`, 또는 데이터 원천 한계로 판단 불가능한 `INCONCLUSIVE_DATA` 중 하나여야 한다.

## 2. 현재 근거와 제약

- 지표 엔진은 EMA/RSI/VWAP/Bollinger/StochRSI를 계산한다: `src/engines/indicator_engine.py:16-24,97-135`.
- 현재 스크리너는 trend/momentum/volume/VWAP/volatility 가중합이며 LONG_SETUP만 생성한다: `src/universe/screener.py:28-30,108-126,190-202`.
- 역사 DB 품질 도구는 종목별 기간·세션 커버리지·분봉 결측·gap을 집계한다: `src/history/quality.py:35-114`.
- 공식 평가 경로는 chronological/embargo/LOSO를 허용하고 random split을 금지한다: `src/history/evaluation.py:19-20,61-120`.
- 현재 BacktestEngine은 단일 종목 실행 중심이고 walk-forward는 TODO다: `src/backtest/engine.py:239-252`.
- 체결 모델은 고정 slippage/fee 전량 체결 모델이다: `src/backtest/execution.py:20-36`.
- 성과 엔진은 return/win rate/PF/expectancy/MDD/Sharpe를 제공한다: `src/portfolio/performance.py:21-74`.
- AI 진입 필터 인터페이스는 이미 있으나 현재 운영 config는 FakeAI다: `src/ai/engine.py:50-75`, `config/runtime_us.yaml:24-26`.
- AI는 주문·청산 권한이 없고 RiskManager가 최종 통제한다: `src/ai/README.md:34-46`, `src/risk/risk_manager.py:1-7,82-95`.
- 실주문 TossBrokerProvider는 skeleton이다: `src/order/toss_broker.py:16-27`.
- 운영 correctness gate: GitHub issue #44(EOD 종목별 봉 skew), #64(promotion cohort config_hash 혼합).

## 3. 공통 연구 계약

### 데이터 계층

공통 30개 엔지니어링 유니버스(최종 확정은 사전등록에서 수행):

- 시장: SPY, QQQ, IWM, DIA
- 섹터: XLK, XLF, XLE, XLV, XLI, XLY, XLU, SMH
- 대형 성장/반도체: AAPL, MSFT, NVDA, AMD, AVGO, AMZN, META, GOOGL, TSLA, MU, NFLX, CRM, PLTR, VST
- 레버리지/인버스 자연실험: TQQQ, SQQQ, SOXL, SOXS

필수 기간:

- Intraday: 최소 3년 1분봉 또는 원천 1분봉에서 생성한 5분봉.
- Swing: 최소 5년 수정 일봉.
- Blind OOS: intraday 최신 6개월, swing 최신 12개월.
- 프리마켓을 사용하는 전략은 premarket OHLCV 커버리지가 없으면 공식 판정에서 제외한다.

필수 데이터 검증:

- UTC/거래소 시간대, DST, 세션 라벨.
- split/reverse split/dividend 등 corporate action과 수정주가 정책.
- 중복·역전 시각·OHLC 불변식·음수 거래량·결측봉.
- 상장폐지/신규상장과 survivorship-bias 범위.
- 데이터 공급자별 symbol mapping과 source provenance.
- 데이터가 부족하면 기간이나 합격 기준을 완화하지 않고 해당 전략을 `INCONCLUSIVE_DATA`로 판정한다.

### 공통 체결·리스크

- 신호봉 체결 금지; 다음 거래 가능 봉 가격 사용.
- 동일한 1R 위험 예산과 포트폴리오 노출 상한 적용.
- 물타기 금지, 전략별 scale-in은 기본 OFF.
- Intraday는 기본 EOD flat; swing만 사전등록된 overnight 허용.
- 비용 민감도: 왕복 0.14%p / 공식 0.34%p / stress 0.68%p.
- 현재 RiskConfig의 최대 MDD 10%, 동시 포지션 2, 포트폴리오 실질노출 50%를 상한으로 유지: `src/risk/risk_manager.py:58-70`.
- 레버리지/인버스 결과는 1x와 분리 보고하고 독립 종목 증거로 중복 계산하지 않는다.

### 공통 평가 지표

- OOS net expectancy per trade 및 per 1R.
- profit factor, win rate, average win/loss, payoff ratio.
- daily Sharpe/Sortino, Calmar, MDD, recovery duration.
- turnover, exposure time, cost ratio, 거래 수.
- 월별/분기별 수익 일관성.
- 종목·날짜·섹터·시장 국면별 기여도와 편중.
- 최악 일, 최대 연속 손실, tail loss.
- 인접 파라미터 안정성 및 비용 stress.

## 4. 단계별 일정

## Phase 0 — H4 종결과 증거 고정 (Day 1~2)

작업:

1. PR #65의 v1.4(FINAL), docs-only diff, H4 미실행 상태, review 코멘트를 재검증한다.
2. PR #65를 merge하여 사전등록과 경로 A 근거를 main에 영구 보존한다.
3. 별도 issue/branch/worktree에서 H4 분석 구현 및 테스트를 작성한다.
4. `PREREG_screener_ranking_h4.md` 규칙을 한 번만 실행한다.
5. 결과를 `docs/research/RESULT_screener_ranking_h4.md`에 저장하고 별도 PR로 리뷰한다.
6. GO면 기존 랭킹 신호를 후보군에 포함하고, NULL이면 경로 A를 공식 종료한다.

중단 조건:

- prereg와 구현 매핑이 불완전하면 결과를 열지 않는다.
- 입력 데이터 결함이 발견되면 분석을 무효화하고 결함만 수정한 뒤 prereg의 무효화 규칙에 따라 재실행한다.

완료 증거:

- PR #65 merged.
- H4 결과 PR과 독립 리뷰.
- `GO` 또는 `NULL` 단일 판정.

## Phase 1 — 데이터 확보·품질 게이트 (Day 3~7)

작업:

1. `market_history.sqlite`의 현재 symbol/기간/interval/결측을 inventory한다.
2. Toss API의 3년 intraday 깊이, 수정주가, 프리마켓 제공 가능성을 probe한다.
3. 부족한 경우 데이터 공급자 후보를 결정하되, 라이선스·point-in-time·corporate action 조건을 문서화한다.
4. 공통 30개를 backfill하고, 가능한 경우 공식 횡단면 풀 100~300개를 별도 manifest로 만든다.
5. `src/history/quality.py`를 확장해 일별 세션 완전성, corporate-action discontinuity, stale bar, provider provenance를 검증한다.
6. 원천 1분봉은 read-only로 보존하고 5분봉·일봉은 결정론적으로 재생성한다.
7. 데이터 snapshot ID/hash를 고정한다.

합격 기준:

- 공통 30개 각 전략 필요 기간의 95% 이상 세션 커버리지.
- blind OOS 구간은 주요 결측 0 또는 사전 정의된 제외일 목록으로 설명 가능.
- 동일 입력에서 집계봉 byte-identical.
- corporate action 전후 비정상 수익률이 수정 정책으로 설명됨.

중단 조건:

- Day 7까지 충분한 point-in-time 데이터가 없으면 진짜 Stocks-in-Play/Cameron/Sykes형 주장은 제외한다.
- 30개 고정 유니버스 결과는 “현재 유동성 종목 표본” 범위 밖으로 일반화하지 않는다.

## Phase 2 — 공통 피처와 백테스트 기반 확장 (Day 5~10, Phase 1과 일부 병행)

구현 피처:

- ATR14 및 normalized ATR.
- 전일 종가 대비 gap %.
- 5/15분 opening range high/low/width.
- 시각대별 relative volume(RVOL); 미래 거래량 사용 금지.
- rolling 20/50/150/200일 이동평균과 slope.
- rolling high/low, breakout distance.
- SPY/QQQ/SMH 대비 상대강도.
- intraday VWAP distance를 ATR/실현변동성으로 정규화.
- premarket return/volume은 데이터 커버리지가 확인된 경우에만.

백테스트 확장:

- 공통 `StrategySignal` 계약: symbol, timestamp, direction, entry, invalidation, holding_policy, reason.
- 다종목 event-time 포트폴리오 시뮬레이션.
- 동일 시각 후보의 결정론적 랭킹과 자본 경쟁.
- long과 inverse-ETF 방향 지원; 개별주 naked short는 이번 달 범위 밖.
- 거래일 단위 chronological walk-forward + embargo.
- 비용 3수준 동시 실행과 결과 manifest.

예상 수정 영역:

- `src/engines/indicators.py`, `src/engines/indicator_engine.py`.
- 신규 `src/strategies/`.
- `src/backtest/models.py`, `src/backtest/engine.py`, `src/backtest/execution.py`.
- `src/history/evaluation.py`, `src/portfolio/performance.py`.

테스트:

- 각 지표의 hand-calculated fixture.
- 입력 순서 불변, 중복봉 불변, warm-up 경계.
- look-ahead 음성 테스트.
- next-bar fill, gap-through-stop, 동시 후보 자본 경쟁.
- Paper/PnL 성과 산식 parity.

## Phase 3 — 네 전략 사전등록·구현 (Day 8~14)

전략 1: Liquid-universe ORB

- 사전 정의한 gap/RVOL 유동성 조건.
- 5분 또는 15분 opening range 중 primary 하나 고정.
- range 돌파, next-bar 진입, stop/target/EOD 규칙 고정.

전략 2: Gap + RVOL Continuation

- premarket 또는 정규장 첫 유효 gap 정의.
- catalyst 데이터가 없으면 `price-volume continuation`으로만 명명.
- 초기 눌림·재돌파 조건과 최대 진입 시각 고정.

전략 3: VWAP/ATR Mean Reversion

- VWAP 이탈을 ATR 또는 realized vol로 정규화.
- 강한 추세일 차단 조건은 QQQ/SPY의 완료봉만 사용.
- VWAP 회귀 target, hard stop, time stop, EOD 규칙 고정.

전략 4: VCP/Cup-style Swing Breakout

- Stage-2 trend template의 기계적 정의.
- contraction 횟수·폭 감소·거래량 감소·pivot 정의.
- next-day 또는 next-bar breakout fill, ATR stop, 최대 보유일 고정.
- 펀더멘털이 없으면 “price-volume VCP-lite”로 명명하고 Minervini 완전 재현을 주장하지 않는다.

사전등록 공통:

- 각 전략 primary spec 1개, sensitivity spec 최대 1개.
- 모든 threshold, 예외, tie-break, missing-data 처리.
- IS에서 볼 수 있는 지표와 blind OOS 접근 금지.
- 다중 비교 보정과 최종 승자 선택식.
- 합격 실패 시 규칙 수정 없이 `NO_SELECTION`.
- **r3 순차 실행에서는 각 후보의 blind OOS 전까지 그 후보의 사전등록만 필수다.**
  후속 후보는 선행 후보의 판정 후 별도 preregistration으로 잠그며, 미개봉 후보의 규칙을 결과 전에
  불필요하게 확정하지 않는다.

## Phase 4 — Walk-forward·Blind OOS 전략 토너먼트 (Day 15~18)

실행 순서:

1. 개발 구간에서 walk-forward를 실행한다.
2. 파라미터 이웃과 비용 민감도를 확인한다.
3. 전략별 구현/통계 독립 리뷰를 통과한다.
4. 마지막으로 blind OOS를 한 번 연다.
5. 공통 leaderboard와 전략별 상세 리포트를 만든다.

최소 선택 게이트:

- 공식 비용 0.34%p 후 blind OOS net expectancy > 0.
- OOS profit factor >= 1.20.
- Intraday: OOS 거래일 >= 60, 거래 >= 100, 최근 6개월 중 >= 4개월 양수.
- Swing: OOS 기간 >= 12개월, 거래 >= 60, 4분기 중 >= 3분기 양수.
- OOS MDD 절대값 <= 10%.
- stress 비용 0.68%p에서 누적 손익 >= 0.
- 단일 종목이 총 net profit의 25% 초과 기여 금지.
- 단일 거래일이 총 net profit의 20% 초과 기여 금지.
- 인접 파라미터에서 부호가 급반전하는 parameter cliff 금지.
- 데이터 누수·survivorship·corporate action 중대 결함 0.

선택 규칙:

- 통과 전략이 1개면 그 전략 선택.
- 여러 개면 OOS expectancy가 아니라 `robustness score`(비용 stress, MDD, 월별 안정성, 편중, 표본 수) 우선.
- 통과 전략이 없으면 `NO_SELECTION`; 상대적으로 덜 나쁜 전략을 선택하지 않는다.

## Phase 5 — 선택 전략 동결·운영 게이트 수정 (Day 19~20)

작업:

1. 선택 전략의 코드·config·prompt·data snapshot·report hash를 release candidate로 고정한다.
2. issue #44를 수정하고 다종목 EOD terminal-flat 통합 테스트를 통과시킨다.
3. issue #64를 수정하여 promotion cell에 config_hash/data snapshot/strategy version을 포함한다.
4. Paper 전용 fresh namespace와 launch preflight 문서를 생성한다.
5. 실제 주문 provider는 계속 비활성화한다.

완료 기준:

- 전체 pytest green.
- EOD 후 open_positions == 0.
- 다른 config_hash cohort 혼합 시 promotion report fail-closed.
- fresh namespace, producer commit, config hash, data snapshot 기록.

## Phase 6 — 1~2주 Paper Baseline + AI Shadow (Day 21~30)

실제 Paper 경로:

- 선택 전략 규칙만 주문에 영향.
- AI provider의 응답은 주문에 영향 0.
- 동일한 고정 사이징·손절·익절·EOD 정책.

AI shadow 입력:

- 전략 후보와 신호 이유.
- 지표 snapshot과 시장/섹터 context.
- 포트폴리오 노출·손실 상태.
- 확보 가능할 경우 point-in-time 뉴스/실적/event context.
- 뉴스가 없으면 AI 성과는 “정형 데이터 재판독”으로 별도 표기하고 비정형 AI 가치로 주장하지 않는다.

AI shadow 출력:

- `WOULD_APPROVE`, `WOULD_REJECT`, `WAIT`.
- confidence, catalyst classification, conflict flags, reason codes.
- 주문/수량/stop/target 필드는 schema에서 금지.

비교 지표:

- AI가 거부했을 거래의 실제 net outcome(avoided loss).
- AI가 거부해 놓쳤을 실제 이익(missed gain).
- approve/reject 집단의 expectancy 차이.
- 모델·prompt 반복 일관성.
- timeout/schema/rate-limit 비율.
- latency와 stale-context 비율.

한 달 내 AI 판정 한계:

- 8~10 Paper 세션은 AI 승격에 충분한 확증이 아니라 feasibility evidence다.
- AI가 유리해 보여도 바로 매매 권한을 주지 않는다.
- 다음 1~2주 추가 shadow/A-B를 거친 뒤에만 “규칙 후보 veto” 권한을 검토한다.

## 5. 한 달 종료 산출물

필수 문서:

- H4 최종 판정과 경로 A 종료/유지 결정.
- 데이터 manifest·quality report·known limitations.
- 실행한 후보별 preregistration과 후속 후보의 순차 진행/보류 기록.
- 전략별 walk-forward 및 blind OOS 결과.
- 공통 leaderboard와 선택 결정 메모.
- Paper session manifest와 비용 보고.
- AI shadow feasibility 보고.
- 실계좌 전환 readiness checklist.

최종 상태:

- `SELECT_ONE`: 전략이 모든 OOS 게이트 통과, Paper/AI shadow 계속 진행.
- `EXTEND_PAPER`: 전략은 통과했지만 Paper 세션 수나 운영 안정성 부족.
- `NO_SELECTION`: 어떤 전략도 통과하지 못함; 실거래·AI 영향 활성화 금지.
- `INCONCLUSIVE_DATA`: 데이터 결함 때문에 전략 선택 여부를 판단할 수 없고 승인된 복구 경로가 없음; 실거래·Paper 승격·AI 영향·후속 후보 활성화 금지.

## 6. 실계좌 연결 조건(다음 단계)

이 한 달 계획은 실계좌 주문 활성화를 목표로 하지 않는다. 다음 조건 이후 별도 단계로 수행한다.

- 선택 전략의 최소 10~20 FULL Paper 세션.
- 비용 후 Paper expectancy > 0과 운영 오류 0.
- #44/#64 및 reconciliation 배선 완료.
- TossBroker 주문/부분체결/거부/idempotency/retry 구현과 소액 sandbox 또는 승인된 최소 주문 검증.
- kill switch, broker-state reconciliation, orphan order, restart recovery 테스트.
- 최초 실거래는 AI OFF, 최소 금액, 단일 종목, 일일 손실 한도 강화.

## 7. 주요 리스크와 완화

- 데이터 공급자 깊이 부족: 기준 완화 금지; 전략을 INCONCLUSIVE 처리.
- 4전략 × 파라미터 과최적화: primary 1개 + sensitivity 1개, blind OOS 단일 공개.
- 서로 다른 보유기간의 불공정 비교: 1R·일별 포트폴리오 수익·MDD 기준으로 비교.
- 동적 종목 전략을 고정 30개로 과대 일반화: 엔지니어링/공식 유니버스 결과를 분리.
- AI가 기존 전략 성과를 오염: 첫 1~2주는 shadow-only, 주문 영향 0.
- 일정 압박으로 실패 전략 강제 선택: `NO_SELECTION`을 정상 성공 상태로 취급.

## 8. 검증 체크리스트

- [ ] PR #65 v1.4(FINAL) merge 및 H4 단일 패스 완료.
- [ ] 데이터 snapshot/hash와 품질 리포트 존재.
- [ ] 각 후보 blind OOS 이전 해당 후보 prereg·구현·독립 리뷰 커밋.
- [ ] look-ahead/순서불변/체결/비용/다종목 자본경쟁 테스트 통과.
- [ ] 전체 pytest green.
- [ ] 전략별 0.14/0.34/0.68 비용 결과 존재.
- [ ] 승자, NO_SELECTION 또는 INCONCLUSIVE_DATA 결정이 기계적 gate와 일치.
- [ ] #44/#64가 Paper 사용 전에 해결됨.
- [ ] Paper는 fresh namespace와 FULL manifest 사용.
- [ ] AI shadow 결과가 주문에 영향을 주지 않았음을 parity 테스트로 증명.
- [ ] 실주문 provider 비활성 유지.
