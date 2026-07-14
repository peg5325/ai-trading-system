"""Re-entry Cooldown v1 (hard, opt-in) 테스트.

정책(timeframe 기반 unit: trading_bars|elapsed_minutes) · RiskManager(NEW_ENTRY 차단 ·
**청산/RISK_REDUCING 절대 무차단** · 기본off 회귀 · conditional 예약 경고) ·
restart/resume/session boundary · #006R missed-opportunity 반례 · Backtest A/B.
과매매/whipsaw 안전장치이며 기본 비활성이다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.live.models import LoopConfig
from src.live.paper_loop import PaperTradingLoop
from src.models import Candle, exchange_tz
from src.pnl.models import FillEvent
from src.portfolio.account import PortfolioAccount
from src.risk.models import (BUY, EOD_FLATTEN, NEW_ENTRY, SELL, STOP_LOSS,
                             TAKE_PROFIT, OrderIntent, PortfolioSnapshot, Position)
from src.risk.reentry_cooldown import ReentryCooldownConfig, ReentryCooldownPolicy
from src.risk.risk_manager import (RC_REENTRY_COOLDOWN, RC_RISK_REDUCING_ALLOWED,
                                   RiskConfig, RiskManager)

T0 = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)   # 10:00 ET REGULAR


def _cfg(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("timeframe_minutes", 5.0)
    return ReentryCooldownConfig(**kw)


# ================= 1. 순수 정책 (timeframe 기반 unit) =================
def test_duration_for_reason_mapping():
    c = _cfg(stop_loss=6, take_profit=3)
    assert c.duration_for(STOP_LOSS) == 6
    assert c.duration_for(TAKE_PROFIT) == 3
    assert c.duration_for(EOD_FLATTEN) == 0
    assert c.duration_for(None) == 0


def test_unit_trading_bars():
    # unit=trading_bars, timeframe 5분, stop_loss=6봉 → 30분 창
    p = ReentryCooldownPolicy(_cfg(unit="trading_bars", timeframe_minutes=5, stop_loss=6))
    assert p.blocked(T0 + timedelta(minutes=25), T0, STOP_LOSS) is True    # 5봉 < 6
    assert p.blocked(T0 + timedelta(minutes=30), T0, STOP_LOSS) is False   # 6봉 == 6
    assert p.blocked(T0 + timedelta(minutes=100), T0, STOP_LOSS) is False


def test_unit_elapsed_minutes():
    # unit=elapsed_minutes, stop_loss=30분
    p = ReentryCooldownPolicy(_cfg(unit="elapsed_minutes", stop_loss=30))
    assert p.blocked(T0 + timedelta(minutes=25), T0, STOP_LOSS) is True
    assert p.blocked(T0 + timedelta(minutes=30), T0, STOP_LOSS) is False


def test_no_timestamp_floor_bucket():
    # 절대버킷(timestamp//300)이 아니라 delta 기반 — 봉 경계와 무관하게 경과로 판정.
    p = ReentryCooldownPolicy(_cfg(unit="trading_bars", timeframe_minutes=5, stop_loss=2))
    # 정확히 5분 경과(경계 비정렬 시각) → 1봉 < 2 → 차단
    off = datetime(2026, 7, 8, 14, 2, 30, tzinfo=timezone.utc)
    assert p.blocked(off + timedelta(minutes=5), off, STOP_LOSS) is True
    assert p.blocked(off + timedelta(minutes=10), off, STOP_LOSS) is False  # 2봉


def test_stop_loss_blocks_take_profit_separate():
    p = ReentryCooldownPolicy(_cfg(unit="trading_bars", stop_loss=6))
    assert p.blocked(T0 + timedelta(minutes=5), T0, STOP_LOSS) is True
    assert p.blocked(T0 + timedelta(minutes=5), T0, TAKE_PROFIT) is False   # take_profit=0
    p2 = ReentryCooldownPolicy(_cfg(unit="trading_bars", take_profit=4))
    assert p2.blocked(T0 + timedelta(minutes=5), T0, TAKE_PROFIT) is True
    assert p2.blocked(T0 + timedelta(minutes=5), T0, STOP_LOSS) is False


def test_disabled_never_blocks():
    p = ReentryCooldownPolicy(ReentryCooldownConfig(enabled=False, stop_loss=6))
    assert p.blocked(T0 + timedelta(minutes=1), T0, STOP_LOSS) is False
    p2 = ReentryCooldownPolicy(_cfg(stop_loss=6))
    assert p2.blocked(T0, None, None) is False        # 상태 없음(첫 진입)


def test_future_exit_defensive():
    # last_exit_time 이 now 보다 미래(비정상) → 차단 안 함(음수 경과 방어)
    p = ReentryCooldownPolicy(_cfg(unit="trading_bars", stop_loss=6))
    assert p.blocked(T0, T0 + timedelta(minutes=5), STOP_LOSS) is False


def test_reserved_mode_scope_warns():
    with pytest.warns(UserWarning):
        ReentryCooldownConfig(enabled=True, mode="conditional", stop_loss=6)
    with pytest.warns(UserWarning):
        ReentryCooldownConfig(enabled=True, scope="market", stop_loss=6)


def test_conditional_signature_reserved():
    p = ReentryCooldownPolicy(_cfg(unit="trading_bars", stop_loss=6))   # mode=hard(기본)
    feats = {"close": 101, "vwap": 100}
    assert p.blocked(T0 + timedelta(minutes=5), T0, STOP_LOSS, features=feats) is True


# ================= 2. RiskManager =================
def _entry(symbol="X"):
    return OrderIntent(symbol, NEW_ENTRY, BUY, quantity=1.0, price=100.0)


def _snap(now_time=None, last_exit_time=None, last_exit_reason=None, positions=None):
    return PortfolioSnapshot(
        account_value=10_000.0, positions=positions or {}, cash=10_000.0,
        now_time=now_time,
        last_exit_time={"X": last_exit_time} if last_exit_time else {},
        last_exit_reason={"X": last_exit_reason} if last_exit_reason else {})


def _rm(**cd):
    cd.setdefault("enabled", True)
    return RiskManager(RiskConfig(reentry_cooldown=ReentryCooldownConfig(**cd)))


def test_risk_blocks_new_entry_during_cooldown():
    rm = _rm(unit="trading_bars", timeframe_minutes=5, stop_loss=6)
    d = rm.evaluate(_entry(), _snap(T0 + timedelta(minutes=15), T0, STOP_LOSS))
    assert d.allowed is False and RC_REENTRY_COOLDOWN in d.reason_codes


def test_risk_allows_after_cooldown_expires():
    rm = _rm(unit="trading_bars", timeframe_minutes=5, stop_loss=6)
    d = rm.evaluate(_entry(), _snap(T0 + timedelta(minutes=30), T0, STOP_LOSS))
    assert d.allowed is True and RC_REENTRY_COOLDOWN not in d.reason_codes


def test_cooldown_never_blocks_exit():
    # 쿨다운 활성 + 포지션 보유에서 STOP_LOSS(청산/RISK_REDUCING) intent → 항상 허용
    rm = _rm(unit="trading_bars", stop_loss=6)
    pos = {"X": Position("X", 5.0, 100.0, 99.0, 1)}
    exit_intent = OrderIntent("X", STOP_LOSS, SELL, quantity=5.0, price=99.0)
    snap = _snap(T0 + timedelta(minutes=5), T0, STOP_LOSS, positions=pos)
    d = rm.evaluate(exit_intent, snap)
    assert d.allowed is True
    assert RC_RISK_REDUCING_ALLOWED in d.reason_codes
    assert RC_REENTRY_COOLDOWN not in d.reason_codes


def test_default_off_no_behavior_change():
    rm = RiskManager()          # 기본 = cooldown 비활성
    d = rm.evaluate(_entry(), _snap(T0 + timedelta(minutes=1), T0, STOP_LOSS))
    assert d.allowed is True and RC_REENTRY_COOLDOWN not in d.reason_codes


# ================= 3. restart / resume / session boundary =================
def _loop(cd_cfg):
    """최소 mock 으로 PaperTradingLoop 구성(cooldown 상태 로직만 검증)."""
    return PaperTradingLoop(None, None, None, None,
                            LoopConfig(reentry_cooldown=cd_cfg))


def _account_with_stop(sym="X"):
    acct = PortfolioAccount.from_fills(10_000.0, [
        FillEvent(sym, BUY, 10, 100.0, timestamp=T0 - timedelta(minutes=30)),
        FillEvent(sym, SELL, 10, 99.0, timestamp=T0, reason=(STOP_LOSS,))])
    return acct


def test_restart_rebuilds_last_exit_from_account():
    loop = _loop(_cfg(unit="trading_bars", stop_loss=6))
    loop.account = _account_with_stop("X")
    loop._last_exit_time.clear(); loop._last_exit_reason.clear()
    loop._rebuild_last_exit()                        # 재시작 복원
    assert loop._last_exit_time["X"] == T0
    assert loop._last_exit_reason["X"] == STOP_LOSS
    # 복원된 상태로 쿨다운 판정(15분 후 재진입 → 차단)
    pol = ReentryCooldownPolicy(loop.cfg.reentry_cooldown)
    assert pol.blocked(T0 + timedelta(minutes=15), loop._last_exit_time["X"],
                       loop._last_exit_reason["X"]) is True


def test_resume_first_session_establish_preserves_state():
    # resume: 복원된 상태가 '첫' 세션 확립(prev=None)에서 지워지지 않아야 함
    loop = _loop(_cfg(unit="trading_bars", stop_loss=6))
    loop._last_exit_time["X"] = T0
    loop._last_exit_reason["X"] = STOP_LOSS
    loop._maybe_reset_session(T0.astimezone(exchange_tz("US")))   # prev None → 확립만
    assert loop._last_exit_time.get("X") == T0                    # 보존


def test_session_boundary_resets_when_enabled():
    loop = _loop(_cfg(unit="trading_bars", stop_loss=6, reset_on_new_session=True))
    loop._maybe_reset_session(T0.astimezone(exchange_tz("US")))   # day1 확립
    loop._last_exit_time["X"] = T0; loop._last_exit_reason["X"] = STOP_LOSS
    next_day = (T0 + timedelta(days=1)).astimezone(exchange_tz("US"))
    loop._maybe_reset_session(next_day)                          # 실제 전환 → 초기화
    assert loop._last_exit_time == {} and loop._last_exit_reason == {}


def test_session_boundary_no_reset_when_disabled():
    loop = _loop(_cfg(unit="trading_bars", stop_loss=6, reset_on_new_session=False))
    loop._maybe_reset_session(T0.astimezone(exchange_tz("US")))
    loop._last_exit_time["X"] = T0; loop._last_exit_reason["X"] = STOP_LOSS
    next_day = (T0 + timedelta(days=1)).astimezone(exchange_tz("US"))
    loop._maybe_reset_session(next_day)
    assert loop._last_exit_time.get("X") == T0                   # 유지


# ================= 4. Backtest A/B & #006R 반례 =================
def _candle(sym, ts, o, h, l, c, market="US"):
    return Candle(symbol=sym, interval="5m", ts_utc=ts,
                  ts_exchange=ts.astimezone(exchange_tz(market)), session="REGULAR",
                  open=o, high=h, low=l, close=c, volume=10000.0,
                  currency="USD", adjusted=True, market=market)


def _series(prices, sym="X"):
    """close 시퀀스 → 같은 US 정규장 하루의 5분봉 리스트."""
    out = []
    prev = prices[0]
    for i, c in enumerate(prices):
        ts = T0 + timedelta(minutes=5 * i)
        out.append(_candle(sym, ts, prev, max(prev, c) * 1.0005, min(prev, c) * 0.999, c))
        prev = c
    return out


class _ForcedEntry:
    def signal(self, symbol, indicators, t, leverage_factor):
        return ["forced"]


def _bt_trades(candles, cooldown_bars):
    from src.backtest.engine import BacktestEngine
    from src.backtest.models import BacktestConfig
    risk = RiskConfig(daily_loss_limit_pct=1.0, max_drawdown_pct=1.0,
                      max_consecutive_losses=10_000, max_positions=10,
                      reentry_cooldown=ReentryCooldownConfig(
                          enabled=cooldown_bars > 0, unit="trading_bars",
                          timeframe_minutes=5.0, stop_loss=cooldown_bars))
    cfg = BacktestConfig(initial_capital=100_000.0, entry_notional_pct=0.05,
                         stop_loss_pct=0.01, take_profit_pct=None,
                         exit_on_trend_break=False, leverage_eod_flatten=False,
                         allow_overnight_non_leverage=True, min_trades=1, risk=risk)
    return BacktestEngine(cfg).run(candles, "X", 1, _ForcedEntry()).trades


def test_backtest_ab_cooldown_reduces_trades():
    downtrend = _series([100.0 * (0.994 ** i) for i in range(48)])
    off = _bt_trades(downtrend, 0)
    on = _bt_trades(downtrend, 8)
    assert len(off) > 0
    assert len(on) < len(off)          # cooldown on → 재진입 억제 → 회전수↓


def test_006R_missed_opportunity_counterexample():
    # #006R: 손절 직후 급반등하는 구간 — cooldown 이 '수익' 재진입을 막는 반례.
    # V자: 하락(손절 유발) → 급반등. off 는 반등을 타고 on 은 쿨다운으로 놓친다.
    from scripts.compare_cooldown import _missed_opportunity
    down = [100.0 - 1.2 * i for i in range(6)]           # 100 → 94 (손절)
    up = [down[-1] + 1.5 * i for i in range(1, 25)]      # 급반등
    vshape = _series(down + up)
    off = _bt_trades(vshape, 0)
    on = _bt_trades(vshape, 8)
    mo = _missed_opportunity(off, on)
    # cooldown 이 차단한 진입 중 '이익'이었던 것이 존재 → 안전장치의 기회비용(반례)
    assert mo["missed_profit"] > 0
    assert mo["blocked_entries"] > 0


def test_compare_outputs_all_metrics():
    from scripts.compare_cooldown import compare
    r = compare(_series([100.0 * (0.994 ** i) for i in range(48)]), "X", cooldown_bars=8)
    for run in ("off", "on"):
        for k in ("trades", "total_return_pct", "win_rate", "profit_factor",
                  "max_drawdown_pct", "total_cost_est", "cost_ratio_est", "net_pnl"):
            assert k in r[run], f"{run} missing {k}"
    for k in ("blocked_entries", "missed_profit", "avoided_loss", "net_missed"):
        assert k in r["missed_opportunity"]
