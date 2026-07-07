"""운영 Configuration. runtime.yaml 로 분리.

nested yaml 을 읽어 평탄한 RuntimeConfig 로 매핑한다. yaml 은 lazy import(테스트는 from_dict).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RuntimeConfig:
    symbols: list[str] = field(default_factory=lambda: ["SOXL"])
    poll_interval_s: float = 20.0
    account_value: float = 100_000.0
    fetch_spacing_s: float = 0.0       # 종목간 시세 호출 간격(429 완화, 0=끔)
    max_calls_per_session: int | None = None
    # AI
    ai_provider: str = "fake"          # "fake" | "openai"
    ai_model: str = "gpt-5.5-mini"
    ai_dry_run: bool = False
    # review
    review_enabled: bool = False
    review_db: str = "data/reviews.sqlite"
    # stores
    journal_db: str = "data/journal.sqlite"
    candle_db: str = "data/market.sqlite"
    metrics_db: str = "data/metrics.sqlite"
    # paper broker
    slippage_bps: float = 5.0
    fee_bps: float = 2.0
    # logging / ops artifacts
    log_dir: str = "logs"
    log_max_bytes: int = 1_000_000
    log_backup_count: int = 3
    reports_dir: str = "reports"
    status_path: str = "status.json"

    @classmethod
    def from_dict(cls, d: dict) -> "RuntimeConfig":
        d = d or {}
        ai = d.get("ai", {})
        paper = d.get("paper_broker", {})
        journal = d.get("journal", {})
        candles = d.get("candles", {})
        review = d.get("review", {})
        logging_ = d.get("logging", {})
        return cls(
            symbols=list(d.get("symbols", ["SOXL"])),
            poll_interval_s=float(d.get("poll_interval_s", 20.0)),
            account_value=float(d.get("account_value", 100_000.0)),
            fetch_spacing_s=float(d.get("fetch_spacing_s", 0.0)),
            max_calls_per_session=d.get("max_calls_per_session"),
            ai_provider=ai.get("provider", "fake"),
            ai_model=ai.get("model", "gpt-5.5-mini"),
            ai_dry_run=bool(ai.get("dry_run", False)),
            review_enabled=bool(review.get("enabled", False)),
            review_db=review.get("db", "data/reviews.sqlite"),
            journal_db=journal.get("db", "data/journal.sqlite"),
            candle_db=candles.get("db", "data/market.sqlite"),
            metrics_db=d.get("metrics", {}).get("db", "data/metrics.sqlite"),
            slippage_bps=float(paper.get("slippage_bps", 5.0)),
            fee_bps=float(paper.get("fee_bps", 2.0)),
            log_dir=logging_.get("dir", "logs"),
            log_max_bytes=int(logging_.get("max_bytes", 1_000_000)),
            log_backup_count=int(logging_.get("backup_count", 3)),
            reports_dir=d.get("reports_dir", "reports"),
            status_path=d.get("status_path", "status.json"),
        )


def load_yaml(path: str | Path) -> RuntimeConfig:
    import yaml  # lazy: 운영에서만 필요
    with open(path, "r", encoding="utf-8") as f:
        return RuntimeConfig.from_dict(yaml.safe_load(f))
