# src/providers — 시세 수집 (Toss Open API)

## 1. 목적

토스증권 오픈 API 접근 계층. OAuth2 토큰 관리 + 원시 캔들 HTTP fetch(`TossClient`), 그리고
원시 응답 → Canonical `Candle` 변환(`TossMarketDataProvider`). **그 외의 일은 하지 않는다.**

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `Settings`(`src/config.py`), `symbol`, `interval`(`1m`/`1d`), `count`, `before` 커서 |
| 출력 | `TossClient.get_candles` → 원시 `dict` / `TossMarketDataProvider` → `list[Candle]` |

## 3. 주요 클래스/함수

- `TossClient(settings: Settings, timeout: float = 20.0)` — `toss_client.py:19`
- `TossClient.get_candles(symbol, interval, count=200, before=None) -> dict` — `toss_client.py:64`
  - `count` 를 `[1, 200]` 으로 clamp. **HTTP 401 시 캐시 토큰을 폐기하고 정확히 1회 재시도**
    (서버측 토큰 무효화 무중단 복구).
- `TossMarketDataProvider(settings: Settings)` — `toss_market_data.py:20`
- `.recent(symbol, interval, count=300) -> list[Candle]` — `ts_utc` 오름차순
- `.fetch_page(symbol, interval, count=200, before=None) -> tuple[list[Candle], str | None]`
- `.iter_history(symbol, interval, *, max_pages, page_size=200, sleep_s=0.4) -> Iterator[Candle]`

## 4. 다른 모듈과의 관계

```
src/config.py ──► providers ──► src/models.py (Candle)
                     │
                     └──► src/ingest/backfill.py, scripts/run_paper.py
```

`src/models.py`(Canonical Candle)와 `src/config.py` 외에는 **아무것도 import 하지 않는다.**
`src/` 안에서 이 모듈을 쓰는 곳은 `src/ingest/backfill.py` 뿐이다.

## 5. 절대 하면 안 되는 것

- ❌ **지표를 계산하지 않는다.** `toss_market_data.py:3`: *"지표 계산/판단은 여기 없다. 오직 Toss 응답 -> Canonical 변환만."*
- ❌ **주문 메서드를 두지 않는다.** `toss_client.py:6`: *"읽기 전용. 주문 메서드는 여기 없다."*
  주문은 `src/order/` 의 `BrokerProvider` 책임.
- ❌ 종목 선별·매매 판단 금지. Provider 는 fetch/변환만 한다.
- ❌ Provider 원시 dict 를 상위 계층으로 흘려보내지 말 것 — 경계에서 반드시 `Candle` 로 변환한다.

## 6. 사용 예시

```python
from src.config import load_settings
from src.providers.toss_market_data import TossMarketDataProvider

with TossMarketDataProvider(load_settings()) as p:
    candles = p.recent("AAPL", "1m", count=300)
    for c in p.iter_history("AAPL", "1m", max_pages=5):
        ...
```

## 7. 테스트

- `tests/test_toss_client.py` — `TossClient`(인증/토큰 캐시/401 재시도) 커버.
- ⚠️ `toss_market_data.py` 는 직접 테스트가 없다(상위 계층 테스트에서 간접 커버).
