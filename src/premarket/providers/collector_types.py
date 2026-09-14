"""Types shared by provider adapters without importing the collector."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class CollectionContext:
    trading_day: date
    as_of: str
    symbols: tuple[str, ...]
    stage: str = "PRIMARY"
    schedule: dict[str, Any] | None = None
    policy_config_sha256: str | None = None
