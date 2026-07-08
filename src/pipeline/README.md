# src/pipeline — 한 사이클 오케스트레이션 (TradingPipeline)

## 1. 목적

MarketData → Indicator → Screener → AI → Risk → Order(Paper) 를 **한 사이클** 연결하는 커넥터.
**연결만 한다** — 지표/AI/리스크 판단을 여기서 구현하지 않는다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `cycle_id`, `universe`, `candles_by_symbol`, `portfolio: PortfolioSnapshot`, `system_state: SystemState`, `market: MarketContext`, `exit_intents` |
| 출력 | `PipelineResult` → `list[SymbolOutcome]` (`action` = `ORDERED`/`SKIPPED`, `reason`, `ai_decision`, `risk_decision`, `order_record`, …) |

## 3. 주요 클래스/함수

- `TradingPipeline(indicator_engine, screener, ai_engine, risk_manager, order_manager, config=None, leverage_by_symbol=None)` — `pipeline.py:46`
- `.run_once(*, cycle_id, universe, candles_by_symbol, portfolio, system_state, market, exit_intents=None) -> PipelineResult` — `pipeline.py:57` (키워드 전용)
- `PipelineConfig(entry_notional_pct=0.05)` — `pipeline.py:40`
- `SystemState(kill_switch=False, block_new_entries=False)` — `models.py:24`

### 실행 순서

1. **청산 먼저** — `exit_intents` 는 **AI 를 건너뛰고** 바로 Risk → Order (`pipeline.py:67-80`)
2. **진입** — 정렬된 유니버스마다 지표 → Screener → Risk 평가 → AI 필터 → `is_entry_permitted` → Order

진입 차단은 스냅샷 **복사본**에 적용된다(`replace(portfolio, kill_switch=block_entries)`, `:64`)
→ 진입을 막아도 청산 경로는 영향받지 않는다.

## 4. 다른 모듈과의 관계

`src/ai`, `src/risk`, `src/universe`, `src/models` 를 import. `src/live/paper_loop.py` 가 호출한다.
`client_order_id` 는 결정론적으로 생성된다: `f"{symbol}:{cycle_id}"`, 청산은 `f"{symbol}:{cycle_id}:{intent.kind}"`.

## 5. 절대 하면 안 되는 것

- ❌ **비즈니스 로직을 넣지 않는다.** 도크스트링: 지표/AI/리스크 판단을 파이프라인이 직접 구현 금지. 연결만.
- ❌ **실주문 금지.** `OrderManager` 는 반드시 `PaperBrokerProvider` 로 생성되어야 한다.
- ❌ **청산에 AI 를 태우지 않는다.** `EXIT`/`EOD_FLATTEN` 은 AI 를 우회해 Risk→Order 로 직행한다.
- ❌ 현재 시각·난수 사용 금지. 유니버스는 정렬해서 순회하고 `client_order_id` 도 결정론적이어야 한다.

> ⚠️ **알려진 버그**: `pipeline.py:100` 이 `market` 파라미터(`MarketContext`)를 시장 문자열(`"US"`/`"KR"`)로
> 덮어쓴다. 그 결과 `:117` 의 `ai.decide(...)` 에는 `MarketContext` 대신 문자열이 전달되고,
> `session` / `is_eod_flatten_window` 가 AI 프롬프트·캐시 키에 **도달하지 못한다.**
> (문서화만 해둠 — 수정은 별도 작업.)

## 6. 사용 예시

```python
pipeline = TradingPipeline(IndicatorEngine(), UniverseScreener(), ai_engine,
                           risk_manager, order_manager,
                           PipelineConfig(entry_notional_pct=0.05))

result = pipeline.run_once(
    cycle_id="20260708T143000Z",
    universe=["SOXL"],
    candles_by_symbol={"SOXL": candles},
    portfolio=snapshot,
    system_state=SystemState(),
    market=MarketContext(session="REGULAR"),
    exit_intents=[],
)
result.orders()   # ORDERED 인 SymbolOutcome 만
```

## 7. 테스트

`tests/test_pipeline.py` (주 테스트). `tests/test_exits.py`, `tests/test_journal.py`,
`tests/test_paper_loop.py`, `tests/test_replay.py` 에서도 사용.
