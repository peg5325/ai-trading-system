from __future__ import annotations

import hashlib
import json
from datetime import time

from src.freeze_paper.binding import CONTRACT_PATH, binding_from_contract, load_binding
from src.risk.models import RISK_REDUCING


def test_binding_hash_is_the_contract_file_sha256():
    binding = load_binding()
    assert binding.sha256 == hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
    assert binding.contract["version"] == "1.3"
    assert binding.quantity_decimals == 6
    assert binding.min_order_notional_usd == 1.0


def test_risk_and_exit_configs_come_from_implementation_binding():
    binding = load_binding()
    contract = binding.contract
    risk = contract["implementation_binding"]["risk_config"]
    cfg = binding.risk_config
    assert cfg.max_symbol_notional_pct == risk["max_symbol_notional_pct"]
    assert cfg.max_symbol_effective_pct == risk["max_symbol_effective_pct"]
    assert cfg.max_portfolio_effective_pct == risk["max_portfolio_effective_pct"]
    assert cfg.daily_loss_limit_pct == risk["daily_loss_limit_pct"]
    assert cfg.max_drawdown_pct == risk["max_drawdown_pct"]
    assert cfg.max_positions == risk["max_positions"]
    assert cfg.max_scale_ins == risk["max_scale_ins"]
    assert cfg.max_consecutive_losses == risk["max_consecutive_losses"]
    assert cfg.block_all_entries_in_eod_window is True
    assert cfg.reentry_cooldown.enabled is False
    exits = binding.exit_config
    assert exits.stop_loss_pct == contract["exits"]["stop_loss_pct_from_fill"]
    assert exits.take_profit_pct == contract["exits"]["take_profit_pct_from_fill"]
    assert exits.time_exit_minutes == contract["exits"]["time_exit_minutes"]
    assert exits.time_exit_bars is None
    assert exits.enable_all_symbols_eod_flatten is True
    assert exits.eod_flatten_minutes_before_close == 10
    assert binding.entry_time_et == time(9, 35)
    assert binding.risk_leverage_factor == 1
    assert binding.inverse_mapping == {"TQQQ": "SQQQ", "SOXL": "SOXS"}
    assert set(contract["implementation_binding"]["order_kinds"].values()) - {
        contract["implementation_binding"]["order_kinds"]["rule"]} <= RISK_REDUCING


def test_module_boundaries_hold():
    root = CONTRACT_PATH.parents[2] / "src"
    for path in (root / "premarket").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "src.freeze_paper" not in text and "src.order" not in text, path
    for path in (root / "freeze_paper").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("src.ai", "toss_broker", "src.providers", "import requests",
                          "urllib", "import random", "load_candles"):
            assert forbidden not in text, (path, forbidden)
        if path.name not in {"runner.py", "receipt.py", "evaluator.py"}:   # pure code
            for forbidden in ("datetime.now", ".write_", "subprocess", "write_immutable"):
                assert forbidden not in text, (path, forbidden)


def test_binding_follows_contract_values_not_constants():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["portfolio"]["max_total_effective_exposure_pct"] = 0.5
    contract["exits"]["time_exit_minutes"] = 30
    contract["implementation_binding"]["risk_config"]["daily_loss_limit_pct"] = 0.02
    binding = binding_from_contract(contract, "x" * 64)
    assert binding.max_exposure_pct == 0.5
    assert binding.exit_config.time_exit_minutes == 30
    assert binding.risk_config.daily_loss_limit_pct == 0.02
    assert binding.sha256 == "x" * 64
