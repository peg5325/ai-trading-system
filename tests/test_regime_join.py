"""국면 조인 — **look-ahead 차단**과 확장 지평 규약 고정 (H3).

이 파일의 중심은 "국면 값이 맞나"가 아니라 **"결정 시점 이후 정보가 라벨에 새지 않나"** 다.
예비 관측에서 하루 국면을 당일 종가로 계산했을 때 신호가 15배 부풀려졌기 때문에,
그 오염을 재현할 수 있는 형태로 테스트를 짠다.

핵심 기법 — **오염 주입(poisoning)**: 결정 시점 이후 봉을 극단값으로 바꾼 뒤 라벨이
그대로인지 본다. 라벨이 흔들리면 그 경로로 미래 정보가 흐르고 있다는 뜻이다.
`asof` 필드만 검사하면 "계산은 미래를 봤는데 asof 만 과거로 적힌" 경우를 놓친다.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.forward_extended import (
    compute_extended_forwards,
    regular_closes_by_date,
)
from src.history.regime_join import (
    BREADTH_MIN_SYMBOLS,
    BreadthIndex,
    LookAheadError,
    RegimeLabels,
    assert_no_lookahead,
    breadth_regime,
    context_series,
    daily_bars,
    daily_context_before,
    daily_context_index,
    default_warmup,
    vol_regime,
    volatility_series,
    windowed_context,
)
from src.market_context.builder import build_benchmark_context
from src.models import ET, Candle, classify_session

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DB = os.path.join(_ROOT, "data", "market_history.sqlite")


# ---------------------------------------------------------------------------
# 합성 캔들 헬퍼 — 거래일 D 의 정규장 5m 봉을 만든다.
# ---------------------------------------------------------------------------

def _bar(symbol: str, et_dt: datetime, close: float, volume: float = 1000.0) -> Candle:
    ts_ex = et_dt.replace(tzinfo=ET)
    return Candle(symbol=symbol, interval="5min",
                  ts_utc=ts_ex.astimezone(timezone.utc), ts_exchange=ts_ex,
                  session=classify_session(ts_ex), open=close, high=close,
                  low=close, close=close, volume=volume, currency="USD", adjusted=True)


def _session_bars(symbol: str, day: date, closes: list[float],
                  start_hour: int = 9, start_min: int = 30) -> list[Candle]:
    """정규장 09:30 부터 5분 간격으로 closes 를 깐다."""
    base = datetime(day.year, day.month, day.day, start_hour, start_min)
    return [_bar(symbol, base + timedelta(minutes=5 * i), c) for i, c in enumerate(closes)]


def _multi_day(symbol: str, days: list[date], per_day: int,
               level_fn) -> list[Candle]:
    """여러 거래일에 걸친 5m 봉. level_fn(day_index, bar_index) -> close."""
    out: list[Candle] = []
    for di, d in enumerate(days):
        out.extend(_session_bars(symbol, d, [level_fn(di, i) for i in range(per_day)]))
    return out


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 1) warm-up 윈도가 결과를 바꾸지 않는다 (builder 재사용의 전제)
# ---------------------------------------------------------------------------

def test_windowed_context_equals_full_history_on_synthetic():
    """윈도 호출 == 전체 이력 호출. 다르면 성능 최적화가 국면을 바꾼 것이다."""
    days = _weekdays(date(2026, 3, 2), 12)
    bars = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di * 0.7 + i * 0.02)
    for i in (len(bars) - 1, len(bars) // 2, 700):
        windowed = windowed_context(bars, i, symbol="QQQ", ema_period=20)
        full = build_benchmark_context(bars[:i + 1], decision_time=bars[i].ts_utc,
                                       symbol="QQQ", timeframe="5m", ema_period=20)
        assert windowed.to_dict() == full.to_dict(), f"봉 {i} 에서 윈도/전체 결과가 다르다"


@pytest.mark.skipif(not os.path.exists(HISTORY_DB),
                    reason="market_history.sqlite 없음(gitignore 운영 산출물)")
@pytest.mark.parametrize("ema_period", [20, 200])
def test_windowed_context_matches_full_history_on_real_data(ema_period):
    """실데이터에서도 윈도 == 전체 이력(1e-9). EMA 수렴 가정을 실측으로 고정한다."""
    from src.history.candidate_stream import build_decision_bars, load_1m_candles
    bars = build_decision_bars(load_1m_candles(HISTORY_DB, "QQQ",
                                               table="candle_history"), "QQQ")
    if len(bars) < default_warmup(ema_period) + 50:
        pytest.skip(f"QQQ 봉이 warm-up({default_warmup(ema_period)})보다 적다")
    step = max(1, len(bars) // 40)
    for i in range(default_warmup(ema_period), len(bars), step):
        w = windowed_context(bars, i, symbol="QQQ", ema_period=ema_period).to_dict()
        f = build_benchmark_context(bars[:i + 1], decision_time=bars[i].ts_utc,
                                    symbol="QQQ", timeframe="5m",
                                    ema_period=ema_period).to_dict()
        for k, wv in w.items():
            fv = f[k]
            if isinstance(wv, float) and isinstance(fv, float):
                assert abs(wv - fv) < 1e-9, f"봉 {i} {k}: {wv} vs {fv}"
            else:
                assert wv == fv, f"봉 {i} {k}: {wv!r} vs {fv!r}"


# ---------------------------------------------------------------------------
# 2) 장중 국면 — 결정 시점 이후 봉이 라벨을 바꾸면 안 된다 (오염 주입)
# ---------------------------------------------------------------------------

def test_intraday_context_ignores_future_bars():
    """T 이후 봉을 폭등시켜도 T 의 국면은 불변이어야 한다."""
    days = _weekdays(date(2026, 3, 2), 6)
    bars = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di * 0.3 + i * 0.01)
    i = 300
    before = windowed_context(bars, i, symbol="QQQ", ema_period=20).to_dict()

    poisoned = list(bars)
    for j in range(i + 1, len(poisoned)):
        b = poisoned[j]
        poisoned[j] = Candle(**{**b.__dict__, "close": b.close * 5.0,
                                "high": b.high * 5.0, "open": b.open * 5.0})
    after = windowed_context(poisoned, i, symbol="QQQ", ema_period=20).to_dict()
    assert before == after, "미래 봉이 장중 국면 라벨을 바꿨다 — look-ahead"


def test_intraday_asof_never_exceeds_decision_time():
    days = _weekdays(date(2026, 3, 2), 4)
    bars = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di + i * 0.01)
    for ctx, bar in zip(context_series(bars, symbol="QQQ", ema_period=20), bars):
        assert ctx is not None
        asof = datetime.fromisoformat(ctx.benchmark_source_bar_end.replace("Z", "+00:00"))
        assert asof <= bar.ts_utc


# ---------------------------------------------------------------------------
# 3) 하루 국면 — **당일 정보를 절대 쓰지 않는다** (예비 관측을 15배 부풀린 그 오염)
# ---------------------------------------------------------------------------

def test_daily_bars_use_regular_session_close_only():
    """일봉 종가 = 그 날 마지막 REGULAR 봉 종가. POST 봉이 섞이면 안 된다."""
    d = date(2026, 3, 3)
    bars = _session_bars("QQQ", d, [100.0 + i for i in range(78)])   # 정규장
    bars += [_bar("QQQ", datetime(d.year, d.month, d.day, 17, 0), 999.0)]  # POST 폭등
    dbars = daily_bars(bars, "QQQ")
    assert len(dbars) == 1
    assert dbars[0].close == bars[77].close, "POST 봉이 일간 종가로 새어들었다"
    assert dbars[0].ts_utc == bars[77].ts_utc, "일봉 라벨은 정규장 마감 시각이어야 한다"


def test_daily_context_uses_only_strictly_prior_days():
    """앵커 거래일 당일 일봉은 절대 선택되지 않는다."""
    days = _weekdays(date(2026, 3, 2), 30)
    bars = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di * 0.5)
    dbars = daily_bars(bars, "QQQ")
    dd, ctxs = daily_context_index(dbars, symbol="QQQ")
    anchor = days[25]
    ctx = daily_context_before(dd, ctxs, anchor)
    assert ctx is not None
    asof = datetime.fromisoformat(ctx.benchmark_source_bar_end.replace("Z", "+00:00"))
    assert asof.astimezone(ET).date() == days[24], "전날 마감이 아니다"
    assert asof.astimezone(ET).date() < anchor


def test_daily_context_ignores_same_day_and_future_days():
    """★ 핵심 — 앵커 당일과 그 이후 일봉을 폭등시켜도 하루 국면은 불변.

    이 테스트가 통과하지 못하면 예비 관측의 +0.120%p 오염이 그대로 재현된다.
    """
    days = _weekdays(date(2026, 3, 2), 30)
    anchor = days[25]
    base = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di * 0.5)

    def daily_label(bars):
        dbars = daily_bars(bars, "QQQ")
        dd, ctxs = daily_context_index(dbars, symbol="QQQ")
        ctx = daily_context_before(dd, ctxs, anchor)
        return None if ctx is None else ctx.to_dict()

    before = daily_label(base)
    poisoned = [
        c if c.ts_exchange.date() < anchor else
        Candle(**{**c.__dict__, "close": c.close * 10, "high": c.high * 10,
                  "open": c.open * 10})
        for c in base
    ]
    assert before == daily_label(poisoned), "당일/이후 봉이 하루 국면을 바꿨다 — look-ahead"


def test_daily_context_none_before_first_day():
    days = _weekdays(date(2026, 3, 2), 5)
    bars = _multi_day("QQQ", days, 78, lambda di, i: 100.0 + di)
    dbars = daily_bars(bars, "QQQ")
    dd, ctxs = daily_context_index(dbars, symbol="QQQ")
    assert daily_context_before(dd, ctxs, days[0]) is None, "첫 거래일엔 '전날'이 없다"


# ---------------------------------------------------------------------------
# 4) breadth — 결정 시점 이하 봉만, stale 종목 제외
# ---------------------------------------------------------------------------

def _breadth_fixture(n_symbols: int, above: int):
    """above 개 종목은 EMA 위, 나머지는 아래가 되도록 만든다."""
    days = _weekdays(date(2026, 3, 2), 3)
    bi = BreadthIndex()
    bars_ref = None
    for s in range(n_symbols):
        sym = f"S{s}"
        rising = s < above
        bars = _multi_day(sym, days, 78,
                          (lambda di, i: 100.0 + di * 2 + i * 0.05) if rising
                          else (lambda di, i: 200.0 - di * 2 - i * 0.05))
        bi.add(sym, bars, context_series(bars, symbol=sym, ema_period=20))
        bars_ref = bars
    return bi, bars_ref


def test_breadth_counts_symbols_above_ema():
    n, above = 10, 7
    bi, bars = _breadth_fixture(n, above)
    b, used, asof = bi.at(bars[-1].ts_utc)
    assert used == n
    assert b == pytest.approx(above / n)
    assert breadth_regime(b) == "UP"
    assert asof <= bars[-1].ts_utc


def test_breadth_ignores_bars_after_decision_time():
    """mid 시점 breadth 는 mid 이후 봉이 인덱스에 있든 없든 같아야 한다.

    전체 인덱스로 조회한 값과, mid 까지만 담아 만든 인덱스로 조회한 값을 비교한다 —
    다르면 결정 시점 이후 봉이 breadth 에 흘러들고 있다.
    """
    days = _weekdays(date(2026, 3, 2), 3)
    per_symbol = {}
    for s in range(8):
        sym = f"S{s}"
        rising = s < 6
        per_symbol[sym] = _multi_day(
            sym, days, 78,
            (lambda di, i: 100.0 + di * 2 + i * 0.05) if rising
            else (lambda di, i: 200.0 - di * 2 - i * 0.05))

    full = BreadthIndex()
    for sym, bars in per_symbol.items():
        full.add(sym, bars, context_series(bars, symbol=sym, ema_period=20))

    any_bars = next(iter(per_symbol.values()))
    mid = any_bars[len(any_bars) // 2].ts_utc

    truncated = BreadthIndex()
    for sym, bars in per_symbol.items():
        past = [b for b in bars if b.ts_utc <= mid]
        truncated.add(sym, past, context_series(past, symbol=sym, ema_period=20))

    assert full.at(mid) == truncated.at(mid), "미래 봉이 breadth 를 바꿨다 — look-ahead"


def test_breadth_none_when_too_few_symbols():
    bi, bars = _breadth_fixture(BREADTH_MIN_SYMBOLS - 1, 2)
    b, used, _ = bi.at(bars[-1].ts_utc)
    assert b is None and used == BREADTH_MIN_SYMBOLS - 1


def test_breadth_excludes_stale_symbols():
    """공백으로 낡은 봉밖에 없는 종목은 breadth 에서 빠진다."""
    bi, bars = _breadth_fixture(8, 6)
    far_future = bars[-1].ts_utc + timedelta(hours=6)
    b, used, _ = bi.at(far_future)
    assert used == 0 and b is None, "stale 봉이 현재 시장 폭으로 세어졌다"


# ---------------------------------------------------------------------------
# 5) 변동성 국면 — 기준선이 인과적인가
# ---------------------------------------------------------------------------

def test_volatility_series_is_causal():
    """뒤쪽 봉을 잘라내도 앞쪽 값이 변하지 않아야 한다(전체표본 분위수 금지)."""
    days = _weekdays(date(2026, 3, 2), 30)
    bars = _multi_day("QQQ", days, 78,
                      lambda di, i: 100.0 + (i % 7) * (0.5 if di < 15 else 3.0))
    vol_full, ref_full = volatility_series(bars)
    cut = int(len(bars) * 0.6)
    vol_cut, ref_cut = volatility_series(bars[:cut])
    for i in range(cut):
        assert vol_full[i] == vol_cut[i], f"봉 {i} 실현변동성이 미래에 의존한다"
        assert ref_full[i] == ref_cut[i], f"봉 {i} 변동성 기준선이 미래에 의존한다"


def _naive_vol_ref(vol, ref_bars):
    """기준선의 순진한 구현 — 봉마다 창 전체를 정렬한다. 최적화의 정답지."""
    n = len(vol)
    out = [None] * n
    for i in range(n):
        lo = max(0, i - ref_bars + 1)
        seg = sorted(v for v in vol[lo:i + 1] if v is not None)
        if len(seg) < ref_bars // 4:
            continue
        mid = len(seg) // 2
        out[i] = seg[mid] if len(seg) % 2 else (seg[mid - 1] + seg[mid]) / 2.0
    return out


@pytest.mark.parametrize("ref_bars", [8, 17, 40])
def test_volatility_ref_matches_naive_median(ref_bars):
    """슬라이딩 창 중앙값 == 창 전체 정렬 중앙값.

    성능 최적화가 값을 바꾸면 국면 라벨이 조용히 달라진다. 창 경계·홀짝·동점·warm-up
    None 이 모두 섞이도록 만든 입력으로 정답지와 대조한다.
    """
    days = _weekdays(date(2026, 3, 2), 6)
    # 동점과 급변이 함께 나오도록: 계단 + 반복 패턴
    bars = _multi_day("QQQ", days, 78,
                      lambda di, i: 100.0 + (i % 5) * (0.4 if di < 3 else 2.5))
    vol, ref = volatility_series(bars, window=12, ref_bars=ref_bars)
    assert ref == _naive_vol_ref(vol, ref_bars)
    assert any(r is not None for r in ref), "전부 None 이면 비교가 무의미하다"


def test_volatility_ref_window_drops_old_bars():
    """창을 벗어난 낡은 값이 기준선에 계속 남으면 안 된다.

    앞구간은 저변동, 뒷구간은 고변동으로 만들고 창 길이를 짧게 잡으면, 뒷구간의
    기준선은 앞구간 값에 끌려가지 않아야 한다.
    """
    days = _weekdays(date(2026, 3, 2), 8)
    bars = _multi_day("QQQ", days, 78,
                      lambda di, i: 100.0 + (i % 3) * (0.1 if di < 4 else 5.0))
    vol, ref = volatility_series(bars, window=12, ref_bars=40)
    assert ref == _naive_vol_ref(vol, 40)
    tail = [r for r in ref[-40:] if r is not None]
    head = [r for r in ref[100:200] if r is not None]
    assert tail and head
    assert min(tail) > max(head), "낡은 저변동 값이 기준선에 남아 있다"


def test_vol_regime_labels_high_low():
    assert vol_regime(0.02, 0.01) == (pytest.approx(2.0), "HIGH")
    assert vol_regime(0.005, 0.01) == (pytest.approx(0.5), "LOW")
    assert vol_regime(None, 0.01) == (None, None)
    assert vol_regime(0.02, None) == (None, None)
    assert vol_regime(0.02, 0.0) == (None, None), "기준선 0 은 비율 정의 불가"


# ---------------------------------------------------------------------------
# 6) 불변식 강제 — assert_no_lookahead
# ---------------------------------------------------------------------------

def test_assert_no_lookahead_accepts_valid_labels():
    assert_no_lookahead(RegimeLabels(
        decision_ts_utc="2026-03-10T14:00:00+00:00", et_date="2026-03-10",
        intraday_asof="2026-03-10T14:00:00Z", daily_asof="2026-03-09T20:00:00Z"))


def test_assert_no_lookahead_rejects_future_asof():
    with pytest.raises(LookAheadError, match="미래 정보"):
        assert_no_lookahead(RegimeLabels(
            decision_ts_utc="2026-03-10T14:00:00+00:00", et_date="2026-03-10",
            intraday_asof="2026-03-10T14:05:00Z"))


def test_assert_no_lookahead_rejects_same_day_daily_regime():
    """★ 하루 국면이 당일 마감을 쓰면 거부 — 예비 오염의 정확한 형태."""
    with pytest.raises(LookAheadError, match="당일 정보"):
        assert_no_lookahead(RegimeLabels(
            decision_ts_utc="2026-03-10T23:00:00+00:00", et_date="2026-03-10",
            daily_asof="2026-03-10T20:00:00Z"))


# ---------------------------------------------------------------------------
# 7) 확장 지평 — EOD / Nd
# ---------------------------------------------------------------------------

def _fwd_fixture():
    days = _weekdays(date(2026, 3, 2), 5)
    bars: list[Candle] = []
    for di, d in enumerate(days):
        bars.extend(_session_bars("T", d, [100.0 + di * 10 + i for i in range(78)]))
        bars.append(_bar("T", datetime(d.year, d.month, d.day, 17, 0), 555.0))  # POST
    closes = regular_closes_by_date(bars)
    return bars, closes, sorted(closes)


def test_eod_targets_regular_close_not_post():
    bars, closes, days = _fwd_fixture()
    anchor = bars[10]                                   # 첫날 정규장 중
    out = compute_extended_forwards(anchor, closes=closes, trading_days=days)
    eod = out["eod"]
    assert eod["complete"]
    assert eod["exit_price"] == 100.0 + 77, "POST 봉(555)이 EOD 로 새어들었다"
    assert eod["exit_et_date"] == "2026-03-02"


def test_eod_incomplete_for_post_session_anchor():
    """앵커가 POST 면 마감은 이미 지났다 — 익일로 넘기지 않고 미완결."""
    bars, closes, days = _fwd_fixture()
    post = [b for b in bars if b.session == "POST"][0]
    out = compute_extended_forwards(post, closes=closes, trading_days=days)
    assert out["eod"]["complete"] is False
    assert out["eod"]["incomplete_reason"] == "eod_passed"


def test_day_horizons_step_trading_days():
    bars, closes, days = _fwd_fixture()
    anchor = bars[10]
    out = compute_extended_forwards(anchor, closes=closes, trading_days=days)
    for n in (1, 2, 3):
        fw = out[f"{n}d"]
        assert fw["complete"], f"{n}d 가 미완결"
        assert fw["exit_et_date"] == days[n].isoformat()
        assert fw["exit_price"] == 100.0 + n * 10 + 77


def test_day_horizon_incomplete_when_data_runs_out():
    bars, closes, days = _fwd_fixture()
    anchor = [b for b in bars if b.ts_exchange.date() == days[-1]][0]
    out = compute_extended_forwards(anchor, closes=closes, trading_days=days)
    for n in (1, 2, 3):
        assert out[f"{n}d"]["complete"] is False
        assert out[f"{n}d"]["incomplete_reason"] == "no_future_day"


def test_extended_forwards_are_strictly_forward():
    """모든 완결 지평의 exit 는 앵커보다 뒤여야 한다."""
    bars, closes, days = _fwd_fixture()
    for anchor in bars[::13]:
        out = compute_extended_forwards(anchor, closes=closes, trading_days=days)
        for key, fw in out.items():
            if not fw["complete"]:
                continue
            exit_ts = datetime.fromisoformat(fw["exit_ts_utc"])
            assert exit_ts > anchor.ts_utc, f"{key} exit 가 앵커보다 앞이다"


def test_return_pct_is_percent_units():
    """H2-b 의 30m/60m 과 같은 단위(%)여야 비교가 성립한다."""
    bars, closes, days = _fwd_fixture()
    anchor = bars[0]
    out = compute_extended_forwards(anchor, closes=closes, trading_days=days)
    eod = out["eod"]
    expected = (eod["exit_price"] - anchor.close) / anchor.close * 100.0
    assert eod["return_pct"] == pytest.approx(expected)
