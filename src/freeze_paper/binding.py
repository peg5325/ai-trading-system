"""Load the locked Paper contract and derive component configs from it.

Every number used by the engine comes from the contract JSON; nothing is
hard-coded here.  The contract file SHA-256 is carried in engine state so a
run can be tied to the exact contract bytes it enforced.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

from src.exits.generator import ExitConfig
from src.risk.risk_manager import RiskConfig

CONTRACT_PATH = (Path(__file__).resolve().parents[2] / "docs" / "research"
                 / "PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json")


@dataclass(frozen=True)
class ContractBinding:
    contract: dict[str, Any]
    sha256: str
    risk_config: RiskConfig
    exit_config: ExitConfig
    strategy_template_id: str
    max_candidates: int
    initial_equity: float
    max_exposure_pct: float
    quantity_decimals: int
    min_order_notional_usd: float
    entry_time_et: time
    slippage_bps: float
    commission_bps: float
    sec_fee_pct_of_sell_notional: float
    sec_fee_minimum_usd: float
    official_increment_pct_points_per_side: float
    stress_increment_pct_points_per_side: float
    eod_minutes_before_close: int
    risk_leverage_factor: int
    inverse_mapping: dict[str, str]
    index_leveraged_etfs: tuple[str, ...]


def load_binding(path: Path | None = None) -> ContractBinding:
    raw = (path or CONTRACT_PATH).read_bytes()
    return binding_from_contract(json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest())


def binding_from_contract(contract: dict[str, Any], sha256: str) -> ContractBinding:
    impl = contract["implementation_binding"]
    risk = impl["risk_config"]
    exits = contract["exits"]
    costs = contract["execution_costs"]
    portfolio = contract["portfolio"]
    direction = contract["direction_scope"]
    hour, minute = (int(part) for part in contract["entry"]["entry_time_et"].split(":"))
    eod_minutes = int(impl["eod_flatten_minutes_before_close"])
    risk_config = RiskConfig(
        max_symbol_notional_pct=float(risk["max_symbol_notional_pct"]),
        max_symbol_effective_pct=float(risk["max_symbol_effective_pct"]),
        max_portfolio_effective_pct=float(risk["max_portfolio_effective_pct"]),
        daily_loss_limit_pct=float(risk["daily_loss_limit_pct"]),
        max_drawdown_pct=float(risk["max_drawdown_pct"]),
        max_consecutive_losses=int(risk["max_consecutive_losses"]),
        max_scale_ins=int(risk["max_scale_ins"]),
        max_positions=int(risk["max_positions"]),
        block_all_entries_in_eod_window=bool(risk["block_all_entries_in_eod_window"]),
    )
    exit_config = ExitConfig(
        stop_loss_pct=float(exits["stop_loss_pct_from_fill"]),
        take_profit_pct=float(exits["take_profit_pct_from_fill"]),
        time_exit_minutes=float(exits["time_exit_minutes"]),
        enable_all_symbols_eod_flatten=not bool(exits["overnight"]),
        eod_flatten_minutes_before_close=eod_minutes,
        block_all_entries_in_eod_window=bool(risk["block_all_entries_in_eod_window"]),
    )
    return ContractBinding(
        contract=contract,
        sha256=sha256,
        risk_config=risk_config,
        exit_config=exit_config,
        strategy_template_id=str(contract["strategy_template_id"]),
        max_candidates=int(contract["decision_input"]["max_candidates"]),
        initial_equity=float(contract["account"]["initial_equity"]),
        max_exposure_pct=float(portfolio["max_total_effective_exposure_pct"]),
        quantity_decimals=int(portfolio["quantity_decimals"]),
        min_order_notional_usd=float(portfolio["minimum_order_notional_usd"]),
        entry_time_et=time(hour, minute),
        slippage_bps=float(costs["paper_slippage_bps_per_side"]),
        commission_bps=float(costs["paper_commission_bps_per_side"]),
        sec_fee_pct_of_sell_notional=float(costs["paper_sec_fee_pct_of_sell_notional"]),
        sec_fee_minimum_usd=float(costs["paper_sec_fee_minimum_usd"]),
        official_increment_pct_points_per_side=float(
            costs["official_incremental_pct_points_per_side"]),
        stress_increment_pct_points_per_side=float(
            costs["stress_incremental_pct_points_per_side"]),
        eod_minutes_before_close=eod_minutes,
        risk_leverage_factor=int(impl["risk_leverage_factor"]),
        inverse_mapping=dict(direction["inverse_mapping"]),
        index_leveraged_etfs=tuple(direction["index_3x_etfs_traded_as_long_watch"]),
    )
