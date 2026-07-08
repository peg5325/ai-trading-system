# src/pnl — 실현손익 단일 엔진 (FIFO)

> 설계 문서상 "metrics". 실현손익 계산과 성과 지표 집계가 여기 있다.

## 1. 목적

**단 하나의 FIFO 실현손익 엔진.** live / paper / backtest 가 전부 이 모듈을 통과하므로
경로별로 숫자가 어긋날 수 없다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `FillEvent(symbol, side, quantity, price, fee=0.0, ref_price=None, timestamp=None, bar_index=None, reason=())` |
| 출력 | `RealizedTrade`(frozen), `OpenLot`(frozen), `PnlSummary` |

## 3. 파일별 역할

| 파일 | 내용 |
|---|---|
| `fifo.py` | `FifoMatcher` — 호출 순서대로 fill 을 받아 SELL 을 가장 오래된 BUY lot 과 매칭. `match_fills()` 편의 함수 |
| `journal_pnl.py` | 저널의 `FILLED` 주문을 시간순 `FillEvent` 로 바꿔 동일 matcher 에 태우는 어댑터 (live/paper 경로) |
| `metrics.py` | `summarize(trades, open_lots) -> PnlSummary` — 승률/PF/Expectancy/보유시간 |
| `models.py` | `FillEvent` / `RealizedTrade` / `OpenLot` / `PnlSummary` |

### 주요 시그니처

- `FifoMatcher(bar_minutes: float = 5.0)` — `fifo.py:36`
- `.ingest(fill) -> list[RealizedTrade]` — `fifo.py:44` (BUY 는 `[]`)
- `.ingest_many(fills)` / `.open_lots(symbol=None)` / `.open_quantity(symbol)`
- `match_fills(fills, *, bar_minutes=5.0) -> (trades, open_lots)` — `fifo.py:125`
- `summarize(trades, open_lots=None) -> PnlSummary` — `metrics.py:13`
- `realized_from_journal(journal, *, symbol=None, start=None, end=None, bar_minutes=5.0)` — `journal_pnl.py:58`

### 핵심 불변식

```
net_pnl == gross_pnl - slippage - commission
net_pnl == (exit_price - entry_price) * quantity - commission
```

구성상 **정확히** 성립한다(`fifo.py:94-96`), `tests/test_pnl.py:79` 가 assert 한다.
`ref_price is None` 이면 `ref := price` → `slippage == 0`.
`realized_pnl` 은 `net_pnl` 의 별칭(`models.py:48`).

## 4. 다른 모듈과의 관계

`src/risk/models.py` 의 `BUY`/`SELL` 상수만 import 하는 **최하위 leaf**.
사용처(가장 넓은 fan-in): `backtest/engine.py`, `portfolio/account.py`, `live/paper_loop.py`,
`cli/inspect.py`, `replay/system.py`, `runtime/paper_runtime.py`.

`FifoMatcher` 를 프로덕션에서 생성하는 **외부 호출자는 3곳뿐**이다 —
`backtest/engine.py:92`, `portfolio/account.py:42`, `pnl/journal_pnl.py:63`
(그 외에는 모듈 자체 편의함수 `fifo.py:127` 의 `match_fills` 내부 생성).
"단일 엔진"은 구호가 아니라 사실이다.

## 5. 절대 하면 안 되는 것

- ❌ **두 번째 손익 계산기를 만들지 않는다.** 어떤 모듈도 자체 PnL 산식을 갖지 않는다.
- ❌ **불변식을 깨지 않는다.** `net_pnl == gross_pnl - slippage - commission` 은 테스트로 고정돼 있다.
- ❌ **정렬을 엔진에 맡기지 않는다.** 엔진은 `ingest()` **호출 순서를 시간순으로 신뢰**한다 — 정렬은 호출자 책임.
- ❌ 현재 시각·난수 사용 금지.
- ❌ 초과 매도를 예외로 처리하지 않는다 — 감사용으로 `unmatched_sells` 에 방어적으로 기록한다(`fifo.py:118`).
- ❌ 부분청산 시 SELL 당 1건이 아니라 **lot 매칭 단위마다** `RealizedTrade` 가 생긴다는 점을 잊지 말 것.

## 6. 사용 예시

```python
from src.pnl.fifo import FifoMatcher
from src.pnl.metrics import summarize
from src.pnl.models import FillEvent
from src.risk.models import BUY, SELL

m = FifoMatcher(bar_minutes=5.0)
m.ingest(FillEvent("SOXL", BUY, 10.0, 100.05, fee=0.5, ref_price=100.0, bar_index=0))
closed = m.ingest(FillEvent("SOXL", SELL, 4.0, 109.9, fee=0.2, ref_price=110.0, bar_index=3))

t = closed[0]
assert abs(t.net_pnl - (t.gross_pnl - t.slippage - t.commission)) < 1e-9
summary = summarize(m.trades, m.open_lots())
```

조회: `./.venv/bin/python scripts/inspect_cli.py pnl`

## 7. 테스트

`tests/test_pnl.py` — 부분청산, FIFO 순서, 수수료 배분, 슬리피지 분해 + 불변식,
`ref_price=None`, 방어적 초과매도, `summarize`, 결정론, `realized_from_journal`.
