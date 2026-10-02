# src/freeze_paper — FREEZE Paper 계약 엔진 (이슈 #165 PR 1)

## 1. 목적

`docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json`(v1.3)을 기존 부품
(RiskManager → OrderManager → PaperBrokerProvider → PortfolioAccount/FIFO, ExitSignalGenerator)으로
**그대로 강제하는** 오프라인 결정론 엔진. 두 번째 PnL 엔진이나 별도 청산 로직을 만들지 않는다.

## 2. 파일

| 파일 | 역할 |
|---|---|
| `binding.py` | 계약 JSON 로드, 파일 SHA-256, `RiskConfig`/`ExitConfig`와 수치 전부를 계약에서 파생 |
| `gates.py` | `evaluate_gates(trading_day, reports_root, contract) -> GateResult`: FREEZE 파일 해시↔COMPLETE 영수증, `artifact_sha256` 재계산, JSON 경로 게이트, 방향별 다리(LONG / INVERSE 매핑), 최대 3개 |
| `sizing.py` | `portfolio.sizing_procedure` 문자 그대로의 포팅(v1.3 소수점: Decimal 내림 6자리, USD 1 미만 `BELOW_MIN_ORDER_NOTIONAL`), `EXPOSURE_ROOM` |
| `costs.py` | 수수료 10bp/편도, SEC fee(Decimal 센트 올림, 최소 0.01), 공식·stress 파생 비용(읽기 전용) |
| `engine.py` | `start_session` / `step_bar` / `finish_session` / `run_session`. 상태는 JSON 직렬화 dict |
| `receipt.py` | 첫 세션 영수증 스키마·검증기(활성화의 유일한 스위치). 영수증을 만들지 않는다 |
| `runner.py` | 장마감 후 세션 실행기: 활성화 → 시간 가드 → 엄격한 세션 순서 → 게이트 → backfill(subprocess) → OOS 가드 로더 → 엔진 → immutable artifact·장부 |
| `evaluator.py` | 10세션 평가(계약 `evaluation`): 검증된 세션 artifact만 사용, 계좌·공식/stress/운영비 후 수익률, 장중 최대 낙폭, LONG/INVERSE 분리, SPY 총수익 벤치마크, HALT → EXTEND → NO_PROMOTION → ELIGIBLE 우선순위 |
| `reconcile.py` | 일일 대조: 주문 저장소·계좌·체결 재생 포지션, 주문↔체결 1:1, 현금 항등식, 세션 종료 무포지션, id 중복, 재시작 재구성 |

## 3. 봉 처리 순서 (`exits.priority_within_bar`)

1. 시가: 위험 중단/이월 청산(`EXIT`) → `TIME_EXIT`·`EOD_FLATTEN` → 09:35 ET 봉에서만 신규 진입
2. 봉 안: `STOP_LOSS`/`TAKE_PROFIT`(손절 우선, 갭 규칙, 진입 봉 포함)
3. 종가: 기록 장부로 평가, 일일 손실(직전 마감 자산 앵커)·고점 대비 낙폭(복원된 고점) 검사 → 다음 봉 시가 청산

봉은 `snapshot.payload.schedule`의 `[market_open_utc, market_close_utc)`만 쓴다(`Candle.session` 미신뢰).
EOD는 마감 10분 전. 마감까지 청산 못 하면 `UNRESOLVED_EXIT`, 다음 세션 첫 봉에서 `EXIT` 후 이후 세션 `HALTED`.

### 호출자 계약

- `start_session` 에는 **NYSE 거래일만** 넘긴다. brief 가 없을 때 쓰는 캘린더 fallback
  (`build_premarket_schedule`)은 휴장일이면 예외를 던진다. 같은 날 또는 이전 날의 세션은 거부되며
  재시작은 체크포인트 상태를 이어 `step_bar` 하는 방식만 허용한다. 계약 해시가 다른 gate/state 도 거부한다.
- INVERSE 다리 승인은 정규화·planner 가 `approved_short_strategy_templates` 로부터 만든
  `strategy_template_approved` 플래그를 신뢰한다. 이 패키지는 config 를 다시 읽지 않는다.
- 비정상 봉(NaN·0 이하·고저 역전)은 `INVALID_BAR` 이벤트로 남기고 결측 봉과 똑같이 처리한다.
- `Candle.ts_utc`는 봉 **시작** 시각이다. 실행기는 `[market_open_utc, market_close_utc)` 봉만 넘기므로
  09:35 ET 진입 봉은 EDT에서 `13:35Z`이고, 마감 시각에 시작하는 봉은 쓰지 않는다.
