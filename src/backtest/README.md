# src/backtest — 백테스트 엔진 (BacktestEngine)

## 1. 목적

결정론적 long-only 백테스터. 저장된 canonical 캔들을 **live/paper 와 동일한**
`IndicatorEngine` / `UniverseScreener` / `RiskManager` / `ExitSignalGenerator` / `FifoMatcher` 에 흘린다.
Provider 나 Broker 에는 절대 닿지 않는다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `list[Candle]`, `symbol`, `leverage_factor`, (옵션) `entry_strategy`, `BacktestConfig` |
| 출력 | `BacktestReport(symbol, start, end, summary: dict, trades: list[Trade])` |

`summary` = `performance_summary()` + `exposure_time` + `sample_status`(`OK` / `INSUFFICIENT_SAMPLE`).

## 3. 주요 클래스/함수

- `BacktestEngine(config: BacktestConfig | None = None, risk_manager: RiskManager | None = None)` — `engine.py:63`
- `.run(candles, symbol, leverage_factor=1, entry_strategy=None) -> BacktestReport` — `engine.py:68`
- `.run_in_out(candles, symbol, leverage_factor=1, ratio=0.7, entry_strategy=None)` — `engine.py:220`
- `.walk_forward(...)` — `engine.py:227`, **`NotImplementedError`** (예약)
- `ExecutionModel(slippage_bps=5.0, fee_bps=2.0)` — `execution.py:21`;
  `.execute(requested_qty, ref_price, side) -> Fill` — `execution.py:32`
- `BacktestConfig` (frozen) — `models.py:55`: `initial_capital=100_000.0, entry_notional_pct=0.05,
  stop_loss_pct=0.03, take_profit_pct=None, exit_on_trend_break=True, leverage_eod_flatten=True,
  allow_overnight_non_leverage=True, min_trades=100, execution=ExecutionModel(), risk=RiskConfig()`
- `split_in_out(candles, ratio=0.7)` — `models.py:95`

## 4. 다른 모듈과의 관계

`engines`, `exits`, `pnl`, `portfolio`, `risk`, `universe`, `models` 를 import.
반대로 `src/order/paper_broker.py` 와 `scripts/run_paper.py` 가 이 모듈의 **`ExecutionModel` 을 가져다 쓴다**
— 그래서 백테스트와 Paper 의 체결(슬리피지/수수료)이 정확히 일치한다.

## 5. 절대 하면 안 되는 것

- ❌ **Look-ahead 금지.** 진입 전략은 `indicators.iloc[:t+1]` 만 읽어야 한다(`ScreenerEntryStrategy` 가 강제).
- ❌ **신호봉 종가로 체결하지 않는다.** 봉 `t` 종가에 판단 → 봉 `t+1` **시가**에 체결
  (`pending_entry` / `pending_exit`). `engine.py:8` 이 명시적으로 금지.
- ❌ **Provider / Broker 접근 금지.** 캔들은 밖에서 주입받는다.
- ❌ 현재 시각·난수 사용 금지.
- ❌ live 와 다른 지표/청산 코드를 쓰지 않는다 — 동일 엔진 공유가 이 모듈의 존재 이유다.

> **의도된 예외**(`engine.py:157-160`): 세션 **마감봉**에서는 백테스트가 EOD flatten 을 신호 청산보다
> 먼저 적용하고 그 봉의 **종가**로 체결한다(종가 체결 충실도 보존). live/paper 는 `evaluate()` 의
> `SL>TP>TIME>EOD` 우선순위를 따른다. **마감봉 외 모든 봉은 동일한 `evaluate_signal` 을 공유**한다.
> 버그처럼 보이지만 아니다.

잔여 포지션은 마지막 종가로 강제 청산되고 사유는 `END_OF_DATA`.

## 6. 사용 예시

```python
from src.backtest.engine import BacktestEngine
from src.backtest.execution import ExecutionModel
from src.backtest.models import BacktestConfig

eng = BacktestEngine(BacktestConfig(
    initial_capital=100_000.0,
    stop_loss_pct=0.03,
    execution=ExecutionModel(slippage_bps=5.0, fee_bps=2.0),
))
report = eng.run(candles, "SOXL", leverage_factor=3)
report.summary["total_return_pct"], report.summary["sample_status"]
```

## 7. 테스트

`tests/test_backtest.py` — look-ahead, next-bar-open 체결, 슬리피지/수수료, EOD flatten,
MDD, 표본 부족, in/out split, `walk_forward` 의 `NotImplementedError`.
