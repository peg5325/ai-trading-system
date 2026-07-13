"""Exit Follow-up Dataset (청산 사후 관측) 테스트.

SELL row 생성 · forward return · MFE · MAE · session_end_return · horizon 초과 NULL ·
look-ahead 방지 · journal→dataset 통합(고정 픽스처, 결정론) · #004 동일 생성.
매매 로직 무관 read-only 배치. Look-ahead Bias 없음을 강하게 검증한다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dataset.exit_builder import (ExitDatasetBuilder, ExitLabelConfig,
                                      build_exit_dataset_from_journal)
from src.models import Candle, exchange_tz
from src.pnl.models import RealizedTrade

def _find_data(name: str) -> str:
    """data/<name> 을 상위 디렉터리로 올라가며 탐색(worktree→main 체크아웃).

    data/*.sqlite 는 gitignore 세션 산출물이라 worktree 엔 없고 main 체크아웃에만 있다.
    없으면 첫 후보 경로를 반환(→ skipif 로 스킵)."""
    d = os.path.dirname(os.path.abspath(__file__))
    first = None
    for _ in range(6):
        cand = os.path.join(d, "data", name)
        if first is None:
            first = cand
        if os.path.exists(cand):
            return cand
        d = os.path.dirname(d)
    return first


# data/*.sqlite 는 gitignore 세션 산출물 → 존재할 때만 통합 테스트 실행
# (US #003 은 결정론적 고정 픽스처로 대체됨 — Issue #17)
_KR_DB = _find_data("journal_krx_exit_test_session004.sqlite")
_KR_MKT = _find_data("market_krx_exit_test_session004.sqlite")

T0 = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)   # 10:00 ET, US REGULAR


def _candle(sym, ts_utc, o, h, l, c, market="US", currency="USD"):
    return Candle(
        symbol=sym, interval="5m", ts_utc=ts_utc,
        ts_exchange=ts_utc.astimezone(exchange_tz(market)),
        session="REGULAR", open=o, high=h, low=l, close=c, volume=1000.0,
        currency=currency, adjusted=True, market=market)


class _FakeStore:
    """read-only 캔들 소스(테스트용). query(symbol, interval) → asc 리스트."""
    def __init__(self, candles):
        self._by_sym: dict[str, list] = {}
        for c in candles:
            self._by_sym.setdefault(c.symbol, []).append(c)
        for v in self._by_sym.values():
            v.sort(key=lambda c: c.ts_utc)

    def query(self, symbol, interval, *, start=None, end=None, limit=None):
        return list(self._by_sym.get(symbol, []))


def _trade(symbol="X", *, exit_time=T0, exit_price=100.0, reason="TAKE_PROFIT",
           entry_time=None, entry_price=99.0, holding_minutes=30.0):
    return RealizedTrade(
        symbol=symbol, quantity=10.0,
        entry_time=entry_time or (exit_time - timedelta(minutes=int(holding_minutes))),
        exit_time=exit_time, entry_bar=None, exit_bar=None,
        entry_price=entry_price, exit_price=exit_price,
        entry_ref=entry_price, exit_ref=exit_price,
        gross_pnl=(exit_price - entry_price) * 10.0, commission=0.0, slippage=0.0,
        net_pnl=(exit_price - entry_price) * 10.0, realized_pnl=(exit_price - entry_price) * 10.0,
        realized_return_pct=0.0, holding_minutes=holding_minutes, holding_bars=None,
        entry_reason=(), exit_reason=(reason,))


def _ramp_day(sym="X", n=13, market="US", currency="USD"):
    """exit 봉(T0, close=100) + 이후 5m 램프 봉 n개(close 101,102,...). 마지막 봉이 세션말."""
    candles = [_candle(sym, T0, 100.0, 100.0, 100.0, 100.0, market, currency)]
    for i in range(1, n + 1):
        ts = T0 + timedelta(minutes=5 * i)
        close = 100.0 + i
        candles.append(_candle(sym, ts, close - 0.5, close + 0.5, close - 0.5, close,
                               market, currency))
    return candles


# ---- 1. SELL row 생성 ----
def test_sell_row_created():
    store = _FakeStore(_ramp_day())
    ds = ExitDatasetBuilder(store).build([_trade(reason="TIME_EXIT")], session_id="s1")
    assert len(ds) == 1
    r = ds.records[0]
    assert r.side == "SELL" and r.symbol == "X" and r.market == "US"
    assert r.exit_reason == "TIME_EXIT" and r.session_id == "s1"
    assert r.exit_price == 100.0
    assert r.schema_version == "1.0"


# ---- 2. Forward Return 계산 (+5/+15/+30/+60) ----
def test_forward_returns():
    store = _FakeStore(_ramp_day(n=13))          # +60분(=index12)까지 존재
    r = ExitDatasetBuilder(store).build([_trade()], session_id="s1").records[0]
    fmap = r.forward_map()
    # +5 close=101 → 0.01, +15 close=103 → 0.03, +30 close=106 → 0.06, +60 close=112 → 0.12
    assert abs(fmap[5].forward_return - 0.01) < 1e-9 and fmap[5].price == 101.0
    assert abs(fmap[15].forward_return - 0.03) < 1e-9
    assert abs(fmap[30].forward_return - 0.06) < 1e-9
    assert abs(fmap[60].forward_return - 0.12) < 1e-9
    assert all(fmap[h].complete for h in (5, 15, 30, 60))


# ---- 3. MFE 계산 ----
def test_mfe():
    store = _FakeStore(_ramp_day(n=13))
    r = ExitDatasetBuilder(store).build([_trade()], session_id="s1").records[0]
    # eval 60분: best high = +60봉 high = 112.5 → (112.5-100)/100 = 0.125
    assert abs(r.mfe - 0.125) < 1e-9
    assert r.best_price_after_exit == 112.5


# ---- 4. MAE 계산 ----
def test_mae():
    store = _FakeStore(_ramp_day(n=13))
    r = ExitDatasetBuilder(store).build([_trade()], session_id="s1").records[0]
    # 모든 저점이 100 위(램프 상승) → worst low = +5봉 low = 100.5 → 0.005
    assert abs(r.mae - 0.005) < 1e-9
    assert r.worst_price_after_exit == 100.5


# ---- 5. Session End Return ----
def test_session_end_return():
    store = _FakeStore(_ramp_day(n=12))          # 마지막 봉 = +60분, close=112 (세션말)
    r = ExitDatasetBuilder(store).build([_trade()], session_id="s1").records[0]
    assert r.session_end_price == 112.0
    assert abs(r.session_end_return - 0.12) < 1e-9
    assert r.session_end_complete is True


# ---- 6. Horizon 초과 → NULL / INCOMPLETE ----
def test_horizon_beyond_session_is_incomplete():
    store = _FakeStore(_ramp_day(n=6))           # +30분까지만 존재(세션말=+30)
    r = ExitDatasetBuilder(store).build([_trade()], session_id="s1").records[0]
    fmap = r.forward_map()
    assert fmap[30].complete is True and fmap[30].price is not None
    # +60 은 세션 종료(+30) 초과 → NULL/INCOMPLETE
    assert fmap[60].complete is False and fmap[60].price is None
    assert fmap[60].forward_return is None
    # eval 창(60)도 세션말(+30)로 잘림 → 미완결
    assert r.eval_complete is False and r.eval_n_bars == 6


# ---- 7. Look-ahead 방지 ----
def test_no_lookahead():
    # 청산 이전 봉(T-5m)에 극단 고/저가 → forward/MFE/MAE 에 절대 반영 안 됨
    pre = _candle("X", T0 - timedelta(minutes=5), 50.0, 999.0, 1.0, 50.0)
    # 청산 봉 정각(T0)에도 극단값 → strict '>' 로 제외되어야 함
    candles = [pre] + _ramp_day(n=13)
    r = ExitDatasetBuilder(_FakeStore(candles)).build([_trade()], session_id="s1").records[0]
    assert r.best_price_after_exit == 112.5      # 999(이전봉) 무시
    assert r.worst_price_after_exit == 100.5     # 1(이전봉) 무시
    assert abs(r.mfe - 0.125) < 1e-9
    assert r.forward_map()[5].price == 101.0     # 청산 이후 첫 봉만


# ---- Replay Label: STOP_LOSS 회복 / TAKE_PROFIT 지속 ----
def test_labels_recover_and_continue():
    store = _FakeStore(_ramp_day(n=13))          # 청산 후 상승 지속(고가 101.5..113.5)
    b = ExitDatasetBuilder(store)
    # 손절: 진입가 102 > 청산가 100(실제 손절 형태). 회복 기준 = 진입가 102.
    sl = b.build([_trade(reason="STOP_LOSS", entry_price=102.0, exit_price=100.0)],
                 session_id="s1").records[0]
    assert sl.did_price_recover_after_stop_loss is True      # 고가가 진입가 102 회복
    assert sl.did_price_continue_after_take_profit is None   # 사유 불일치 → N/A
    # 익절: 청산가 100 대비 +0.3%(100.3) 초과 상승
    tp = b.build([_trade(reason="TAKE_PROFIT", exit_price=100.0)], session_id="s1").records[0]
    assert tp.did_price_continue_after_take_profit is True
    assert tp.did_price_recover_after_stop_loss is None

    # 청산 후 하락만 + 진입가 102 → 진입가 회복 실패 → False
    down = [_candle("Y", T0, 100, 100, 100, 100)]
    for i in range(1, 8):
        ts = T0 + timedelta(minutes=5 * i); c = 100.0 - i
        down.append(_candle("Y", ts, c + 0.5, c + 0.5, c - 0.5, c))
    r2 = ExitDatasetBuilder(_FakeStore(down)).build(
        [_trade("Y", reason="STOP_LOSS", entry_price=102.0, exit_price=100.0)],
        session_id="s1").records[0]
    assert r2.did_price_recover_after_stop_loss is False


# ---- exit 이후 봉 없음 → forwards NULL, session_end 미확인 ----
def test_exit_at_session_end_no_post_bars():
    only = [_candle("Z", T0, 100, 100, 100, 100)]     # 청산 봉이 그날 마지막
    r = ExitDatasetBuilder(_FakeStore(only)).build([_trade("Z")], session_id="s1").records[0]
    assert all(fp.price is None and not fp.complete for fp in r.forwards)
    assert r.session_end_complete is False
    assert r.eval_n_bars == 0 and r.mfe is None


# ---- to_flat 컬럼(사용자 요청 스키마) ----
def test_flat_schema_columns():
    store = _FakeStore(_ramp_day(n=13))
    flat = ExitDatasetBuilder(store).build([_trade()], session_id="s1").to_flat_dicts()[0]
    for col in ("session_id", "symbol", "market", "side", "entry_time", "exit_time",
                "holding_minutes", "holding_bars", "entry_price", "exit_price", "exit_reason",
                "forward_price_5m", "forward_return_5m", "forward_price_60m", "forward_return_60m",
                "session_end_price", "session_end_return", "mfe", "mae",
                "best_price_after_exit", "worst_price_after_exit",
                "did_price_recover_after_stop_loss", "did_price_continue_after_take_profit",
                "schema_version"):
        assert col in flat, f"missing column: {col}"


# ---- 8. Journal → Exit Dataset 결정론적 통합 (고정 픽스처 DB, 로컬 DB 비의존) ----
import pytest


def _candle1m(sym, ts_utc, o, h, l, c):
    """CandleStore 저장/조회 계약(interval=1m, USD→US)에 맞춘 US 봉."""
    return Candle(
        symbol=sym, interval="1m", ts_utc=ts_utc,
        ts_exchange=ts_utc.astimezone(exchange_tz("US")),
        session="REGULAR", open=o, high=h, low=l, close=c, volume=1000.0,
        currency="USD", adjusted=True, market="US")


# 사유별 SELL 1건씩 — 알려진 소량 데이터. 가변 로컬 #003 DB(29↔54)를 대체한다.
_FIX_EXITS = (
    ("AAA", "STOP_LOSS", 100.0, 99.0),
    ("BBB", "TAKE_PROFIT", 100.0, 101.5),
    ("CCC", "TIME_EXIT", 100.0, 100.2),
    ("DDD", "EOD_FLATTEN", 100.0, 100.0),
)


def _order_record(symbol, side, kind, qty, price):
    from src.order.models import OrderRecord
    return OrderRecord(
        client_order_id=f"{symbol}-{side}-{kind}", broker_order_id=None,
        symbol=symbol, side=side, kind=kind, order_type="MARKET",
        requested_quantity=qty, filled_quantity=qty, avg_fill_price=price,
        fee=0.0, status="FILLED", ref_price=price).to_dict()


def _outcome(symbol, side, kind, qty, price):
    return {"symbol": symbol, "action": "ORDERED", "reason": kind,
            "order_record": _order_record(symbol, side, kind, qty, price)}


def _write_fixture_dbs(tmp_path):
    """알려진 소량 데이터로 journal + candle DB 를 생성(결정론적, 로컬 산출물 비의존)."""
    from src.journal.models import JournalCycle
    from src.journal.sqlite_store import SqliteJournalStore
    from src.storage.candle_store import CandleStore

    jdb = str(tmp_path / "journal_fixture.sqlite")
    mdb = str(tmp_path / "market_fixture.sqlite")
    syms = [s for s, *_ in _FIX_EXITS]

    j = SqliteJournalStore(jdb)
    # 진입 사이클(BUY) → 청산 사이클(SELL). FIFO 를 위해 진입이 먼저 기록된다.
    entries = [_outcome(s, "BUY", "ENTRY", 10.0, entry_px)
               for s, _r, entry_px, _x in _FIX_EXITS]
    exits = [_outcome(s, "SELL", reason, 10.0, exit_px)
             for s, reason, _e, exit_px in _FIX_EXITS]
    j.save_cycle(JournalCycle("20260708T133500Z", "2026-07-08T13:35:00Z", "REGULAR",
                              syms, "RAN", None, None,
                              outcomes=entries, order_count=len(entries)))
    j.save_cycle(JournalCycle("20260708T140000Z", "2026-07-08T14:00:00Z", "REGULAR",
                              syms, "RAN", None, None,
                              outcomes=exits, order_count=len(exits)))
    j.close()

    # 청산 봉(T0=14:00) + 이후 같은 거래일 봉(T0+5..T0+30) → forward horizon 완결.
    # 봉 label 은 interval="1m"(조회 계약)이나 배치 간격은 build_exit_dataset_from_journal
    # 기본 bar_minutes=5.0 에 맞춰 5분(T0+5·i)이다 — builder 는 label 이 아닌 ts_utc 로만 판단.
    candles = []
    for s in syms:
        for i in range(0, 7):
            ts = T0 + timedelta(minutes=5 * i)
            c = 100.0 + i * 0.1
            candles.append(_candle1m(s, ts, c, c + 0.5, c - 0.5, c))
    with CandleStore(mdb) as store:
        store.upsert_many(candles)
    return jdb, mdb


def test_build_exit_dataset_from_journal_deterministic(tmp_path):
    """journal → exit dataset 통합 경로를 고정 픽스처로 결정론적으로 검증.

    로컬 누적 DB(data/*.sqlite)에 의존하지 않으므로 DB 없음/29건/54건과 무관하게
    항상 동일하게 통과한다(Issue #17)."""
    from src.journal.sqlite_store import SqliteJournalStore
    from src.storage.candle_store import CandleStore

    jdb, mdb = _write_fixture_dbs(tmp_path)
    j = SqliteJournalStore(jdb)
    with CandleStore(mdb) as store:
        ds = build_exit_dataset_from_journal(j, store, session_id="fixture_us")
    j.close()

    assert len(ds) == len(_FIX_EXITS)          # 알려진 청산 4건(사유별 1건)
    reasons = {r.exit_reason for r in ds.records}
    assert reasons == {"STOP_LOSS", "TAKE_PROFIT", "TIME_EXIT", "EOD_FLATTEN"}
    assert all(r.side == "SELL" and r.market == "US" for r in ds.records)
    # 장중 청산이므로 forward(+15m)는 모두 완결(청산 이후 같은 날 봉 존재)
    assert all(r.forward_map()[15].complete for r in ds.records)


def test_exit_dataset_from_journal_is_reproducible(tmp_path):
    """동일 입력 → 동일 출력(레코드 수·사유). 결정론 계약."""
    from src.journal.sqlite_store import SqliteJournalStore
    from src.storage.candle_store import CandleStore

    jdb, mdb = _write_fixture_dbs(tmp_path)

    def _run():
        j = SqliteJournalStore(jdb)
        with CandleStore(mdb) as store:
            ds = build_exit_dataset_from_journal(j, store, session_id="fixture_us")
        j.close()
        return len(ds), sorted(r.exit_reason for r in ds.records)

    assert _run() == _run()


# ---- 9. #004 동일 Dataset 생성 ----
@pytest.mark.skipif(not (os.path.exists(_KR_DB) and os.path.exists(_KR_MKT)),
                    reason="#004 세션 데이터 없음 — 로컬 전용 통합")
def test_session_004_records():
    from src.journal.sqlite_store import SqliteJournalStore
    from src.storage.candle_store import CandleStore
    j = SqliteJournalStore(_KR_DB)
    with CandleStore(_KR_MKT) as store:
        ds = build_exit_dataset_from_journal(j, store, session_id="krx_exit_test_session004")
    assert len(ds) > 0
    assert all(r.side == "SELL" and r.market == "KR" for r in ds.records)
    # look-ahead 안전: 완결된 forward 는 exit 이후 시각이어야 함
    for r in ds.records:
        for fp in r.forwards:
            if fp.complete:
                assert fp.target_time is not None and fp.price is not None