- 청산 주문 ID는 `pfl1-{진입일}-{종목}-SELL-{n}`: 거절·부분 체결된 청산(손절·익절 포함)은 사유와 함께
  대기 청산으로 남아 다음 유효 봉 시가에서 가격 재확인 없이 n+1로 재시도한다. 봉당 종목별 청산 시도는 1회다.
  이미 FILLED인 ID인데 포지션이 남아 있으면 `LEDGER_INCONSISTENT`로 기록하고 재제출하지 않는다(UNRESOLVED_EXIT로 종료).
- 같은 분에 대문자 변환 후 겹치는 봉 키는 모두 `INVALID_BAR`(`BAR_KEY_COLLISION`)로 버린다.
- 게이트 허용 목록은 FREEZE snapshot의 `policy_config.allowed_symbols`다. 빈 목록이면 `NO_TRADE`다
  (planner는 빈 목록을 무제한으로 보지만 여기서는 의도적으로 fail-closed). 목록이 없거나 형식이 틀리면
  `ARTIFACT_STRUCTURE_INVALID`다. PR 2 활성화 영수증은 운영 config 목록에 `TQQQ`·`SOXL`이 있고 단일 종목
  레버리지 ETF가 없음을 확인해야 한다(2026-09-29 FREEZE snapshot의 `allowed_symbols`에는 TQQQ·SOXL이 있고
  단일 종목 레버리지 ETF는 없었다).

## 4. 절대 하면 안 되는 것

- ❌ **AI를 import하거나 호출하지 않는다.** 진입은 FREEZE 게이트, 청산은 결정론 규칙뿐이다.
- ❌ **실계좌 브로커/Toss 주문 API를 쓰지 않는다.** 체결은 `PaperBrokerProvider`뿐이다.
- ❌ **`src/premarket`가 이 패키지나 `src.order`를 import하게 만들지 않는다.** 의존은 이 패키지 → premarket 한 방향.
- ❌ 현재 시각·난수·네트워크·파일 쓰기를 쓰지 않는다. `gates.py`는 FREEZE 파일을 읽기만 한다.
- ❌ 계약 수치를 코드에 하드코딩하지 않는다. `binding.py`를 거친다.
- ❌ 청산을 막지 않는다. 모든 청산은 `RISK_REDUCING` 종류로 보낸다.
- ❌ 영수증 없이 Daily Brief 승인 목록을 채우지 않는다(활성화 OFF 유지, 테스트가 강제).

### 첫 세션 영수증 전 SPY 기준 종가 확보(runbook)

1. 구간 시작일(첫 세션 직전 NYSE 거래일, `window_start_close_date`) 저녁, 정규장 마감 + 20분 이후 ~
   당일 24:00 ET 이전에 SPY 1분봉 tail repair를 한 번 실행한다.

   ```bash
   .venv/bin/python scripts/history_backfill_official.py --db data/market_history.sqlite \
     --symbols SPY --interval 1m --completed-through YYYY-MM-DD --tail-repair \
     --sessions REGULAR --max-pages 8 --page-size 200 --spacing 0.4 \
     --run-id premarket-freeze-paper-window-start-YYYYMMDD \
     --raw-snapshot-dir data/raw_snapshots/premarket-freeze-paper-window-start-YYYYMMDD --json
   ```

2. 저장된 증거를 읽어 영수증 `window_start_spy_close_evidence`에 그대로 적는다.

   ```bash
   .venv/bin/python -c "from datetime import date; from pathlib import Path; \
   from src.freeze_paper.spy import spy_close_evidence; \
   print(spy_close_evidence(Path('data/market_history.sqlite'), date.fromisoformat('YYYY-MM-DD')))"
   ```

3. 영수증을 만든 뒤 `load_receipt`로 검증한다. 검증기는 DB를 읽기 전용으로 열어 기록값과 행을 대조하고
   같은 캡처 규칙을 적용한다(`RECEIPT_INVALID:WINDOW_START_SPY_CLOSE`).

영수증은 매 실행마다 다시 검증된다. 구간 도중 구간 시작일 SPY 행이 더 높은 우선순위 source로 덮어쓰이면
(`updated_at` 변경) 이후 실행은 `NOT_ACTIVATED`가 되고(fail-closed) 검토 전까지 세션 기록이 멈춘다.
DB를 읽을 수 없으면 `RECEIPT_INVALID:WINDOW_START_SPY_CLOSE_UNREADABLE`로 같은 처리다.

