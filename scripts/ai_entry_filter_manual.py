"""실제 OpenAI 로 entry filter 를 한 번 호출하는 수동 스크립트 (요구사항 16).

pytest 는 이 경로를 타지 않는다. 실제 호출은 비용이 발생하므로 수동 실행 전용.

준비:
  ./.venv/bin/pip install openai
  export OPENAI_API_KEY=sk-...
실행:
  ./.venv/bin/python scripts/ai_entry_filter_manual.py

기본은 dry_run=False. 비용이 걱정되면 아래 DRY_RUN=True 로 두면 호출하지 않는다.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ai.engine import AIConfig, AIDecisionEngine, is_entry_permitted  # noqa: E402
from src.ai.models import MarketContext  # noqa: E402
from src.ai.providers import OpenAIProvider  # noqa: E402
from src.risk.models import RiskDecision  # noqa: E402

DRY_RUN = False
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5-mini")

candidate = {"symbol": "SOXL", "score": 82.5, "candidate_type": "LONG_SETUP",
             "reasons": ["5m trend recovered above EMA20", "price reclaimed VWAP",
                         "volume above recent average"],
             "risk_flags": ["LEVERAGED_ETF", "EXTENDED_HOURS_SPREAD"]}
indicators = {"close": 194.8, "ema7": 194.1, "ema20": 193.2, "rsi14": 57.5,
              "vwap": 193.9, "bb_upper": 196.0, "bb_lower": 190.5,
              "stochrsi_k": 72.0, "stochrsi_d": 60.0}


class Snap:
    account_value = 100_000.0
    session = "REGULAR"
    is_eod_flatten_window = False
    consecutive_losses = 0
    daily_pnl_pct = 0.0
    drawdown_pct = 0.0
    total_effective_exposure = 15_000.0


def main() -> None:
    if not DRY_RUN and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY 가 필요합니다. (또는 DRY_RUN=True)")
    engine = AIDecisionEngine(
        OpenAIProvider(model_name=MODEL),
        AIConfig(temperature=0.0, dry_run=DRY_RUN, max_calls_per_day=50))
    risk = RiskDecision(True, [], 10.0, ["LEVERAGED_ETF"])
    decision = engine.decide(candidate, indicators, Snap(), risk, MarketContext("REGULAR"))
    print("=== AIEntryDecision ===")
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    print("\n=== ai_decision_log ===")
    print(json.dumps(engine.last_log.to_dict(), ensure_ascii=False, indent=2))
    print("\n최종 진입 허용(AI+Risk):", is_entry_permitted(decision, risk))


if __name__ == "__main__":
    main()
