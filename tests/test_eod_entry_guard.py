"""EOD 청산창 전종목 신규진입 차단 (opt-in, 기본 OFF) — 이슈 #42.

배경(자본 안전): US Phase 6 s001 에서 15:50 ET EOD 전종목 청산 직후 15:55 ET(창 내)
NVDA·QQQ 가 재진입해 **오버나잇 잔여 포지션 2개**가 남았다. 원인은 near-close 진입차단이
`leverage_factor > 1` 에만 걸려 있어서(risk_manager.py), `enable_all_symbols_eod_flatten`
으로 전 종목 청산을 켜도 **진입 차단은 전 종목으로 확장되지 않는 비대칭** 때문.

이 파일이 강제하는 계약:
1. 기본(False) → 기존 행동 보존(일반 종목은 EOD 창에서도 진입 가능 — 회귀 0)
2. True → leverage 무관 NEW_ENTRY/ADD 를 기존 사유 NEAR_CLOSE_NO_NEW_ENTRY 로 차단
3. 레버리지 near-close 차단은 옵션 True/False 모두 유지(기존 규칙 불변)
4. RISK_REDUCING(EXIT/STOP_LOSS/TAKE_PROFIT/TIME_EXIT/TREND_BREAK/EOD_FLATTEN)은 **절대 미차단**
5. YAML/RuntimeConfig 파싱 + config_hash 토글 민감성 + run_paper 배선
6. 통합: EOD 청산 후 다음 EOD-window 사이클에서 재진입 차단 → terminal flat
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.exits.generator import ExitConfig
from src.live.models import FakeMarketDataProvider, LoopConfig, ST_RAN
from src.live.paper_loop import PaperTradingLoop
from src.models import ET, Candle
from src.order.models import InMemoryPositionStore
from src.risk.models import (
    ADD,
    BUY,
    EOD_FLATTEN,
    EXIT,
    NEW_ENTRY,
    RISK_REDUCING,
    SELL,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_EXIT,
    OrderIntent,
    PortfolioSnapshot,
    Position,
)
from src.risk.risk_manager import (
    RC_NEAR_CLOSE_NO_ENTRY,
    RC_RISK_REDUCING_ALLOWED,
    RiskConfig,
    RiskManager,
)
from src.runtime.config import RuntimeConfig
from src.runtime.isolation import canonical_config_hash
from src.storage.candle_store import CandleStore

# 일반(비레버리지) 종목 = 결함의 실제 피해자(s001 의 NVDA/QQQ)
PLAIN = "NVDA"
LEVERED = "SOXL"


def snap(*, eod: bool, positions=None, **kw) -> PortfolioSnapshot:
    base = dict(account_value=100_000.0, positions=positions or {},
                is_eod_flatten_window=eod)
    base.update(kw)
    return PortfolioSnapshot(**base)


def entry(symbol=PLAIN, kind=NEW_ENTRY, lev=1, qty=10.0, price=100.0) -> OrderIntent:
    return OrderIntent(symbol, kind, BUY, qty, price, leverage_factor=lev)


def guard_rm(enabled: bool) -> RiskManager:
    return RiskManager(RiskConfig(block_all_entries_in_eod_window=enabled))


# ── 1) 기본값 False → 기존 행동 보존(회귀 0) ────────────────────────

def test_default_is_off():
    """옵션 기본값은 False — 기존 config/행동을 그대로 보존한다."""
    assert RiskConfig().block_all_entries_in_eod_window is False
    assert ExitConfig().block_all_entries_in_eod_window is False


def test_off_plain_symbol_entry_allowed_in_eod_window():
    """기본(False): 일반 종목은 EOD 창에서도 진입 허용 = s001 당시 동작(회귀 0 기준선)."""
    d = guard_rm(False).evaluate(entry(), snap(eod=True))
    assert d.allowed is True
    assert RC_NEAR_CLOSE_NO_ENTRY not in d.reason_codes


def test_off_plain_symbol_add_allowed_in_eod_window():
    pos = {PLAIN: Position(PLAIN, 5.0, 100.0, 100.0, 1)}
    d = guard_rm(False).evaluate(entry(kind=ADD), snap(eod=True, positions=pos))
    assert d.allowed is True
    assert RC_NEAR_CLOSE_NO_ENTRY not in d.reason_codes


# ── 2) True → 일반 종목 NEW_ENTRY/ADD 차단 ──────────────────────────

def test_on_blocks_plain_new_entry_in_eod_window():
    """★ 핵심: 옵션 ON 이면 leverage=1 종목도 EOD 창 신규진입 차단(기존 사유 재사용)."""
    d = guard_rm(True).evaluate(entry(), snap(eod=True))
    assert d.allowed is False
    assert RC_NEAR_CLOSE_NO_ENTRY in d.reason_codes
    assert d.adjusted_quantity == 0.0


def test_on_blocks_plain_add_in_eod_window():
    pos = {PLAIN: Position(PLAIN, 5.0, 100.0, 100.0, 1)}
    d = guard_rm(True).evaluate(entry(kind=ADD), snap(eod=True, positions=pos))
    assert d.allowed is False
    assert RC_NEAR_CLOSE_NO_ENTRY in d.reason_codes


def test_on_does_not_block_outside_eod_window():
    """옵션 ON 이어도 창 밖(is_eod_flatten_window=False)에서는 무영향 — 창 한정 규칙."""
    d = guard_rm(True).evaluate(entry(), snap(eod=False))
    assert d.allowed is True
    assert RC_NEAR_CLOSE_NO_ENTRY not in d.reason_codes


# ── 3) 레버리지 기존 규칙은 토글과 무관하게 유지 ────────────────────

def test_leveraged_blocked_regardless_of_toggle():
    """기존 레버리지 near-close 차단은 옵션 False 여도 그대로 유지된다."""
    for enabled in (False, True):
        d = guard_rm(enabled).evaluate(entry(symbol=LEVERED, lev=3), snap(eod=True))
        assert d.allowed is False, f"enabled={enabled}"
        assert RC_NEAR_CLOSE_NO_ENTRY in d.reason_codes


# ── 4) RISK_REDUCING 은 절대 미차단(자본 안전 불변식) ───────────────

def test_risk_reducing_never_blocked_in_eod_window():
    """청산 계열은 옵션/레버리지 무관 항상 허용 — 진입 가드가 탈출로를 막으면 안 된다."""
    pos = {PLAIN: Position(PLAIN, 10.0, 100.0, 100.0, 1)}
    for kind in sorted(RISK_REDUCING):
        for enabled in (False, True):
            it = OrderIntent(PLAIN, kind, SELL, 10.0, 100.0, leverage_factor=1)
            d = guard_rm(enabled).evaluate(it, snap(eod=True, positions=pos))
            assert d.allowed is True, f"{kind} enabled={enabled}"
            assert RC_NEAR_CLOSE_NO_ENTRY not in d.reason_codes
            assert d.reason_codes == [RC_RISK_REDUCING_ALLOWED]
            assert d.adjusted_quantity == 10.0


def test_eod_flatten_allowed_even_with_kill_switch_and_guard_on():
    """EOD_FLATTEN 은 옵션 ON + kill switch + 손실한도 동시 상황에서도 허용(기존 계약 유지)."""
    pos = {LEVERED: Position(LEVERED, 10.0, 194.0, 190.0, 3)}
    it = OrderIntent(LEVERED, EOD_FLATTEN, SELL, 10.0, 190.0, leverage_factor=3)
    d = guard_rm(True).evaluate(it, snap(eod=True, positions=pos, kill_switch=True,
                                         daily_pnl_pct=-0.9, consecutive_losses=99))
    assert d.allowed is True
    assert d.adjusted_quantity == 10.0


# ── 5) config 파싱 / hash / 배선 ────────────────────────────────────

def test_runtime_config_defaults_false_when_key_absent():
    """기존 config 는 이 키가 없다 → 기본 False(무영향)."""
    cfg = RuntimeConfig.from_dict({"exits": {"stop_loss_pct": 0.01}})
    assert cfg.exit_config.block_all_entries_in_eod_window is False


def test_runtime_config_parses_explicit_true():
    cfg = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": True}})
    assert cfg.exit_config.block_all_entries_in_eod_window is True


def test_runtime_config_parses_explicit_false():
    cfg = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": False}})
    assert cfg.exit_config.block_all_entries_in_eod_window is False


def test_config_hash_changes_with_toggle():
    """옵션은 '거래 행동' 계약 → 토글하면 config_hash 가 달라져야 한다(다른 계약=다른 세션).

    같은 값이면 hash 동일(결정론)."""
    off = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": False}})
    on = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": True}})
    assert canonical_config_hash(off) != canonical_config_hash(on)
    assert canonical_config_hash(on) == canonical_config_hash(
        RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": True}}))


def test_run_paper_wires_exit_config_flag_into_risk_config():
    """run_paper 배선을 **값으로** 검증 — exits.* → RiskConfig 전달이 실제로 일어나는가.

    RiskConfig 는 exit_config 를 통째로 받지 않으므로 이 배선이 빠지면 옵션이 조용히
    무력화된다. 소스 문자열 검사는 주석 처리된 배선도 통과시키므로(리뷰 지적) 실제
    RiskManager 를 만들어 값이 도착했는지 본다."""
    from scripts.run_paper import build_risk_manager

    on = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": True}})
    off = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": False}})
    assert build_risk_manager(on).cfg.block_all_entries_in_eod_window is True
    assert build_risk_manager(off).cfg.block_all_entries_in_eod_window is False
    # 키 자체가 없는 기존 config → 기본 false 가 도착(회귀 0)
    legacy = RuntimeConfig.from_dict({"exits": {"stop_loss_pct": 0.01}})
    assert build_risk_manager(legacy).cfg.block_all_entries_in_eod_window is False


def test_run_paper_wiring_actually_blocks_entry_end_to_end():
    """배선된 RiskManager 가 실제로 EOD 창 진입을 막는지 — 값 전달 + 동작을 한 번에."""
    from scripts.run_paper import build_risk_manager

    on = RuntimeConfig.from_dict({"exits": {"block_all_entries_in_eod_window": True}})
    d = build_risk_manager(on).evaluate(entry(), snap(eod=True))
    assert d.allowed is False and RC_NEAR_CLOSE_NO_ENTRY in d.reason_codes


def test_absent_key_hashes_same_as_explicit_false():
    """키 부재 == 명시 false — 같은 행동이면 같은 hash(계약 모호성 없음).

    hash 는 '거래 행동'의 지문이므로, 옵션을 안 쓴 config 와 명시적으로 끈 config 는
    같은 세션 계약이어야 한다."""
    absent = RuntimeConfig.from_dict({"exits": {"stop_loss_pct": 0.01}})
    explicit = RuntimeConfig.from_dict(
        {"exits": {"stop_loss_pct": 0.01, "block_all_entries_in_eod_window": False}})
    assert canonical_config_hash(absent) == canonical_config_hash(explicit)


def test_backtest_can_express_the_guard_contract():
    """백테스트도 신규 계약을 표현할 수 있다 — live/backtest 패리티(리뷰 지적 검증).

    BacktestEngine 은 BacktestConfig.risk(RiskConfig)를 그대로 쓰므로 별도 필드 추가 없이
    가드를 켤 수 있고, 기본은 false 라 기존 백테스트는 무영향이다. (ExitConfig 쪽 사본은
    ExitSignalGenerator 가 읽지 않는 진입 정책이라 backtest ExitConfig 매핑과 무관.)"""
    from src.backtest.engine import BacktestEngine
    from src.backtest.models import BacktestConfig

    assert BacktestEngine(BacktestConfig(
        risk=RiskConfig(block_all_entries_in_eod_window=True)
    )).risk.cfg.block_all_entries_in_eod_window is True
    # 기본 백테스트는 기존 행동 유지(회귀 0)
    assert BacktestEngine(BacktestConfig()).risk.cfg.block_all_entries_in_eod_window is False




# ── 6) 통합: EOD 청산 → 같은 창의 다음 사이클 재진입 차단 → terminal flat ──
#
# s001 재현 구조: 정규장 마감 16:00 ET · 창 15:50~16:00.
#   cycle 1 = 15:50 봉 → 보유 포지션 EOD_FLATTEN(청산은 항상 허용)
#   cycle 2 = 15:55 봉 → 재진입 시도. 가드 ON=차단(flat 유지) / OFF=재진입(오버나잇 발생)
# 5분봉은 다음 버킷의 봉이 도착해야 마감된다 → 15:50 봉을 얻으려면 1분봉이 15:55 까지,
# 15:55 봉을 얻으려면 16:00 까지 필요(resample 이 마지막 미완성 버킷을 버림).

_CLOSE = datetime(2026, 7, 6, 16, 0, tzinfo=ET)        # 정규장 마감
_LAST_1M_C1 = _CLOSE - timedelta(minutes=5)            # 15:55 → newest 5m = 15:50
_N_WARMUP = 130                                        # 지표 워밍업용 1분봉 수


def _1m_series(symbol, last, n=_N_WARMUP, base=100.0):
    """[last-(n-1)분 … last] 상승 추세 1분봉(screener LONG_SETUP 유도)."""
    out, prev = [], base
    start = last - timedelta(minutes=n - 1)
    for i in range(n):
        c = base + i * 0.05
        out.append(Candle.from_moment(symbol, "1m", start + timedelta(minutes=i),
                                      prev, max(prev, c) + 0.05, min(prev, c) - 0.05,
                                      c, 10_000 + i, "USD", True))
        prev = c
    return out


def _build_loop(tmp_path, name, *, guard_on, symbol=PLAIN):
    """실 pipeline(FakeAI APPROVE + PaperBroker + 실 RiskManager)로 EOD 창을 돌린다."""
    from src.ai.engine import AIConfig, AIDecisionEngine
    from src.ai.models import DEC_APPROVE
    from src.ai.providers import FakeAIProvider
    from src.backtest.execution import ExecutionModel
    from src.engines.indicator_engine import IndicatorEngine
    from src.journal.models import InMemoryJournalStore
    from src.order.manager import OrderManager
    from src.order.models import InMemoryOrderStore
    from src.order.paper_broker import PaperBrokerProvider
    from src.pipeline.pipeline import PipelineConfig, TradingPipeline
    from src.universe.screener import UniverseScreener

    ps = InMemoryPositionStore()
    om = OrderManager(PaperBrokerProvider(ExecutionModel(5, 2)), InMemoryOrderStore(), ps)
    ai = AIDecisionEngine(FakeAIProvider(response={
        "decision": DEC_APPROVE, "confidence_score": 80, "entry_quality": "GOOD",
        "risk_level": "LOW", "reason": ["ok"], "warnings": []}), AIConfig())
    rm = RiskManager(RiskConfig(block_all_entries_in_eod_window=guard_on))
    pipe = TradingPipeline(IndicatorEngine(), UniverseScreener(), ai, rm, om,
                           PipelineConfig(entry_notional_pct=0.2))
    market = FakeMarketDataProvider({symbol: _1m_series(symbol, _LAST_1M_C1)})
    journal = InMemoryJournalStore()
    loop_cfg = LoopConfig(
        universe=(symbol,), account_value=100_000.0, initial_capital=100_000.0,
        exit_config=ExitConfig(enable_all_symbols_eod_flatten=True,
                               eod_flatten_minutes_before_close=10,
                               block_all_entries_in_eod_window=guard_on))
    lp = PaperTradingLoop(market, CandleStore(tmp_path / f"{name}.sqlite"), pipe, ps,
                          loop_cfg, journal=journal, sleep=lambda s: None)
    return lp, journal, market


def _advance_to_1555(market, symbol=PLAIN):
    """1분봉 15:56~16:00 추가 → 다음 tick 의 newest 5m 봉 = 15:55(여전히 EOD 창)."""
    tail = _1m_series(symbol, _CLOSE, n=_N_WARMUP)      # 같은 추세를 16:00 까지 연장
    market.data[symbol] = tail


def _qty(lp, symbol=PLAIN):
    return lp.account.positions().get(symbol, {"quantity": 0.0})["quantity"]


def _cycle_actions(journal, cycle_id):
    return [(o.get("symbol"), o.get("action"), o.get("reason"))
            for o in journal.get_cycle(cycle_id).outcomes]


def _run_two_eod_cycles(tmp_path, name, *, guard_on):
    """창 안 2개 사이클(15:50 청산 → 15:55 재진입 시도)을 돌리고 결과를 돌려준다."""
    from src.pnl.models import FillEvent
    lp, journal, market = _build_loop(tmp_path, name, guard_on=guard_on)
    # 창 진입 시점에 이미 보유 중 — EOD 청산 대상
    lp.positions.apply_fill(PLAIN, "BUY", 10.0, 100.0, 0)
    lp.account.apply_fill(FillEvent(PLAIN, "BUY", 10.0, 100.0))
    assert _qty(lp) > 0

    rep1 = lp.tick()                                    # cycle 1 = 15:50 봉
    assert rep1.status == ST_RAN, rep1
    flat_after_exit = _qty(lp)

    _advance_to_1555(market)
    rep2 = lp.tick()                                    # cycle 2 = 15:55 봉(창 내)
    assert rep2.status == ST_RAN, rep2
    return lp, journal, rep1, rep2, flat_after_exit


def test_integration_eod_window_flattens_then_blocks_reentry_terminal_flat(tmp_path):
    """★ s001 재발 방지: 창 안에서 청산된 뒤 같은 창의 다음 사이클에 재진입하지 못하고
    세션말 flat 으로 끝난다(오버나잇 0)."""
    lp, journal, rep1, rep2, flat_after_exit = _run_two_eod_cycles(
        tmp_path, "guard_on", guard_on=True)

    # 1) 청산은 그대로 나간다 — 가드가 탈출로를 막지 않는다
    assert flat_after_exit == 0, "EOD 창에서 보유 포지션이 청산되어야 한다"
    assert any(a == "ORDERED" for _, a, _ in _cycle_actions(journal, rep1.cycle_id))

    # 2) 같은 창의 다음 사이클 — 재진입 차단 → terminal flat
    acts2 = _cycle_actions(journal, rep2.cycle_id)
    assert all(a != "ORDERED" for _, a, _ in acts2), acts2
    assert _qty(lp) == 0, "EOD 창 재진입이 차단되어 세션말 flat 이어야 한다(오버나잇 금지)"

    # 3) 차단 사유가 정확히 이 가드인지 — MAX_POSITIONS 등 다른 이유로 우연히 통과하는 착시 배제.
    #    AI 는 APPROVE 했는데 Risk 가 막았다는 것까지 확인(권한 순서 AI→Risk 유지).
    cyc2 = journal.get_cycle(rep2.cycle_id)
    codes = [c for o in cyc2.outcomes
             for c in (o.get("risk_decision") or {}).get("reason_codes", [])]
    assert codes == [RC_NEAR_CLOSE_NO_ENTRY], codes
    assert all((o.get("ai_decision") or {}).get("decision") == "APPROVE_ENTRY"
               for o in cyc2.outcomes), "AI 는 승인했으나 Risk 가 최종 차단해야 한다"
    # 두 사이클 모두 실제로 EOD 창이었는지(창 밖이라 조용히 통과한 게 아님)
    assert journal.get_cycle(rep1.cycle_id).market["is_eod_flatten_window"] is True
    assert cyc2.market["is_eod_flatten_window"] is True


def test_integration_guard_off_reproduces_s001_overnight_reentry(tmp_path):
    """대조군(회귀 0 + 결함 재현): 가드 OFF 면 청산 후 같은 창에서 재진입해 잔여 포지션이
    남는다 = s001 에서 실제로 일어난 일. 위 테스트가 '아무 일도 안 해서' 통과하는
    착시를 배제한다."""
    lp, journal, rep1, rep2, flat_after_exit = _run_two_eod_cycles(
        tmp_path, "guard_off", guard_on=False)

    assert flat_after_exit == 0                          # 청산은 동일하게 발생
    # 가드 OFF → 창 안 재진입이 체결되어 세션말 잔여 포지션(오버나잇) 발생
    assert any(a == "ORDERED" for _, a, _ in _cycle_actions(journal, rep2.cycle_id))
    assert _qty(lp) > 0, "가드 OFF 는 기존대로 EOD 창 재진입을 허용해야 한다(회귀 0)"