운영자 신뢰 가정: 로컬 시계가 정확하다(`created_at`은 로컬 UTC 시각). DB 행을 수동으로 삭제·재삽입하지
않는다(재삽입하면 `created_at`이 새로 기록되어 규칙이 무의미해진다). 세션 기간에는 Paper 실행기가 매 예정
세션마다 SPY tail repair를 별도 단계로 실행하며(엔진 입력·세션 artifact와 무관), 실패해도 세션은
`INCOMPLETE`가 되지 않고 operation receipt에 `spy_step.status=SPY_BACKFILL_FAILED`로 남는다. 그 경우
평가기의 SPY 벤치마크가 `INCONCLUSIVE_BENCHMARK`가 된다.

### 활성화 후 테스트가 바뀌는 방식

`tests/test_freeze_paper_activation.py`는 승인 목록이 비어 있으면 현재 `policy_config` 해시가 고정값과
같은지, 채워져 있으면 추적 영수증이 존재하고 `load_receipt`로 검증되는지 확인한다. 활성화 PR은
config 승인 목록과 영수증을 **함께** 추가한다. 이때 활성화 config의 파일 SHA-256은 영수증
`activation_config_sha256`에 고정되고(검증기가 대조), 실행기는 FREEZE snapshot의 `policy_config`가 그 config와
같은지 다시 확인한다. 따라서 고정 해시 테스트는 OFF 상태에서만 적용되고, 활성화 뒤의 해시 고정은 영수증이 맡는다.
영수증 `code_commit`은 실행 HEAD의 조상이어야 한다(`git merge-base --is-ancestor`).
- ❌ `load_candles`를 직접 쓰지 않는다. 1분봉은 `research_load`로만 읽는다.
- ❌ 세션 artifact를 덮어쓰거나 stale `.partial`을 지우지 않는다(`DATA_DRIFT`/`INCOMPLETE`로 보고).
- ❌ 평가 결과는 실거래 승인이 아니며 자동 승격은 없다. INTERIM 보고서는 ELIGIBLE이 될 수 없다.
- ❌ SPY 총수익을 S&P 500 Total Return으로 표기하지 않는다(공식 S&P 500 TR은 항상 `INCONCLUSIVE_BENCHMARK`).
- ❌ 배당 금액을 추측하지 않는다. `docs/research/SPY_DIVIDENDS.json`에 출처·확인일이 있는 금액만 쓴다.

### 평가기 사용

10번째 예정 세션이 기록된 뒤(또는 중간 점검으로) 수동 실행한다.

```bash
.venv/bin/python scripts/evaluate_freeze_paper_window.py            # 10개 세션이 모두 기록·검증되면 FINAL, 아니면 INTERIM
.venv/bin/python scripts/evaluate_freeze_paper_window.py --through 2026-10-07 --dry-run
```

- 입력: 검증된 첫 세션 영수증, 서명·연결이 검증된 세션 artifact, SPY 종가, 검증된 SPY 배당.
  하나라도 `INVALID`면 지표를 계산하지 않고(`NOT_EVALUABLE`) 누락·`INCOMPLETE` 세션은 0으로 채우지 않는다.
- FINAL은 예정 10개 세션 artifact가 **모두** 존재·검증될 때만 붙는다. 하나라도 없거나 `INCOMPLETE`·
  `INVALID`면 `--through`와 무관하게 INTERIM이며, 이때 계좌와 SPY 비교(초과수익)는 `None`
  (`benchmark_comparison=WINDOW_INCOMPLETE`)이다. 적은 세션의 계좌를 긴 SPY 구간과 비교하지 않는다.
- 세션 artifact 스키마는 `premarket-freeze-paper/session/v2`(엔진 상태 v2: `session.equity_curve`)다.
  v1 artifact는 `SESSION_SCHEMA_MISMATCH`, 곡선이 없으면 `SESSION_EQUITY_CURVE_MISSING`으로 평가 불가다.
- 다리별 실현손익 합계는 계좌 FIFO 실현손익과 1e-9 안에서 같아야 한다. 다르면 `HALT_AND_REVIEW/LEG_PNL_MISMATCH`.
  `ELIGIBLE_FOR_LIVE_REVIEW`인데 LONG 다리 공식 비용 후 손익이 0 이하면 판정은 그대로 두고
  `ELIGIBILITY_DEPENDS_ON_INVERSE_LEG` flag를 붙인다. profit factor는 손실 거래가 없으면 `null`이다.
