"""ORB 포트폴리오 진입·sizing — 사전등록 §7·§8.3."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from src.models import Candle
from src.research.orb.contract import _require
from src.research.orb.signals import OrbSignal, rank_signals
from src.universe.screener import leverage_for

SKIPPED_REASONS: tuple[str, ...] = (
    "one_r_not_positive",
    "qty_lt_1",
    "family_cap",
    "slot_cap",
    "signal_expired",
)
BINDING_CAPS: tuple[str, ...] = ("risk", "notional", "effective", "portfolio")


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


@dataclass(frozen=True)
class Sizing:
    qty: int
    binding_cap: str
    caps: Mapping[str, float]


def family_for(symbol: str, contract: Mapping[str, object]) -> str:
    families = _require(contract, "universe", "exposure_families")
    for family, symbols in families.items():
        if symbol in symbols:
            return str(family)
    return symbol


def size_position(
    *,
    entry_price: float,
    stop: float,
    symbol: str,
    current_effective: float,
    contract: Mapping[str, object],
) -> Sizing | None:
    one_r = entry_price - stop
    if one_r <= 0:
        return None
    equity = float(_require(contract, "account_model", "initial_capital"))
    risk_pct = float(_require(contract, "risk", "risk_budget_pct_of_equity"))
    max_notional = float(_require(contract, "portfolio", "max_symbol_notional_pct"))
    max_effective = float(_require(contract, "portfolio", "max_symbol_effective_pct"))
    max_portfolio = float(_require(contract, "portfolio", "max_portfolio_effective_pct"))
    lev = leverage_for(symbol)
    caps = {
        "risk": equity * risk_pct / one_r,
        "notional": equity * max_notional / entry_price,
        "effective": equity * max_effective / (entry_price * lev),
        "portfolio": (equity * max_portfolio - current_effective) / (entry_price * lev),
    }
    binding = min(BINDING_CAPS, key=lambda key: (caps[key], BINDING_CAPS.index(key)))
    return Sizing(qty=math.floor(caps[binding]), binding_cap=binding, caps=caps)


def allocate_entries(
    *,
    signals: Sequence[OrbSignal],
    bars_by_symbol: Mapping[str, Candle],
    positions: Mapping[str, Position],
    traded_symbols: set[str],
    contract: Mapping[str, object],
    fill_minute: int,
    close_minute: int,
) -> tuple[tuple[Position, ...], tuple[SkippedEntry, ...]]:
    """동시 신호를 순위대로 cap sizing한다.

    `bars_by_symbol`의 값은 open·ts_utc 속성을 가진 결정봉이다. 같은 봉에서
    먼저 채택한 후보의 실질노출과 family도 다음 후보 판정에 즉시 반영한다.
    """
    accepted: list[Position] = []
    skipped: list[SkippedEntry] = []
    max_positions = int(_require(contract, "portfolio", "max_positions"))
    max_entry_before = int(_require(contract, "signal", "max_entry_minutes_before_close"))
    for rank, signal in enumerate(rank_signals(list(signals)), start=1):
        if signal.symbol in traded_symbols:
            continue
        bar = bars_by_symbol.get(signal.symbol)
        if bar is None or signal.fill_minute != fill_minute:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, signal.fill_minute,
                "signal_expired"))
            traded_symbols.add(signal.symbol)
            continue
        if fill_minute > close_minute - max_entry_before:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, signal.fill_minute,
                "signal_expired"))
            traded_symbols.add(signal.symbol)
            continue
        if len(positions) + len(accepted) >= max_positions:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, fill_minute, "slot_cap"))
            traded_symbols.add(signal.symbol)
            continue
        occupied_families = {
            family_for(p.symbol, contract) for p in (*positions.values(), *accepted)
        }
        if family_for(signal.symbol, contract) in occupied_families:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, fill_minute, "family_cap"))
            traded_symbols.add(signal.symbol)
            continue
        entry_price = float(bar.open)
        sizing = size_position(
            entry_price=entry_price,
            stop=signal.or_low,
            symbol=signal.symbol,
            current_effective=sum(
                p.effective_exposure for p in (*positions.values(), *accepted)),
            contract=contract,
        )
        if sizing is None:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, fill_minute,
                "one_r_not_positive"))
            traded_symbols.add(signal.symbol)
            continue
        if sizing.qty < 1:
            skipped.append(SkippedEntry(
                signal.symbol, signal.day, signal.signal_minute, fill_minute,
                "qty_lt_1", sizing.binding_cap))
            traded_symbols.add(signal.symbol)
            continue
        accepted.append(Position(
            symbol=signal.symbol,
            day=signal.day,
            spec_name=signal.spec_name,
            entry_bar_ts_utc=bar.ts_utc,
            entry_minute=fill_minute,
            entry_price=entry_price,
            qty=sizing.qty,
            leverage=leverage_for(signal.symbol),
            stop=signal.or_low,
            target=entry_price + signal.target_r_multiple * (entry_price - signal.or_low),
            one_r=entry_price - signal.or_low,
            rvol=signal.rvol,
            gap=signal.gap,
            rank=rank,
            binding_cap=sizing.binding_cap,
        ))
        traded_symbols.add(signal.symbol)
    return tuple(accepted), tuple(skipped)
