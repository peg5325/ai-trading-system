# src/order — 주문 실행 (OrderManager + BrokerProvider)

> 설계 문서상 "orders / brokers". 실제 폴더는 `order/` 하나이고, 브로커 구현이 그 안에 있다.

## 1. 목적

실행 계층. 리스크 승인을 받은 `OrderIntent` 를 `OrderRequest` 로 바꿔 `BrokerProvider` 에 넘기고,
체결 결과를 `OrderStore` / `PositionStore` 에 기록한다. **브로커는 교체 가능한 인터페이스**이며,
현재 실체결은 Paper 뿐이다.

```
OrderIntent → (RiskManager) → OrderRequest → BrokerProvider → BrokerOrderResult → OrderRecord
```

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `OrderIntent` + `RiskDecision` (+ `client_order_id`, `ref_price`) |
| 출력 | `OrderRecord` — **거절된 경우에도 항상 반환**된다 |

## 3. 파일별 역할

| 파일 | 내용 |
|---|---|
| `models.py` | `OrderRequest` / `BrokerOrderResult` / `OrderRecord`, Protocol(`BrokerProvider`, `OrderStore`, `PositionStore`), `InMemory*Store`, 상태 상수 |
| `manager.py` | `OrderManager` — 멱등성·리스크 게이트·수량 검증 |
| `paper_broker.py` | `PaperBrokerProvider` — 모의 체결(결정론적, 전량 체결) |
| `toss_broker.py` | `TossBrokerProvider` — **skeleton. `raise NotImplementedError`** |

### 주요 시그니처

- `OrderManager(broker, order_store, position_store)` — `manager.py:26`
- `.submit(intent, risk_decision, *, client_order_id, ref_price, order_type=TYPE_MARKET, limit_price=None) -> OrderRecord` — `manager.py:32`
- `PaperBrokerProvider(execution: ExecutionModel | None = None, reject_symbols: set[str] | None = None, fee_model: Callable[[str, float], float] | None = None)` — `paper_broker.py:18`
  (`fee_model(side, fill notional)` 을 주면 수수료를 그것으로 계산. None 이면 `ExecutionModel.fee` — 기존 동작)

### `submit()` 의 3단 게이트 (순서 고정)

1. **멱등성** — 이미 있는 `client_order_id` 면 저장된 record 를 그대로 반환, 브로커 재호출 없음 (`manager.py:36`)
2. **리스크 게이트** — `not risk_decision.allowed` → `REJECTED / "RISK_BLOCKED"`, **브로커 호출 안 함** (`manager.py:41`)
3. **수량 검증** — `adjusted_quantity <= 0` → `REJECTED / "NON_POSITIVE_QUANTITY"` (`manager.py:47`)

> 브로커로 나가는 수량은 `intent.quantity` 가 아니라 **`risk_decision.adjusted_quantity`** 다.

## 4. 다른 모듈과의 관계

`src/risk/models.py` 를 import 하고, `paper_broker.py` 는 `src/backtest/execution.ExecutionModel` 을 쓴다
— **백테스트와 Paper 가 동일한 체결 모델을 공유**한다(슬리피지/수수료 일치).
조립은 composition root(`scripts/run_paper.py`)에서 이뤄진다.

## 5. 절대 하면 안 되는 것

- ❌ **AI 를 import 하지 않는다.** `OrderManager` 는 AI 의 존재를 모른다. AI→Risk 순서는 `src/pipeline` 이 강제한다.
- ❌ **`risk_decision.allowed` 없이 브로커를 호출하지 않는다.** 도크스트링: *"RiskDecision.allowed=True 인
  주문만 브로커로 전달 (AI 승인만으로는 불가)."* AI 의 `APPROVE_ENTRY` 하나로는 아무 일도 일어나지 않는다.
- ❌ **여기서 리스크를 재판정하지 않는다.** `manager.py:8`: *"kill switch 등은 RiskManager 가 판단 → 여기선 allowed 만 신뢰"*
  그래서 kill switch 하에서도 RiskManager 가 허용한 EXIT / EOD_FLATTEN 은 실행된다.
- ❌ **`TossBrokerProvider` 로 실주문을 붙이지 않는다.** *"실주문은 Paper Trading 통과 후 승인 단계에서 구현합니다."*
  현재 `place_order()` 는 `NotImplementedError` 이며 **참조하는 코드가 하나도 없다.**
- ❌ 같은 `client_order_id` 로 중복 체결 금지 — 멱등성 검사를 우회하지 말 것.

## 6. 사용 예시

```python
from src.backtest.execution import ExecutionModel
from src.order.manager import OrderManager
from src.order.models import InMemoryOrderStore, InMemoryPositionStore
from src.order.paper_broker import PaperBrokerProvider

om = OrderManager(
    PaperBrokerProvider(ExecutionModel(slippage_bps=5.0, fee_bps=2.0)),
    InMemoryOrderStore(), InMemoryPositionStore(),
)
record = om.submit(intent, risk_decision, client_order_id="SOXL:20260708T143000Z", ref_price=50.0)
record.status   # FILLED / REJECTED
```

## 7. 테스트

`tests/test_order_manager.py` — 체결, 리스크 차단, 중복 `client_order_id`, 수수료/슬리피지,
EOD flatten, kill switch 하의 EXIT 허용, 거절 경로.
⚠️ `toss_broker.py` 는 테스트가 없다(구현이 없으므로).
