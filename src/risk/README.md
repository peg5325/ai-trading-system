# src/risk — 리스크 관리 (RiskManager)

## 1. 목적

**주문 허용 여부의 최종 권한.** `OrderIntent` 를 `PortfolioSnapshot` 에 비추어 결정론적으로 평가하고,
**실질 노출(effective exposure = notional × leverage_factor)** 기준으로 수량을 사이징한다.
AI / Screener / Rule 이 무슨 신호를 내든, allow/block 은 여기서 정해진다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `OrderIntent(symbol, kind, side, quantity, price, leverage_factor=1, risk_flags=[])` |
| | `PortfolioSnapshot(account_value, positions, daily_pnl_pct, drawdown_pct, consecutive_losses, kill_switch, session, is_eod_flatten_window, cash)` |
| 출력 | `RiskDecision(allowed: bool, reason_codes: list[str], adjusted_quantity: float, risk_flags: list[str])` |

이 모듈이 **주문 종류 어휘의 원산지**다: `NEW_ENTRY, ADD, EXIT, EOD_FLATTEN, STOP_LOSS,
TAKE_PROFIT, TIME_EXIT, TREND_BREAK` + `BUY/SELL` + 집합 `TRIGGERED_EXITS`, `RISK_REDUCING`, `ENTRY_KINDS`.

## 3. 주요 클래스/함수

- `RiskManager(config: RiskConfig | None = None)` — `risk_manager.py:71`
- `.evaluate(intent, snapshot) -> RiskDecision` — `risk_manager.py:74`
- `RiskConfig` (frozen) — `risk_manager.py:56`, 전체 12개 필드:
  ```python
  max_symbol_notional_pct=0.20        # 단일 종목 최대 명목 노출
  max_symbol_effective_pct=0.30       # 단일 종목 최대 실질 노출
  max_portfolio_effective_pct=0.50    # 전체 최대 실질 노출
  daily_loss_limit_pct=0.03           # 일일 손실 한도
  max_drawdown_pct=0.10               # 최대 낙폭
  max_consecutive_losses=3
  max_scale_ins=2                     # 최초 진입 후 추가 진입 허용 횟수
  block_extended_hours_risky=True     # 연장시간 리스크 주문 차단(False=감량 모드)
  extended_hours_qty_factor=0.5       # ↑ 가 False 일 때만 적용되는 수량 배수
  max_positions=2                     # 동시 보유 최대 종목 수(신규 진입 기준)
  reentry_cooldown=ReentryCooldownConfig()        # 재진입 쿨다운(기본 off, NEW_ENTRY 만)
  block_all_entries_in_eod_window=False           # EOD 청산창 전종목 진입차단(기본 off)
  ```
  `extended_hours_qty_factor` 는 **`block_extended_hours_risky=False` 일 때만** 의미가 있다.

### EOD 청산창 진입 차단 (`block_all_entries_in_eod_window`)

`snapshot.is_eod_flatten_window=True` 인 창에서 신규/추가 진입을 막는 규칙:

| 옵션 | 레버리지(>1x) | 일반 종목(1x) |
|---|---|---|
| `False`(기본) | 차단 | **허용** — 기존 동작 |
| `True` | 차단 | 차단 |

둘 다 사유는 `NEAR_CLOSE_NO_NEW_ENTRY`. **청산(RISK_REDUCING)은 어느 쪽이든 미차단** — 이
검사는 `_evaluate_entry`(진입 전용 경로) 안에 있고 청산은 그 앞에서 이미 분기된다.

`True` 가 필요한 이유(US s001 실측): `exits.enable_all_symbols_eod_flatten=true` 로 전 종목을
청산해도 진입 차단은 레버리지에만 걸려 있어, 15:50 청산 → 15:55 재진입 → 다음 EOD 사이클이
창 밖(POST)이라 미청산 → **오버나잇 잔여 포지션**이 남았다. 값 출처는
`exits.block_all_entries_in_eod_window`(`run_paper.py` 가 `RiskConfig` 로 배선).
- `Position.effective_exposure` = `notional * leverage_factor` — `models.py:45`
- 수량이 한도를 넘으면 **거절이 아니라 축소**될 수 있다 → `allowed=True` + `RC_QTY_REDUCED`
  (`risk_manager.py:194-198`, `_apply_exposure` 내부)

### 핵심 불변식 — 청산은 차단 검사를 우회한다

`evaluate()` 는 `intent.kind in RISK_REDUCING` 이면 kill switch / 일일손실 / drawdown 검사보다
**먼저** 짧게 반환한다(`risk_manager.py:81`). 즉 **kill switch 가 켜져 있어도 빠져나올 수 있다.**
청산 수량은 보유 수량으로 clamp 되고, 포지션이 없으면 `allowed=True, adjusted_quantity=0.0, [RC_NOTHING_TO_REDUCE]`.

> ⚠️ **"언제나"는 아니다.** `quantity <= 0 or price <= 0` 유효성 검사(`risk_manager.py:77-78`,
> `RC_INVALID_INTENT`)는 `RISK_REDUCING` 분기보다 **앞선다.** 마크 가격이 0/누락인 청산 의도는
> 거절된다 — 청산 intent 를 만들 때 유효한 `price` 를 반드시 넣어야 하는 이유다.

## 4. 다른 모듈과의 관계

`src/models.py`(세션 상수)와 자기 `models.py` 만 import.
**이 프로젝트에서 가장 많이 참조되는 모듈**: `order`, `exits`, `pipeline`, `backtest`, `live`,
`portfolio`, `pnl`, `runtime` 이 모두 `src/risk/models.py` 의 상수·타입을 쓴다.

## 5. 절대 하면 안 되는 것

- ❌ **RiskManager 가 Broker 를 호출하지 않는다.** 도크스트링: *"Broker/Provider 에 접근하지 않는다.
  RiskManager 는 오직 이 스냅샷 구조만 평가한다."* 판정만 내리고, 실행은 `OrderManager` 가 한다.
- ❌ **현재 시각을 읽지 않는다.** *"결정론적: 현재 시각이 아니라 snapshot 필드(session, is_eod_flatten_window)에 의존."*
  시계·난수·I/O 없음.
- ❌ **청산/손절/EOD flatten 을 막지 않는다.** *"kill switch·손실한도와 무관하게 허용."*
  생존이 수익보다 우선 — 진입은 막아도 탈출은 절대 막지 않는다.
- ❌ AI 결정을 신뢰해서 검사를 생략하지 않는다. AI 는 입력 컨텍스트일 뿐이다.

## 6. 사용 예시

```python
from src.risk.models import BUY, NEW_ENTRY, OrderIntent, PortfolioSnapshot
from src.risk.risk_manager import RiskConfig, RiskManager

rm = RiskManager(RiskConfig(max_positions=2, daily_loss_limit_pct=0.03))
intent = OrderIntent("TQQQ", NEW_ENTRY, BUY, quantity=10, price=50.0, leverage_factor=3)
decision = rm.evaluate(intent, PortfolioSnapshot(account_value=100_000.0, cash=50_000.0))

decision.allowed             # bool
decision.adjusted_quantity   # 한도에 맞춰 축소될 수 있음
```

## 7. 테스트

`tests/test_risk_manager.py` (주 테스트).
`tests/test_order_manager.py` 는 kill switch 하에서도 EXIT 이 실행되는 것을 검증한다.
