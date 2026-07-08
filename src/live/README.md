# src/live — 실시간 Paper 루프 (PaperTradingLoop)

## 1. 목적

실시간 폴링 드라이버. 1분봉을 가져와 5분봉으로 리샘플하고, **새로 마감된 5분봉마다 정확히 한 번**
`pipeline.run_once()` 를 호출한다. 체결은 Paper 뿐이다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `MarketDataProvider`(Protocol), `CandleStore`, `TradingPipeline`, `position_store`, `LoopConfig`, (옵션) `journal` |
| 출력 | `CycleReport(cycle_id, status, reason, session, pipeline_result, error)` — status ∈ `RAN`/`SKIPPED`/`ERROR`/`STOPPED` |
| 부수효과 | 캔들 upsert, `PortfolioAccount` 에 체결 반영, `JournalCycle` 기록 |

## 3. 주요 클래스/함수

- `PaperTradingLoop(market, candle_store, pipeline, position_store, config=None, result_log=None, journal=None, sleep=time.sleep)` — `paper_loop.py:67`
- `.tick() -> CycleReport` — `paper_loop.py:111` (폴링 1회 — 테스트는 이걸 직접 부른다)
- `.run() -> None` — `paper_loop.py:104` (tick 루프, `sleep` 주입 가능)
- `.stop() / .pause() / .resume() / .set_kill_switch(on)` — `:99-102`
- `LoopConfig` — `models.py:35`: `universe=("SOXL",), recent_1m_count=200, polling_interval_s=20.0,
  eod_flatten_before_min=10, initial_capital=100_000.0, max_positions=2, exit_config=ExitConfig()`
- `frame_to_candles(df, symbol) -> list[Candle]` — `:55` (`src/replay/system.py` 도 재사용)

### 생존 우선 설계

- 심볼별 fetch 실패는 격리(`:120-127`). **전 종목이 실패해야만** 사이클 전체 skip (`:129`).
- 파이프라인 예외는 `ST_ERROR` 로 흡수되고 루프는 계속 돈다 (`:183-186`).
- 중복 봉 가드: `newest <= _last_processed` → `R_NO_NEW_BAR` (`:154`).
- **재시작 안전**: `PortfolioAccount.from_fills(...)` 로 저널에서 계좌를 복원하고,
  bar index 는 절대 UTC 시각에서 유도한다 → `time_exit_bars` 가 재시작을 견딘다.

## 4. 다른 모듈과의 관계

`engines`, `exits`, `pipeline`, `ai`(MarketContext), `pnl`, `portfolio`, `risk`, `universe` 를 import.
`scripts/run_paper.py` 가 조립하고 `src/runtime/PaperRuntime` 이 감싼다.

## 5. 절대 하면 안 되는 것

- ❌ **실주문 금지.** `PaperBrokerProvider` 만 사용한다.
- ❌ **미완결 봉으로 거래하지 않는다.** 새로 마감된 봉에서만 사이클을 돈다.
- ❌ **관측 실패로 매매를 멈추지 않는다.** 반대로, 데이터가 아예 없으면 매매하지 않는다(skip).
- ❌ 사이클 예외를 밖으로 던지지 않는다 — 흡수하고 다음 tick 으로 간다.

> ⚠️ 알려진 한계:
> - `eod` 는 대표 캔들 하나에서 뽑는 **전역 플래그**(`:168-169`) — 단일 시장 유니버스에서만 정확하고,
>   US/KR 혼합에서는 부정확하다.
> - EOD flatten 은 US 전용(`_is_eod_flatten_window` 가 `MARKET_US` 가 아니면 False, `:247`).
> - `LoopConfig.account_value` / `max_positions` 는 이 파일에서 읽히지 않는다
>   (계좌는 `initial_capital`, `max_positions` 는 `run_paper.py` 가 `RiskConfig` 로 넘긴다).

## 6. 사용 예시

```python
loop = PaperTradingLoop(
    market, candle_store, pipeline, position_store,
    LoopConfig(universe=("SOXL",), polling_interval_s=20.0,
               initial_capital=100_000.0,
               exit_config=ExitConfig(stop_loss_pct=0.01, take_profit_pct=0.015)),
    journal=journal,
)
report = loop.tick()      # 테스트/단발 실행
# loop.run()              # 실제 운영은 PaperRuntime 이 감싸서 호출
```

## 7. 테스트

`tests/test_paper_loop.py` (주 테스트). `tests/test_exits.py`, `tests/test_runtime.py`,
`tests/test_metrics_store.py` 도 `FakeMarketDataProvider` 로 루프를 돌린다.
