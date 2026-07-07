# Database (MVP)

다종목이므로 대부분 테이블에 symbol 차원이 들어간다.

## Tables
- candle              # symbol, ts_utc, ts_exchange, interval, o,h,l,c,v, adjusted
- indicator           # symbol 별 계산 결과 캐시
- universe_snapshot   # 스크리너 결과: symbol, rank, leverage_factor, liquidity, refreshed_at
- ai_decision_log     # symbol, decision, confidence, reason[], risk_level, entry_quality, input snapshot
- order_log           # symbol, status(NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED), fees, tax
- position            # symbol 별 내부 상태 (broker 와 대조 대상)
- reconciliation_log  # 내부 vs 브로커 잔고 대조 결과, mismatch 기록
- risk_event_log      # per-symbol / portfolio 트리거 발동 기록
- portfolio_snapshot  # 총 실질 노출, 동시 포지션 수, 일일 손익, drawdown
- backtest_result     # 포트폴리오 단위 metrics (Sharpe, MDD, win rate, profit factor ...)

## Notes
- candle.adjusted: split/corporate-action 보정 여부 명시 (레버리지 ETF 역분할 대응).
- position source of truth = broker. 불일치 시 재동기화 + 신규 주문 중단.
- portfolio_snapshot 으로 포트폴리오 리스크 판정.
