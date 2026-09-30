# src/exits — 청산 신호 생성 (ExitSignalGenerator)

## 1. 목적

청산 규칙의 **단일 소스**. Stop Loss / Take Profit / Time Exit / Trend Break / EOD Flatten 을
순수 결정론 코드로 판정한다. live / paper / backtest 가 **동일 엔진을 공유**하므로 청산 로직이 갈라질 수 없다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `PositionView(symbol, quantity, entry_price, current_price, leverage_factor=1, entry_time=None, entry_bar=None, ema20=None)` |
| | `ExitContext(is_eod_window=False, now_time=None, now_bar=None)` |
| 출력 | `evaluate()` → `str \| None` (청산 사유) / `generate_intents()` → `list[OrderIntent]` (SELL, 전량) |

## 3. 주요 클래스/함수

- `ExitSignalGenerator(config: ExitConfig | None = None)` — `generator.py:66`
- `.evaluate(pos, ctx) -> str | None` — `generator.py:118` (EOD 포함 전체 우선순위)
- `.evaluate_signal(pos, ctx) -> str | None` — `generator.py:104` (EOD 제외)
- `.generate_intents(positions, ctx) -> list[OrderIntent]` — `generator.py:129`
- `.evaluate_intrabar(pos, bar) -> ExitFill | None` — 완료 봉 low/high 기반 손절(우선)·익절과 갭 참조가
- `.evaluate_bar_open(pos, ctx, *, bar_open) -> ExitFill | None` — 봉 시가의 TIME_EXIT(분 단위) > EOD_FLATTEN
  (`evaluate()` 는 변경하지 않았다. 사용처: `src/freeze_paper/engine.py`)
- `ExitConfig` (frozen) — `generator.py:32`: `stop_loss_pct=0.0, take_profit_pct=0.0,
  time_exit_bars=None, time_exit_minutes=None, enable_all_symbols_eod_flatten=False,
  leverage_eod_flatten=True, exit_on_trend_break=False,
  block_all_entries_in_eod_window=False` (0/None = 끔)
  - `block_all_entries_in_eod_window` 는 **청산 규칙이 아니라 진입 정책**이다(이 생성기는 읽지
    않는다). 청산 창의 정의(`eod_flatten_minutes_before_close`)와 짝이라 여기 함께 두고,
    실제 소비는 `RiskManager` 가 한다 — `run_paper.py` 가 `RiskConfig` 로 배선. 자세한 표는
    `src/risk/README.md` 참조.

### 고정 우선순위

```
STOP_LOSS > TAKE_PROFIT > TIME_EXIT > (TREND_BREAK) > EOD_FLATTEN
```

## 4. 다른 모듈과의 관계

`src/risk/models.py` 만 import (상수 + `OrderIntent`).
사용처: `src/live/paper_loop.py`, `src/live/models.py`(`LoopConfig.exit_config` 기본값),
`src/backtest/engine.py`, `src/runtime/config.py`.

생성된 청산은 `RISK_REDUCING` 주문이므로 **기존 RiskManager → OrderManager → Broker 경로를 그대로 재사용**한다.
청산 전용 실행 배선이 따로 없는 이유다.

## 5. 절대 하면 안 되는 것

- ❌ **AI 는 청산에 절대 관여하지 않는다.** `generator.py:4-5`: *"AI 는 청산에 절대 관여하지 않는다.
  청산은 순수 결정론 코드(현재 시각/난수 미사용; 판단은 주입된 스냅샷 필드에만 의존)."* `src/ai` import 금지.
- ❌ **현재 시각을 직접 읽지 않는다.** `ExitContext.is_eod_window` 등은 호출자가 market-correct 하게 주입한다.
- ❌ **우선순위를 바꾸지 않는다.** 손절이 익절보다 항상 먼저다.
- ❌ **사유 문자열(kind 상수)의 이름을 바꾸지 않는다.** 사유는 곧 `OrderIntent.kind` 이고,
  journal → FIFO 를 거쳐 **그대로 `RealizedTrade.exit_reason` 으로 전파**된다(`generator.py:13-14`).
  상수를 바꾸면 저장된 exit_reason 값이 조용히 달라진다.

## 6. 사용 예시

```python
from src.exits.generator import ExitConfig, ExitContext, ExitSignalGenerator, PositionView

gen = ExitSignalGenerator(ExitConfig(stop_loss_pct=0.01, take_profit_pct=0.015, time_exit_bars=30))
intents = gen.generate_intents(
    [PositionView("TQQQ", 10, entry_price=50.0, current_price=49.0, leverage_factor=3, entry_bar=5)],
    ExitContext(is_eod_window=False, now_bar=40),
)
# -> [OrderIntent(kind="STOP_LOSS", side="SELL", ...)]  → RiskManager → OrderManager
```

yaml 설정은 `exits:` 섹션(`config/runtime_*.yaml`)에서 읽는다.
섹션이 없으면 보수적 기본값(전종목 EOD off, SL/TP off, 레버리지 EOD on).

## 7. 테스트

`tests/test_exits.py` — exits + RiskManager + OrderManager + PaperBroker + AI 엔진을 함께 돌려
**AI 가 청산에 영향을 못 준다**는 것까지 통합 검증한다.
