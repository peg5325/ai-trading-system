"""Types shared by provider adapters without importing the collector."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CollectionContext:
    trading_day: date
    as_of: str
    symbols: tuple[str, ...]
