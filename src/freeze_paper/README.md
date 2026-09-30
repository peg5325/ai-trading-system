# src/freeze_paper — FREEZE Paper 계약 엔진 (이슈 #165 PR 1)

## 1. 목적

`docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json`(v1.2)을 기존 부품
(RiskManager → OrderManager → PaperBrokerProvider → PortfolioAccount/FIFO, ExitSignalGenerator)으로
**그대로 강제하는** 오프라인 결정론 엔진. 두 번째 PnL 엔진이나 별도 청산 로직을 만들지 않는다.

## 2. 파일

| 파일 | 역할 |
|---|---|
| `binding.py` | 계약 JSON 로드, 파일 SHA-256, `RiskConfig`/`ExitConfig`와 수치 전부를 계약에서 파생 |
| `gates.py` | `evaluate_gates(trading_day, reports_root, contract) -> GateResult`: FREEZE 파일 해시↔COMPLETE 영수증, `artifact_sha256` 재계산, JSON 경로 게이트, 방향별 다리(LONG / INVERSE 매핑), 최대 3개 |
| `sizing.py` | `portfolio.sizing_procedure` 문자 그대로의 포팅(`floor(x+1e-9)`), `EXPOSURE_ROOM` |
| `costs.py` | 수수료 10bp/편도, SEC fee(Decimal 센트 올림, 최소 0.01), 공식·stress 파생 비용(읽기 전용) |
| `engine.py` | `start_session` / `step_bar` / `finish_session` / `run_session`. 상태는 JSON 직렬화 dict |
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
- ❌ runner, launchd, 장부 파일, 활성화, 평가기는 이 PR 범위가 아니다(PR 2/3).

## 5. 테스트

`tests/test_freeze_paper_{binding,gates,sizing,costs,engine,reconcile}.py` — 합성 봉과 `tmp_path`만 쓰는 오프라인 테스트.
