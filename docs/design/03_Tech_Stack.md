# Recommended Tech Stack

## Core
- Python 3.11+
- FastAPI
- SQLAlchemy
- SQLite (MVP) -> PostgreSQL (Future)
- pandas / pandas-ta
- httpx
- OpenAI SDK
- Docker Compose
- pytest
- loguru

## Data
- Toss API (realtime price / order)
- KIS Open API (intraday history / backup realtime)
- Polygon (intraday history / reference indicators: SOXX, SMH, QQQ, VIX)

## Ops
- Scheduler (job 실패 재시도 + 실패 로그)
- Alerting: Telegram / Slack / Email (하나 이상)
- Config: strategy.yaml (rule trigger / parameters 외부화)
