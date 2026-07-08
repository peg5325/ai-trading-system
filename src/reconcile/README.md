# src/reconcile — 정합성 대조 (ReconciliationService)

> ⚠️ **현재 미배선.** 모듈은 완성·테스트되어 있으나 `src/` 및 `scripts/` 어디에서도 호출하지 않는다.
> 실주문 도입 전에 반드시 연결해야 하는 안전장치다. (아래 §4 참조)

## 1. 목적

내부 상태(포지션 + 미체결 주문)와 **브로커의 실제 계좌**를 순수 결정론적으로 비교해
심각도 등급이 매겨진 불일치 리포트를 만든다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `BrokerProvider`(`get_positions`, `get_open_orders`), `PositionStore`, `OrderStore` — 모두 Protocol |
| 출력 | `ReconciliationReport(status, severity, mismatches[], recommended_action, block_new_entries)` |

### 심각도 → 조치

| severity | recommended_action | block |
|---|---|---|
| `CRITICAL` (`QUANTITY_MISMATCH`, `INTERNAL_ONLY_POSITION`, `BROKER_ONLY_POSITION`) | `BLOCK_NEW_ENTRIES_AND_RESYNC` | ✅ |
| `WARNING` (주문 불일치 전부) | `RESYNC_AND_MONITOR` | ❌ |
| `INFO` (`MARKET_VALUE_MISMATCH`) | `MONITOR` | ❌ |

## 3. 주요 클래스/함수

- `ReconciliationService(broker, position_store, order_store=None, config=None)` — `service.py:51`
- `.reconcile() -> ReconciliationReport` — `service.py:59`
- `ReconcileConfig` (frozen) — `service.py:42`: `quantity_tolerance=1e-6, avg_price_tolerance_pct=0.001,
  market_value_tolerance_pct=0.02, compare_market_value=True, compare_orders=True`
- `PositionView(symbol, quantity, average_price, market_value=0.0)` — `models.py:46`
- `OrderView(order_id, symbol, status, quantity, filled_quantity=0.0)` — `models.py:54`

## 4. 다른 모듈과의 관계

**`src/reconcile/models.py` 외에는 아무것도 import 하지 않는다.**

배선 지점은 이미 준비돼 있다: `SystemState.block_new_entries`(`src/pipeline/models.py:27`) 에
*"reconciliation block_new_entries 를 여기로"* 라는 주석이 있고 `TradingPipeline.run_once` 도 이를 존중한다
(`pipeline.py:62-63`). 하지만 `PaperTradingLoop` 는 `SystemState(kill_switch=...)` 만 만들고
`block_new_entries` 를 **설정하지 않는다** (`paper_loop.py:174`). 즉 현재 프로덕션 경로에서
`reconcile()` 을 부르는 곳이 없다.

## 5. 절대 하면 안 되는 것

- ❌ **상태를 바꾸지 않는다.** 조회된 상태만 비교한다. 주문 기능 없음.
- ❌ **구체 브로커(Toss/KIS)에 의존하지 않는다.** 오직 Protocol 만 본다.
- ❌ **현재 시각에 의존하지 않는다.** *"현재 시각에 의존하지 않고 조회된 상태만 비교한다."*
  symbol / order_id 정렬 순회로 결정론 보장.
- ❌ **CRITICAL 이라고 청산까지 막지 않는다.** `block_new_entries=True` 는 **신규 진입만** 막는다.
  청산·risk-reducing 주문은 `RiskManager` 가 여전히 허용해야 한다(`service.py:6-7`).

## 6. 사용 예시

```python
from src.reconcile.service import ReconcileConfig, ReconciliationService
from src.pipeline.models import SystemState

svc = ReconciliationService(broker, position_store, order_store,
                            ReconcileConfig(quantity_tolerance=1e-6))
report = svc.reconcile()

if report.block_new_entries:                       # CRITICAL
    system_state = SystemState(block_new_entries=True)   # → TradingPipeline.run_once
```

## 7. 테스트

`tests/test_reconcile.py` (유일한 사용처).
