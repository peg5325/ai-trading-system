"""Activation stays OFF unless a valid first-session receipt exists."""
from __future__ import annotations

from src.freeze_paper.receipt import (
    CONFIG_RELATIVE,
    RECEIPT_RELATIVE,
    ROOT,
    load_receipt,
)
from src.premarket.contract import attachable_template, load_config, sha256_payload

# sha256_payload(PremarketConfig.to_dict()) of config/premarket_daily_brief.yaml at 97faeb4.
# The template plumbing adds no config field, so this policy_config hash (embedded in every
# FREEZE snapshot) must stay identical while the allowlists are empty.
CURRENT_POLICY_CONFIG_SHA256 = "8cb5995d9418e2fdc97b85f519557d5b66892f9845578ef4750117b8cf6f1784"


def test_allowlists_are_empty_unless_a_valid_receipt_exists():
    config = load_config(ROOT / CONFIG_RELATIVE)
    approved = config.approved_strategy_templates + config.approved_short_strategy_templates
    if approved:
        assert (ROOT / RECEIPT_RELATIVE).is_file(), "allowlist filled without a receipt"
        load_receipt(ROOT)            # raises ReceiptError when the receipt does not validate
    else:
        assert attachable_template(config) is None


def test_current_policy_config_hash_is_unchanged_while_off():
    config = load_config(ROOT / CONFIG_RELATIVE)
    if not (config.approved_strategy_templates or config.approved_short_strategy_templates):
        assert sha256_payload(config.to_dict()) == CURRENT_POLICY_CONFIG_SHA256
