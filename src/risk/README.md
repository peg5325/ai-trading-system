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
- `RiskConfig` (frozen) — `risk_manager.py:56`: `max_symbol_notional_pct=0.20, max_symbol_effective_pct=0.30,
  max_portfolio_effective_pct=0.50, daily_loss_limit_pct=0.03, max_drawdown_pct=0.10,
  max_consecutive_losses=3, max_positions=2, extended_hours_qty_factor=0.5`
- `Position.effective_exposure` = `notional * leverage_factor` — `models.py:45`
- 수량이 한도를 넘으면 **거절이 아니라 축소**될 수 있다 → `allowed=True` + `RC_QTY_REDUCED` (`risk_manager.py:160`)

### 핵심 불변식 — 청산은 언제나 통과한다

`evaluate()` 는 `intent.kind in RISK_REDUCING` 이면 kill switch / 일일손실 / drawdown 검사보다
**먼저** 짧게 반환한다(`risk_manager.py:81`). 즉 **kill switch 가 켜져 있어도 빠져나올 수 있다.**
청산 수량은 보유 수량으로 clamp 되고, 포지션이 없으면 `allowed=True, adjusted_quantity=0.0, [RC_NOTHING_TO_REDUCE]`.

## 4. 다른 모듈과의 관계

`src/models.py`(세션 상수)와 자기 `models.py` 만 import.
**이 프로젝트에서 가장 많이 참조되는 모듈**: `order`, `exits`, `pipeline`, `backtest`, `live`,
`portfolio`, `pnl`, `runtime` 이 모두 `src/risk/models.py` 의 상수·타입을 쓴다.

## 5. 절대 하면 안 되는 것

- ❌ **RiskManager 가 Broker 를 호출하지 않는다.** 도크스트링: *"Broker/Provider 에 접근하지 않는다.
  RiskManager 는 오직 이 스냅샷 구조만 평가한다."* 판정만 내리고, 실행은 `OrderManager` 가 한다.
- ❌ **현재 시각을 읽지 않는다.** *"결정론적: 현재 시각이 아니라 snapshot 필드에 의존."*
  시간은 오직 `snapshot.session` / `snapshot.is_eod_flatten_window` 로 주입된다. 시계·난수·I/O 없음.
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
