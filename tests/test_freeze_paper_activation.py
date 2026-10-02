"""Activation stays OFF unless a valid first-session receipt exists."""
from __future__ import annotations

import os

import pytest

from src.freeze_paper.receipt import (
    CONFIG_RELATIVE,
    RECEIPT_RELATIVE,
    ROOT,
    load_receipt,
)
from src.premarket.contract import attachable_template, load_config, sha256_payload

# sha256_payload(PremarketConfig.to_dict()) of config/premarket_daily_brief.yaml with empty
# allowlists. 97faeb4 was 8cb5995d…; v1.3 (7a16f34) added news_title_relevance_required, so the
# 2026-10-02 pause (allowlists emptied again) pins the v1.3 value. Any other config change while
# the allowlists are empty must update this hash deliberately.
CURRENT_POLICY_CONFIG_SHA256 = "88c317eb9456fcce1e1a1ddcb3b9161cc9a96d7d238fe308ef69fa300ec29675"


def test_allowlists_are_empty_unless_a_valid_receipt_exists():
    config = load_config(ROOT / CONFIG_RELATIVE)
    approved = config.approved_strategy_templates + config.approved_short_strategy_templates
    if approved:
        assert (ROOT / RECEIPT_RELATIVE).is_file(), "allowlist filled without a receipt"
        # 영수증은 평가 시작일 SPY 종가 증거를 운영 DB와 대조한다. DB는 gitignore 운영 산출물이라
        # 새 worktree에는 없다. 그때는 skip하고, FREEZE_PAPER_REQUIRE_DB=1이면 skip 대신 fail한다.
        if (not (ROOT / "data" / "market_history.sqlite").is_file()
                and os.environ.get("FREEZE_PAPER_REQUIRE_DB") != "1"):
            pytest.skip("market_history.sqlite 없음(gitignore 운영 산출물): DB 증거 검증은 운영 체크아웃에서 실행")
        load_receipt(ROOT)            # raises ReceiptError when the receipt does not validate
    else:
        assert attachable_template(config) is None


def test_current_policy_config_hash_is_unchanged_while_off():
    config = load_config(ROOT / CONFIG_RELATIVE)
    if not (config.approved_strategy_templates or config.approved_short_strategy_templates):
        assert sha256_payload(config.to_dict()) == CURRENT_POLICY_CONFIG_SHA256
