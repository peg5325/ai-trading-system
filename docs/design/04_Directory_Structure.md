# Directory Structure

app/
  providers/          # broker / marketdata / ai (fetch + order only)
  universe/           # universe_screener, ranking, filters
  engines/            # indicator_engine, rule_engine, ai_decision_engine, backtest_engine
  risk/               # risk_manager (per-symbol), portfolio_risk, position_monitor, kill_switch
  services/           # reconciliation, market_calendar, alerting
  models/             # SQLAlchemy models
  schemas/            # pydantic schemas (AI I/O, order, candle, candidate)
  repository/         # DB access
  jobs/               # scheduler jobs (universe refresh, 5min loop, EOD flatten, reconcile)
  backtest/           # execution model, portfolio-level validation, metrics
config/
  universe.yaml       # sector/universe boundary + filters
  strategy.yaml       # rule trigger + parameters
tests/
docker-compose.yml
.env

## Notes
- UniverseScreener 는 universe/ 에. 종목 선별은 여기서만.
- IndicatorEngine 은 engines/ 에 단일 구현, backtest·live·screener 가 공유.
- Exit logic 과 portfolio risk 는 risk/ 에. AI 와 물리적으로 분리.
