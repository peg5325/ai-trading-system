"""Phase 6 EOD 진입가드 config 계약 (이슈 #42).

`config/runtime_us_phase6_eodguard_s001.yaml` 이:
1. EOD 진입가드를 켜고,
2. 그 외 매매계약은 baseline(`runtime_us_phase6_baseline.yaml`)과 **정확히 동일**하며,
3. baseline 과 다른 config_hash · fresh namespace 를 갖는(= 별도 cohort) 지 강제한다.

핵심 의도: 이 세션의 데이터가 baseline s001 과 **섞이지 않게** 하는 것. 계약이 다르면
cohort 도 달라야 한다(승격 판정은 동일 계약 세션끼리만 누적).
"""
from __future__ import annotations

import os

from src.runtime.config import load_yaml
from src.runtime.isolation import canonical_config_hash

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_PATH = os.path.join(_ROOT, "config", "runtime_us_phase6_eodguard_s001.yaml")
BASELINE_PATH = os.path.join(_ROOT, "config", "runtime_us_phase6_baseline.yaml")


def test_eodguard_option_is_on():
    """이 config 의 존재 이유 — 청산창 전종목 진입차단 ON."""
    assert load_yaml(GUARD_PATH).exit_config.block_all_entries_in_eod_window is True


def test_baseline_untouched_still_off():
    """기존 baseline config 는 건드리지 않는다(역사적 재현성) — 여전히 옵션 OFF."""
    assert load_yaml(BASELINE_PATH).exit_config.block_all_entries_in_eod_window is False


def test_strategy_params_identical_to_baseline():
    """전략 파라미터/유니버스/자본은 baseline 과 동일 — 가드 외 변수를 섞지 않는다."""
    g, b = load_yaml(GUARD_PATH), load_yaml(BASELINE_PATH)
    assert g.symbols == b.symbols
    assert g.initial_capital == b.initial_capital
    assert g.max_positions == b.max_positions
    assert g.entry_notional_pct == b.entry_notional_pct
    assert g.ai_provider == b.ai_provider == "fake"       # FakeAI(실AI 금지)
    assert (g.slippage_bps, g.fee_bps) == (b.slippage_bps, b.fee_bps)
    for f in ("stop_loss_pct", "take_profit_pct", "time_exit_minutes",
              "enable_all_symbols_eod_flatten", "leverage_eod_flatten",
              "eod_flatten_minutes_before_close"):
        assert getattr(g.exit_config, f) == getattr(b.exit_config, f), f


def test_only_the_guard_flag_differs_from_baseline():
    """exit_config 차이가 **정확히 가드 한 필드** 뿐임을 강제(의도치 않은 동반 변경 차단)."""
    from dataclasses import asdict
    g, b = load_yaml(GUARD_PATH), load_yaml(BASELINE_PATH)
    gd, bd = asdict(g.exit_config), asdict(b.exit_config)
    diff = {k for k in gd if gd[k] != bd[k]}
    assert diff == {"block_all_entries_in_eod_window"}, diff


def test_observation_on_cooldown_off():
    """MC+Shadow ON · Cooldown OFF — Phase 6 baseline 정책 유지(행동 변경은 가드 하나뿐)."""
    g = load_yaml(GUARD_PATH)
    assert g.market_context.enabled is True
    assert g.market_context.benchmark_symbol == "QQQ"
    assert g.shadow.enabled is True
    assert g.reentry_cooldown.enabled is False           # Regime Gate/Cooldown 은 이번 범위 밖


def test_fresh_identity_and_namespace_separate_from_s001():
    """fresh namespace — 기존 us-phase6-s001 을 재사용하지 않는다(startup guard 통과 조건)."""
    g, b = load_yaml(GUARD_PATH), load_yaml(BASELINE_PATH)
    assert g.start_mode == "FRESH"
    assert (g.market, g.currency) == ("US", "USD")
    assert g.session_id == "us-phase6-eodguard-s001"
    assert g.session_id != b.session_id
    dbs = (g.journal_db, g.candle_db, g.metrics_db, g.review_db, g.system_review_db)
    for db in dbs:
        assert "us_phase6_eodguard" in db
    # 이전 s001 산출물과 경로가 겹치면 FRESH guard 가 거부 → 반드시 분리
    assert not set(dbs) & {b.journal_db, b.candle_db, b.metrics_db,
                           b.review_db, b.system_review_db}
    assert g.log_dir != b.log_dir and g.status_path != b.status_path


def test_config_hash_differs_from_baseline():
    """계약이 다르면 hash 도 달라야 한다 — 두 세션이 같은 cohort 로 합쳐지지 않는 근거."""
    assert canonical_config_hash(load_yaml(GUARD_PATH)) != \
        canonical_config_hash(load_yaml(BASELINE_PATH))
