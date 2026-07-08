# src/ai — AI 진입 필터 (AIDecisionEngine)

## 1. 목적

Screener 후보에 대한 **진입 필터**. `APPROVE_ENTRY` / `REJECT_ENTRY` / `WAIT` 와 confidence 를 낸다.
**자문(advisory)일 뿐이다** — 주문을 실행하지 않고, 청산에는 일절 관여하지 않는다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `candidate`, `indicators: dict`, `portfolio`, `risk_decision`, `market: MarketContext` |
| 출력 | `AIEntryDecision(decision, confidence_score, entry_quality, risk_level, reason, warnings, ...)` |
| 부수 | `AIDecisionLog` → `log_store.save()` + `engine.last_log` |

## 3. 주요 클래스/함수

- `AIDecisionEngine(provider, config=None, cache=None, log_store=None, monotonic=..., today_fn=...)` — `engine.py:79`
- `.decide(candidate, indicators, portfolio, risk_decision, market) -> AIEntryDecision` — `engine.py:95`
- `is_entry_permitted(ai_decision, risk_decision) -> bool` — `engine.py:277` ← **진입의 유일한 관문**
- `AIConfig` (frozen) — `engine.py:67`: `prompt_version="entry_filter_v1", temperature=0.0,
  timeout_s=8.0, min_confidence=60, use_cache=True, dry_run=False, max_calls_per_day=None`
- `FakeAIProvider(...)` — `providers.py:16` (기본값, 무료·결정론) / `OpenAIProvider(model_name="gpt-5.5-mini")` — `providers.py:37`

**Fail-closed**: timeout / rate limit / provider error / invalid JSON / schema mismatch /
low confidence / dry_run / 호출한도 초과 → **전부 `WAIT`**. AI 가 죽으면 매매하지 않는다.

## 4. 다른 모듈과의 관계

**`src/ai` 바깥의 어떤 모듈도 import 하지 않는다** (의도된 leaf — 브로커에 닿을 수 없다).
사용처: `src/pipeline/pipeline.py`, `src/live/paper_loop.py`(`MarketContext`), `src/runtime/metrics.py`,
`src/review/engine.py`, `src/system_review/engine.py`.

## 5. 절대 하면 안 되는 것

- ❌ **AI 가 청산을 판단하지 않는다.** 도크스트링: *"청산/손절/EOD flatten 은 AI 와 완전히 독립이다."*
  청산은 `src/exits/` 의 순수 결정론 코드가 만든다.
- ❌ **주문을 실행하지 않는다.** *"Broker/OrderManager 미접근."* `src/order`, `src/providers` import 금지.
- ❌ **AI 승인만으로 주문이 나가지 않는다.** *"AI 가 APPROVE 해도 RiskManager 가 block 이면 주문 불가."*
  반드시 `is_entry_permitted(ai_decision, risk_decision)` 를 통과해야 한다.
- ❌ **실패를 매수로 해석하지 않는다.** 모든 실패 경로는 `WAIT`(매매 금지)로 수렴한다.
- ❌ LLM 이 보고한 `model_name` 을 믿지 않는다 — 엔진이 `provider.model_name` 으로 덮어쓴다(`engine.py:194`).

시스템 프롬프트에도 동일 제약이 박혀 있다(`engine.py:50-54`):
*"You are NOT an order executor… You do NOT decide exits, stop-loss, take-profit, or EOD flatten…
your approval is advisory only."*

## 6. 사용 예시

```python
from src.ai.engine import AIConfig, AIDecisionEngine, is_entry_permitted
from src.ai.models import MarketContext
from src.ai.providers import FakeAIProvider

engine = AIDecisionEngine(FakeAIProvider(response={...}), AIConfig(min_confidence=60))
d = engine.decide(candidate, {"rsi": 55}, snapshot, risk_decision,
                  MarketContext(session="REGULAR"))

if is_entry_permitted(d, risk_decision):   # AI 승인 AND 리스크 통과
    order_manager.submit(...)
```

## 7. 테스트

`tests/test_ai_decision.py` (주 테스트).
`tests/test_exits.py` 는 **AI 가 청산에 영향을 못 준다**는 사실을 통합 검증한다.
