"""Cost-aware Performance (마찰비용 관측) 테스트.

계좌/종목별 cost ratio, gross=0/gross<0 unstable, WARNING/SEVERE 분류,
Session #003 데이터 기준 cost_ratio ≈ 0.44 재현, 리포트 렌더링.
매매 로직에 무관한 read-only 관측 기능이다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.pnl.cost import (OK, SEVERE, UNSTABLE, WARNING, cost_analysis)
from src.pnl.models import RealizedTrade
from src.runtime.report import _cost_section, generate_markdown


def _rt(symbol: str, gross: float, commission: float, slippage: float,
        *, qty: float = 10.0, entry_price: float = 100.0, exit_price: float = 101.0,
        holding: float | None = 30.0) -> RealizedTrade:
    """테스트용 RealizedTrade. net = gross - commission - slippage 불변식 유지."""
    net = gross - commission - slippage
    return RealizedTrade(
        symbol=symbol, quantity=qty, entry_time=None, exit_time=None,
        entry_bar=None, exit_bar=None, entry_price=entry_price, exit_price=exit_price,
        entry_ref=entry_price, exit_ref=exit_price, gross_pnl=gross,
        commission=commission, slippage=slippage, net_pnl=net, realized_pnl=net,
        realized_return_pct=0.0, holding_minutes=holding, holding_bars=None,
        entry_reason=(), exit_reason=("TAKE_PROFIT",))


# ---- 계좌 전체 cost ratio ----
def test_account_cost_ratio():
    ca = cost_analysis([_rt("A", gross=100.0, commission=10.0, slippage=20.0)])
    assert ca.total_cost == 30.0
    assert abs(ca.cost_ratio - 0.30) < 1e-9        # 30 / 100
    assert ca.ratio_stable is True
    assert abs(ca.cost_impact_net - 30.0 / 70.0) < 1e-9   # cost / |net|


# ---- 종목별 cost ratio + 정렬(심각한 종목 먼저) ----
def test_per_symbol_cost_ratio_and_sort():
    ca = cost_analysis([
        _rt("LOW", gross=1000.0, commission=10.0, slippage=10.0),    # ratio 0.02 OK
        _rt("HIGH", gross=100.0, commission=30.0, slippage=30.0),    # ratio 0.60 SEVERE
        _rt("MID", gross=100.0, commission=20.0, slippage=15.0),     # ratio 0.35 WARNING
    ])
    per = {s.symbol: s for s in ca.per_symbol}
    assert abs(per["LOW"].cost_ratio - 0.02) < 1e-9 and per["LOW"].severity == OK
    assert abs(per["HIGH"].cost_ratio - 0.60) < 1e-9 and per["HIGH"].severity == SEVERE
    assert abs(per["MID"].cost_ratio - 0.35) < 1e-9 and per["MID"].severity == WARNING
    # 정렬: 비율 높은 순 → HIGH, MID, LOW
    assert [s.symbol for s in ca.per_symbol] == ["HIGH", "MID", "LOW"]
    # 종목별 turnover = 진입+청산 거래대금
    assert per["HIGH"].trade_count == 1


# ---- gross_pnl = 0 → unstable (division-by-zero 방지) ----
def test_gross_zero_unstable():
    ca = cost_analysis([_rt("Z", gross=0.0, commission=5.0, slippage=5.0)])
    assert ca.cost_ratio is None
    assert ca.ratio_stable is False
    assert ca.severity == UNSTABLE
    assert ca.per_symbol[0].severity == UNSTABLE


# ---- gross_pnl < 0 → unstable ----
def test_gross_negative_unstable():
    ca = cost_analysis([_rt("N", gross=-50.0, commission=3.0, slippage=4.0)])
    assert ca.cost_ratio is None
    assert ca.severity == UNSTABLE
    # 손실 구간에서도 크래시 없이 total_cost 는 계산됨
    assert ca.total_cost == 7.0


# ---- WARNING / SEVERE 경계 분류 ----
def test_warning_severe_thresholds():
    warn = cost_analysis([_rt("W", gross=100.0, commission=15.0, slippage=15.0)])  # 0.30
    assert warn.severity == WARNING                       # >= 0.30
    below = cost_analysis([_rt("B", gross=100.0, commission=10.0, slippage=19.0)])  # 0.29
    assert below.severity == OK                           # < 0.30
    sev = cost_analysis([_rt("S", gross=100.0, commission=25.0, slippage=25.0)])   # 0.50
    assert sev.severity == SEVERE                         # >= 0.50


# ---- Session #003 재현: cost_ratio ≈ 0.44 (gross 23.23, comm 2.90, slip 7.24) ----
def test_session_003_cost_ratio_reproduction():
    # #003 계좌 합계: Gross 23.23 / Commission 2.90 / Slippage 7.24 → cost 10.14
    trades = [
        _rt("NVDA", gross=13.00, commission=1.50, slippage=4.00),
        _rt("SOXL", gross=10.23, commission=1.40, slippage=3.24),
    ]
    ca = cost_analysis(trades)
    assert abs(ca.gross_pnl - 23.23) < 1e-9
    assert abs(ca.total_cost - 10.14) < 1e-9
    assert round(ca.cost_ratio, 2) == 0.44               # 10.14 / 23.23 = 0.4365
    assert ca.severity == WARNING                         # 0.44 ∈ [0.30, 0.50)


# ---- 리포트 렌더링: Cost Analysis 섹션 + 종목별 표 ----
def test_cost_section_rendering():
    ca = cost_analysis([
        _rt("SOXL", gross=100.0, commission=30.0, slippage=31.0),   # 0.61 SEVERE
        _rt("QQQ", gross=0.0, commission=2.0, slippage=1.0),        # unstable
    ])
    out = "\n".join(_cost_section(ca.to_dict()))
    assert "## Cost Analysis" in out
    assert "### Cost Drag by Symbol" in out
    assert "SOXL" in out and "SEVERE" in out
    assert "unstable" in out                              # gross=0 종목
    assert "WARNING ≥ 0.30" in out and "SEVERE ≥ 0.50" in out


def test_cost_section_empty_when_no_cost():
    assert _cost_section(None) == []
    assert _cost_section({}) == []


# ---- generate_markdown 통합: realized 에 cost 실으면 섹션 출력 ----
class _FakeMetrics:
    iterations = cycles_run = skipped = 0
    candidates = ai_approve = ai_wait = ai_reject = risk_block = 0
    orders = buys = sells = exits = eods = 0
    exceptions = api_errors = pipeline_errors = sqlite_locks = 0

    def avg_loop_ms(self):
        return 0.0

    def avg_ai_ms(self):
        return 0.0


class _FakeCfg:
    poll_interval_s = 20
    symbols = ["SOXL", "QQQ"]


def test_generate_markdown_includes_cost_section():
    ca = cost_analysis([_rt("SOXL", gross=100.0, commission=30.0, slippage=31.0)])
    realized = {"trades": 1, "wins": 1, "losses": 0, "breakeven": 0,
                "win_rate": 1.0, "total_net_pnl": 39.0, "total_gross_pnl": 100.0,
                "total_commission": 30.0, "total_slippage": 31.0, "expectancy": 39.0,
                "profit_factor": None, "avg_holding_minutes": 30.0,
                "avg_holding_bars": None, "open_positions": 0, "open_quantity": 0.0,
                "exit_reasons": {"TAKE_PROFIT": 1}, "cost": ca.to_dict()}
    md = generate_markdown(_FakeMetrics(), {"status": "OK"}, _FakeCfg(),
                           report_date="20260708", run_duration_s=100.0,
                           realized=realized, performance=None)
    assert "## Cost Analysis" in md
    assert "Cost Drag by Symbol" in md
    assert "SEVERE" in md
