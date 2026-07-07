"""RiskManager 테스트: 포지션 한도, 레버리지 노출, kill switch, 손절 허용,
신규 진입 차단, EOD flatten 허용, 일일 손실 한도, 연속 손실 제한, 결정론."""
from __future__ import annotations

from src.risk.models import (
    ADD,
    BUY,
    EOD_FLATTEN,
    EXIT,
    NEW_ENTRY,
    SELL,
    OrderIntent,
    PortfolioSnapshot,
    Position,
)
from src.risk.risk_manager import (
    RC_CONSEC_LOSS,
    RC_DAILY_LOSS,
    RC_DRAWDOWN,
    RC_EXT_HOURS_RISK,
    RC_INVALID_INTENT,
    RC_KILL_SWITCH,
    RC_LEVERAGED_ETF_LIMIT,
    RC_MAX_PORTFOLIO_EFFECTIVE,
    RC_MAX_SCALE_INS,
    RC_MAX_SYMBOL_NOTIONAL,
    RC_NEAR_CLOSE_NO_ENTRY,
    RC_NO_EXISTING_POSITION,
    RC_POSITION_ALREADY_OPEN,
    RC_QTY_REDUCED,
    RiskConfig,
    RiskManager,
)

RM = RiskManager()


def snap(**kw) -> PortfolioSnapshot:
    base = dict(account_value=100_000.0, positions={})
    base.update(kw)
    return PortfolioSnapshot(**base)


def soxl_entry(qty=10.0, price=194.0, kind=NEW_ENTRY, flags=None) -> OrderIntent:
    return OrderIntent("SOXL", kind, BUY, qty, price, leverage_factor=3,
                       risk_flags=flags or [])


# ---- 정상 진입 ----
def test_new_entry_within_limits_allowed():
    d = RM.evaluate(soxl_entry(qty=10), snap())
    assert d.allowed
    assert d.adjusted_quantity == 10.0
    assert "LEVERAGED_ETF" in d.risk_flags


# ---- 레버리지 실질 노출 (예시 시나리오: 하드 차단) ----
def test_leverage_effective_and_portfolio_block():
    # 계좌 10k, 기존 포트폴리오 실질노출 6000(>포트 상한 5000). SOXL 신규 → 두 한도 동시 초과
    positions = {"TQQQ": Position("TQQQ", 10.0, 200.0, 200.0, leverage_factor=3)}  # eff=6000
    s = snap(account_value=10_000.0, positions=positions)
    d = RM.evaluate(soxl_entry(qty=10, flags=["HIGH_VOLATILITY"]), s)
    assert not d.allowed
    assert d.adjusted_quantity == 0
    assert RC_LEVERAGED_ETF_LIMIT in d.reason_codes         # 단일 종목 실질 노출
    assert RC_MAX_PORTFOLIO_EFFECTIVE in d.reason_codes      # 포트폴리오 실질 노출
    assert set(d.risk_flags) == {"LEVERAGED_ETF", "HIGH_VOLATILITY"}


# ---- 단일 종목 명목 한도 → 수량 감량 ----
def test_symbol_notional_limit_reduces_quantity():
    # 비레버리지, 명목 상한 20000, 요청 40000 → 절반으로 감량
    intent = OrderIntent("AAPL", NEW_ENTRY, BUY, 200.0, 200.0, leverage_factor=1)
    d = RM.evaluate(intent, snap())
    assert d.allowed
    assert d.adjusted_quantity == 100.0                      # 20000/200
    assert RC_QTY_REDUCED in d.reason_codes
    assert RC_MAX_SYMBOL_NOTIONAL in d.reason_codes


# ---- kill switch: 진입 차단 ----
def test_kill_switch_blocks_new_entry():
    d = RM.evaluate(soxl_entry(), snap(kill_switch=True))
    assert not d.allowed
    assert RC_KILL_SWITCH in d.reason_codes


# ---- kill switch 여도 청산/EOD flatten 허용 ----
def test_kill_switch_allows_exit_and_flatten():
    pos = {"SOXL": Position("SOXL", 10.0, 194.0, 194.0, leverage_factor=3)}
    s = snap(kill_switch=True, positions=pos, daily_pnl_pct=-0.05)
    ex = RM.evaluate(OrderIntent("SOXL", EXIT, SELL, 10.0, 195.0, 3), s)
    assert ex.allowed and ex.adjusted_quantity == 10.0
    fl = RM.evaluate(OrderIntent("SOXL", EOD_FLATTEN, SELL, 10.0, 195.0, 3), s)
    assert fl.allowed and fl.adjusted_quantity == 10.0


# ---- 손절(EXIT)은 손실한도/킬스위치와 무관하게 허용 ----
def test_stop_loss_allowed_despite_all_limits():
    pos = {"SOXL": Position("SOXL", 10.0, 194.0, 190.0, leverage_factor=3)}
    s = snap(kill_switch=True, positions=pos, daily_pnl_pct=-0.10,
             drawdown_pct=-0.20, consecutive_losses=9)
    d = RM.evaluate(OrderIntent("SOXL", EXIT, SELL, 10.0, 190.0, 3), s)
    assert d.allowed
    assert d.adjusted_quantity == 10.0


# ---- 일일 손실 한도 ----
def test_daily_loss_limit_blocks_entry():
    d = RM.evaluate(soxl_entry(), snap(daily_pnl_pct=-0.03))
    assert not d.allowed
    assert RC_DAILY_LOSS in d.reason_codes


