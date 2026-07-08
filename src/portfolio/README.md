# src/portfolio — 자금관리 (PortfolioAccount)

## 1. 목적

Paper Trading 의 단일 계좌 자금관리. 현금 / 포지션 / 실현손익을 추적하되
**실현손익 계산은 전부 `FifoMatcher` 에 위임**한다. `performance.py` 는 backtest 와 paper 가
공유하는 성과지표 함수다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `FillEvent`(via `apply_fill`), `marks: dict[str, float]` 가격맵 |
| 출력 | `PortfolioValue(cash, market_value, realized_pnl, unrealized_pnl, total_equity)` |
| | `positions() -> dict[symbol, {"quantity", "avg_price"}]`, `peak_equity`, `last_drawdown_pct`, `equity_curve` … |

`performance_summary(...) -> dict` (12개 키): `total_return_pct, win_rate, profit_factor, avg_win,
avg_loss, expectancy, max_drawdown_pct, sharpe_ratio, total_trades, max_consecutive_losses,
initial_capital, final_equity`.

## 3. 주요 클래스/함수

- `PortfolioAccount(initial_capital: float, bar_minutes: float = 1.0)` — `account.py:39`
  (⚠️ `bar_minutes` 기본값이 `FifoMatcher` 의 `5.0` 과 다르다)
- `.apply_fill(fill: FillEvent) -> None` — `account.py:52`
- `.portfolio_value(marks: dict) -> PortfolioValue` — `account.py:95`
- `.mark_equity(marks, day_key=None) -> float` — `account.py:106` (peak / drawdown / 일간수익률 / 자산곡선 갱신)
- `.can_afford(notional: float) -> bool` — `account.py:102`
- `PortfolioAccount.from_fills(initial_capital, fills, bar_minutes=1.0)` — `account.py:141` (재시작 복원)
- `performance_summary(initial_capital, equity_curve, realized_trades, *, periods_per_year=None) -> dict` — `performance.py:21`

## 4. 다른 모듈과의 관계

`account.py` → `src/pnl/fifo`, `src/pnl/models`, `src/risk/models`(BUY).
`performance.py` 는 **src 의존성이 0** (stdlib `math` 뿐).
사용처: `src/live/paper_loop.py`(계좌), `src/backtest/engine.py` + `src/runtime/paper_runtime.py`(성과).

## 5. 절대 하면 안 되는 것

- ❌ **계좌가 자체 PnL 매칭을 구현하지 않는다.** 반드시 `FifoMatcher` 를 재사용한다(`account.py:4`).
- ❌ **계좌는 가격을 모른다.** 평가금액·미실현손익은 항상 주입된 `marks` 로만 계산하고,
  누락 심볼은 `avg_price`/`entry_price` 로 폴백한다.
- ❌ **`performance_summary` 의 산식을 바꾸지 않는다.** *"BacktestEngine._metrics 의 원래 산식과
  정확히 일치해야 한다"*(`performance.py:5`) — backtest/paper 성과 비교가 무의미해진다.
- ❌ 현재 시각·난수 사용 금지. 상태는 `FillEvent` 적용 순서의 순수 함수다
  (`test_from_fills_rebuild_equals_sequential_applies` 가 고정).

`sharpe_ratio` 는 수익률이 2개 미만이거나 분산이 0 이면 `None`. `profit_factor` 는 손실이 없으면 `None`.
`total_equity == cash + market_value` 는 구성상 성립.

## 6. 사용 예시

```python
from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.portfolio.performance import performance_summary
from src.risk.models import BUY

acct = PortfolioAccount(3_000_000, bar_minutes=1.0)
acct.apply_fill(FillEvent("SOXL", BUY, 10.0, 100.0, fee=0.5, ref_price=100.0))
acct.mark_equity({"SOXL": 105.0}, day_key="2026-07-08")
pv = acct.portfolio_value({"SOXL": 105.0})

summary = performance_summary(acct.initial_capital, acct.equity_curve,
                              acct.realized_trades(), periods_per_year=252)
```

> ⚠️ 통화 한계(v1): 자본은 KRW 명목, US 종목가는 USD. 상대 지표(수익률/PF/Sharpe/MDD)는 단위가 상쇄돼
> 유효하지만 **절대 금액은 참고용**이다.

## 7. 테스트

`tests/test_portfolio.py` — 현금 회계, 가중평균 포지션, `PortfolioValue`, 라운드트립 실현손익,
`can_afford`, `mark_equity`(peak/drawdown/일간수익률/곡선), `from_fills` 동치성, Sharpe 수기검증.
