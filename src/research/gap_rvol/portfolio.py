"""Fixed-capital sizing and simultaneous Gap+RVOL candidate competition."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from src.models import Candle
from src.research.gap_rvol.contract import require
from src.research.gap_rvol.signals import GapRvolSignal, rank_signals
from src.universe.screener import leverage_for

BINDING_CAPS = ("risk", "notional", "effective", "portfolio")


@dataclass(frozen=True)
class Position:
    symbol: str
    day: date
    spec_name: str
    entry_bar_ts_utc: datetime
    entry_minute: int
    entry_price: float
    qty: int
    leverage: int
    stop: float
    target: float
    one_r: float
    rvol: float
    gap: float
    rank: int
    binding_cap: str

    @property
    def effective_exposure(self) -> float:
        return self.entry_price * self.qty * self.leverage


@dataclass(frozen=True)
class SkippedEntry:
    symbol: str
    day: date
    signal_minute: int
    fill_minute: int
    reason: str
    binding_cap: str | None = None


def family_for(symbol: str, contract: Mapping[str, object]) -> str:
    for family, symbols in require(contract, "universe", "exposure_families").items():
        if symbol in symbols:
            return str(family)
    return symbol


def _size(
    *,
    symbol: str,
    entry: float,
    stop: float,
    current_effective: float,
    contract: Mapping[str, object],
) -> tuple[int, str] | None:
    one_r = entry - stop
    if one_r <= 0:
        return None
    equity = float(require(contract, "account_model", "initial_capital"))
    leverage = leverage_for(symbol)
    caps = {
        "risk": equity * float(require(
            contract, "risk", "risk_budget_pct_of_equity")) / one_r,
        "notional": equity * float(require(
            contract, "portfolio", "max_symbol_notional_pct")) / entry,
        "effective": equity * float(require(
            contract, "portfolio", "max_symbol_effective_pct")) / (entry * leverage),
        "portfolio": (
            equity * float(require(contract, "portfolio", "max_portfolio_effective_pct"))
            - current_effective
        ) / (entry * leverage),
    }
    binding = min(BINDING_CAPS, key=lambda key: (caps[key], BINDING_CAPS.index(key)))
    return math.floor(caps[binding]), binding


def allocate_entries(
    *,
    signals: Sequence[GapRvolSignal],
    bars_by_symbol: Mapping[str, Candle],
    positions: Mapping[str, Position],
    traded_symbols: set[str],
    contract: Mapping[str, object],
    fill_minute: int,
) -> tuple[tuple[Position, ...], tuple[SkippedEntry, ...]]:
    accepted: list[Position] = []
    skipped: list[SkippedEntry] = []
    max_positions = int(require(contract, "portfolio", "max_positions"))
    for rank, signal in enumerate(rank_signals(signals), start=1):
        if signal.symbol in traded_symbols:
            continue
        bar = bars_by_symbol.get(signal.symbol)
        if bar is None or signal.fill_minute != fill_minute:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute,
                signal.fill_minute, "signal_expired"))
            traded_symbols.add(signal.symbol)
            continue
        if len(positions) + len(accepted) >= max_positions:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute,
                fill_minute, "slot_cap"))
            traded_symbols.add(signal.symbol)
            continue
        occupied = {
            family_for(position.symbol, contract)
            for position in (*positions.values(), *accepted)
        }
        if family_for(signal.symbol, contract) in occupied:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute,
                fill_minute, "family_cap"))
            traded_symbols.add(signal.symbol)
            continue
        entry = float(bar.open)
        sized = _size(
            symbol=signal.symbol,
            entry=entry,
            stop=signal.pullback_low,
            current_effective=sum(
                position.effective_exposure
                for position in (*positions.values(), *accepted)
            ),
            contract=contract,
        )
        if sized is None:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute,
                fill_minute, "one_r_not_positive"))
            traded_symbols.add(signal.symbol)
            continue
        qty, binding = sized
        if qty < 1:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute,
                fill_minute, "qty_lt_1", binding))
            traded_symbols.add(signal.symbol)
            continue
        one_r = entry - signal.pullback_low
        accepted.append(Position(
            symbol=signal.symbol,
            day=signal.day,
            spec_name=signal.spec_name,
            entry_bar_ts_utc=bar.ts_utc,
            entry_minute=fill_minute,
            entry_price=entry,
            qty=qty,
            leverage=leverage_for(signal.symbol),
            stop=signal.pullback_low,
            target=entry + signal.target_r_multiple * one_r,
            one_r=one_r,
            rvol=signal.rvol,
            gap=signal.gap,
            rank=rank,
            binding_cap=binding,
        ))
        traded_symbols.add(signal.symbol)
    return tuple(accepted), tuple(skipped)