- 계좌: 기록 장부 TWR `E_end/E_start − 1`(외부 입출금 없음). 공식·stress는 체결 명목금액 × 편도 증분을
  빼고, 운영비 후는 영수증 `operating_costs.total_usd` 전액을 추가로 뺀다(INTERIM도 전액, 보수적).
  세션 수 비례 배분값은 `diagnostics`에만 참고로 둔다.
  최대 낙폭은 매 봉 기록 평가자산(`session.equity_curve`)과 초기 자본을 고점 후보로 계산한다.
- SPY 종가 출처(증거 기반): Toss 캔들 응답에는 수정 여부 필드가 없고 provider는 1d·1m을 모두
  `adjusted=True`로 저장한다. `PROBE_REPORT_TOSS_CAPABILITY_V1` §4는 두 계열 모두 split back-adjusted이고
  배당 조정은 미검증(R1)이라고 기록한다. 따라서 공식 비조정 일봉 종가는 없다. 평가기는 같은 날 post-close
  tail repair가 저장한(store는 first-write-wins) REGULAR 1분봉 중 마감 1분 전에 시작하는 봉(15:59 ET,
  조기폐장 12:59 ET)의 종가를 `research_load`로 읽고, 결과에 `close_source`로 정확히 표기한다(공식 비조정
  종가라고 주장하지 않는다). 종가가 없으면 `INCONCLUSIVE_BENCHMARK`. 배당은 배당락일 종가에 재투자한다.
- 캡처 규칙(검증, 가정하지 않음): 소급 수정은 나중에 적용되므로 그날 저장된 봉은 체결 당시 가격이다.
  `candle_history.created_at`은 최초 insert 때 한 번만 기록되고, 값이 바뀌면 `updated_at`만 갱신된다
  (`src/history/store.py` `_upsert_row`). 평가기는 SPY 종가 봉의 `created_at`이 그날 정규장 마감(조기폐장
  포함) 이후 ~ 다음 날 00:00 America/New_York(DST 반영) 이전이고 `updated_at == created_at`일 때만 쓴다. 아니면
  `SPY_CLOSE_NOT_CAPTURED_SAME_DAY` 또는 `SPY_CLOSE_OVERWRITTEN`으로 `INCONCLUSIVE_BENCHMARK`이며,
  사용한 종가의 캡처 시각은 `close_evidence`에 남는다. 인접한 SPY 종가 비율이 0.75~1.25를 벗어나면
  `SPY_PRICE_DISCONTINUITY:<날짜>`로 `INCONCLUSIVE_BENCHMARK`다(분할 등 불연속 방어).
- 첫 세션 영수증 `spy_price_source`(필수, 비어 있지 않은 문자열)에는 다음 취지를 적는다:
  "Toss REGULAR 1m bar close starting at 15:59 ET (12:59 ET on early closes); provider series is
  split-adjusted and dividend adjustment is unverified; used only when first written to the store on the
  same evening (after the close, before 00:00 America/New_York) and never overwritten; not the official
  closing auction and not an official unadjusted daily close". 검증기는 문구를 해석하지 않고 존재만 강제한다.
- 배당락일: 영수증 `spy_ex_dividend_dates_in_window`는 `spy_ex_dividend_source` 기준의 **전수 목록**이다(빈 목록도
  명시값). 3·6·9·12월 셋째 금요일 ±2 NYSE 거래일 구간이 평가 구간과 겹치는데 목록에 그 구간의 날짜가 없으면
  `INCONCLUSIVE_BENCHMARK/UNLISTED_EXPECTED_EX_DATE`다.
- 출력: FINAL은 `reports/premarket-freeze-paper-evaluation.<first>_<last>.json`(+`.md`) write-once(다르면
  `DATA_DRIFT`), INTERIM은 `reports/premarket-freeze-paper-evaluation.interim.<first>_<through>.json`(+`.md`)으로
  재생성 가능. 벽시계 시각이 없어 같은 입력이면 byte-identical이다.

## 5. 테스트

`tests/test_freeze_paper_{binding,gates,sizing,costs,engine,reconcile}.py` — 합성 봉과 `tmp_path`만 쓰는 오프라인 테스트.