# ---- 최대 낙폭 ----
def test_drawdown_blocks_entry():
    d = RM.evaluate(soxl_entry(), snap(drawdown_pct=-0.10))
    assert not d.allowed
    assert RC_DRAWDOWN in d.reason_codes


# ---- 연속 손실 ----
def test_consecutive_loss_limit_blocks_entry():
    d = RM.evaluate(soxl_entry(), snap(consecutive_losses=3))
    assert not d.allowed
    assert RC_CONSEC_LOSS in d.reason_codes


# ---- EOD flatten 허용 + 레버리지 신규진입은 flatten 창에서 차단 ----
def test_eod_flatten_allowed_and_leveraged_entry_blocked_in_window():
    pos = {"SOXL": Position("SOXL", 10.0, 194.0, 193.0, leverage_factor=3)}
    s = snap(positions=pos, is_eod_flatten_window=True)
    fl = RM.evaluate(OrderIntent("SOXL", EOD_FLATTEN, SELL, 10.0, 193.0, 3), s)
    assert fl.allowed and fl.adjusted_quantity == 10.0
    # 레버리지 신규 진입은 flatten 창에서 금지
    ne = RM.evaluate(soxl_entry(qty=1), s)
    assert not ne.allowed
    assert RC_NEAR_CLOSE_NO_ENTRY in ne.reason_codes
    # 일반주는 flatten 창에서도 진입 허용(오버나잇 가능)
    plain = RM.evaluate(OrderIntent("AAPL", NEW_ENTRY, BUY, 10.0, 200.0, 1), snap(is_eod_flatten_window=True))
    assert plain.allowed


# ---- 연장시간 리스크 주문 차단 ----
def test_extended_hours_risky_entry_blocked():
    s = snap(session="PRE")
    d = RM.evaluate(soxl_entry(flags=["EXTENDED_HOURS_SPREAD"]), s)
    assert not d.allowed
    assert RC_EXT_HOURS_RISK in d.reason_codes
    # 연장시간이라도 청산은 허용
    pos = {"SOXL": Position("SOXL", 5.0, 194.0, 193.0, leverage_factor=3)}
    ex = RM.evaluate(OrderIntent("SOXL", EXIT, SELL, 5.0, 193.0, 3),
                     snap(session="PRE", positions=pos))
    assert ex.allowed


def test_extended_hours_flag_blocks_regardless_of_snapshot_session():
    # 혼합 유니버스: 포트폴리오 대표 세션이 REGULAR(KR발)이어도
    # US 심야 종목의 EXTENDED_HOURS_SPREAD 플래그면 차단되어야 한다.
    d = RM.evaluate(soxl_entry(flags=["EXTENDED_HOURS_SPREAD"]), snap(session="REGULAR"))
    assert not d.allowed and RC_EXT_HOURS_RISK in d.reason_codes


def test_extended_hours_reduce_mode():
    rm = RiskManager(RiskConfig(block_extended_hours_risky=False))
    s = snap(session="POST")
    d = rm.evaluate(soxl_entry(qty=10, flags=["EXTENDED_HOURS_SPREAD"]), s)
    assert d.allowed
    assert d.adjusted_quantity == 5.0                        # 0.5배 감량
    assert RC_EXT_HOURS_RISK in d.reason_codes


# ---- 추가 진입(분할매수) 횟수 제한 ----
def test_scale_in_limit():
    # 최초 진입 + 2회 추가 = scale_in_count 3 → 추가 차단
    pos3 = {"SOXL": Position("SOXL", 10.0, 194.0, 194.0, 3, scale_in_count=3)}
    d = RM.evaluate(soxl_entry(qty=1, kind=ADD), snap(positions=pos3))
    assert not d.allowed
    assert RC_MAX_SCALE_INS in d.reason_codes
    # 아직 1회 → 추가 허용
    pos1 = {"SOXL": Position("SOXL", 10.0, 194.0, 194.0, 3, scale_in_count=1)}
    ok = RM.evaluate(soxl_entry(qty=1, kind=ADD), snap(positions=pos1))
    assert ok.allowed


def test_add_without_position_blocked():
    d = RM.evaluate(soxl_entry(qty=1, kind=ADD), snap())
    assert not d.allowed
    assert RC_NO_EXISTING_POSITION in d.reason_codes


def test_new_entry_when_already_open_blocked():
    pos = {"SOXL": Position("SOXL", 10.0, 194.0, 194.0, 3)}
    d = RM.evaluate(soxl_entry(qty=1, kind=NEW_ENTRY), snap(positions=pos))
    assert not d.allowed
    assert RC_POSITION_ALREADY_OPEN in d.reason_codes


# ---- 무효 주문 ----
def test_invalid_intent():
    d = RM.evaluate(soxl_entry(qty=0), snap())
    assert not d.allowed
    assert RC_INVALID_INTENT in d.reason_codes


# ---- 출력 구조 ----
def test_output_structure():
    d = RM.evaluate(soxl_entry(flags=["HIGH_VOLATILITY"]), snap()).to_dict()
    assert set(d) == {"allowed", "reason_codes", "adjusted_quantity", "risk_flags"}
    assert isinstance(d["reason_codes"], list) and isinstance(d["risk_flags"], list)


# ---- 결정론 ----
def test_deterministic_same_inputs_same_output():
    s = snap(positions={"SOXL": Position("SOXL", 20.0, 194.0, 194.0, 3)})
    i = soxl_entry(qty=5)
    a, b = RM.evaluate(i, s).to_dict(), RM.evaluate(i, s).to_dict()
    assert a == b
