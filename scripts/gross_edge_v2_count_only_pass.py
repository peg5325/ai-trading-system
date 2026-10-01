"""Count-only feasibility pass for screen v2 (no returns, no fills, dev window only).

Counts qualifying signal days / symbol-days per arm condition. Reads the frozen snapshot
with an SQL date filter <= 2026-01-20 and asserts the bound.
"""
import sqlite3
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/eongyupark/Workspace/02_personal/01_ai_trading/ai_trading_system")
from src.history import exchange_calendar as cal

SNAP = ("file:/Users/eongyupark/Workspace/02_personal/01_ai_trading/ai_trading_system/data/"
        "research_snapshots/p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite?mode=ro&immutable=1")
CORE = ["SPY", "QQQ", "IWM", "TQQQ", "SQQQ", "SOXL", "SOXS", "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "AMZN", "META", "GOOGL", "TSLA", "MU", "NFLX", "CRM", "PLTR"]
MEGA = ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "AMZN", "META", "GOOGL", "TSLA", "MU", "NFLX", "CRM", "PLTR"]
DSYM = ["SOXL", "SOXS", "TQQQ", "SQQQ", "TSLA", "PLTR", "AMD", "MU", "AVGO"]
END = "2026-01-20"
EVAL_START, EX0, EX1 = date(2023, 5, 18), date(2024, 7, 1), date(2024, 8, 30)

con = sqlite3.connect(SNAP, uri=True)
q = (f"SELECT symbol, ts_exchange, open, high, low, close, volume FROM candle_history "
     f"WHERE interval='1m' AND session='REGULAR' AND symbol IN ({','.join('?'*len(CORE))}) "
     f"AND substr(ts_exchange,1,10) <= ?")
df = pd.read_sql_query(q, con, params=(*CORE, END))
df["day"] = df.ts_exchange.str[:10]
df["hm"] = df.ts_exchange.str[11:16]
assert df.day.max() <= END, df.day.max()
print("rows", len(df), "max day", df.day.max())

df = df[df.day >= "2023-01-03"]
eval_days = {d.isoformat() for d in cal.trading_days(EVAL_START, date(2026, 1, 20))
             if not (EX0 <= d <= EX1) and not cal.is_early_close(d)}
print("eval days (non early close)", len(eval_days))

cnt = df.groupby(["symbol", "day"]).size()
def expected(d):
    return cal.expected_regular_minutes(date.fromisoformat(d))

daily = df.groupby(["symbol", "day"]).agg(O=("open", "first"), H=("high", "max"), L=("low", "min"),
                                          C=("close", "last")).reset_index()
daily["n"] = [cnt[(s, d)] for s, d in zip(daily.symbol, daily.day)]
daily["early"] = [cal.is_early_close(date.fromisoformat(d)) for d in daily.day]
daily["valid"] = [(n >= 0.95 * expected(d)) and not e for n, d, e in zip(daily.n, daily.day, daily.early)]


def wilder(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(series, n):
    d = np.diff(series, prepend=np.nan)[1:]
    g, l_ = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = wilder(g, n), wilder(l_, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(al == 0, np.where(ag == 0, 50.0, 100.0), 100 - 100 / (1 + ag / al))
    return np.concatenate([[np.nan], r])


feat = {}
for s in CORE:
    d = daily[(daily.symbol == s) & daily.valid].sort_values("day").reset_index(drop=True)
    rng = (d.H - d.L).values
    tr = np.maximum.reduce([rng, (d.H - d.C.shift()).abs().values, (d.L - d.C.shift()).abs().values])
    tr[0] = rng[0]
    d["nr7"] = pd.Series(rng).rolling(7).min().values == rng
    d["nr4"] = pd.Series(rng).rolling(4).min().values == rng
    d["inside"] = (d.H < d.H.shift()) & (d.L > d.L.shift())
    d["sma20"] = d.C.rolling(20).mean()
    d["sma200"] = d.C.rolling(200).mean()
    d["rsi2"] = rsi(d.C.values, 2)
    d.loc[:5, "rsi2"] = np.nan                         # warm-up 3n
    roc = d.C.pct_change().values
    r3 = np.full(len(d), np.nan)
    r3[1:] = rsi(roc[1:], 3)
    r3[:10] = np.nan                                   # warm-up 3n (+1 for ROC)
    d["rsi3roc"] = r3
    atr14 = wilder(tr, 14)
    atr14[:42] = np.nan
    d["atr14"] = atr14
    d["next_day"] = d.day.shift(-1)
    feat[s] = d


def prev_cond(s, col_expr):
    d = feat[s]
    out = set()
    for i in range(len(d) - 1):
        nd = d.day.iloc[i + 1]
        # t-1 must be the immediately previous trading day and valid (it is valid by construction)
        prev_td = cal.trading_days(date.fromisoformat(d.day.iloc[i]), date.fromisoformat(nd))
        if len(prev_td) != 2 or nd not in eval_days:
            continue
        if col_expr(d.iloc[i]):
            out.add((s, nd))
    return out


res = {}
res["B1_nr7"] = set().union(*[prev_cond(s, lambda r: bool(r.nr7)) for s in CORE])
res["B2_inside_nr4"] = set().union(*[prev_cond(s, lambda r: bool(r.inside and r.nr4)) for s in CORE])
res["B3_c_gt_sma20"] = set().union(*[prev_cond(s, lambda r: r.C > r.sma20) for s in CORE])
res["C1_rsi2_lt5_gt_sma200"] = set().union(*[prev_cond(s, lambda r: r.rsi2 < 5 and r.C > r.sma200) for s in CORE])
res["C2_rsi3roc_lt30"] = set().union(*[prev_cond(s, lambda r: r.rsi3roc < 30) for s in CORE])

# A: R = close_1444 / open_0930 - 1
bar = df.set_index(["symbol", "day", "hm"])
def r1444(s, d):
    try:
        o = bar.loc[(s, d, "09:30"), "open"]
        c = bar.loc[(s, d, "14:44"), "close"]
    except KeyError:
        return None
    return c / o - 1
a_days = {}
for name, s, th in [("A_QQQ_1.5", "QQQ", .015), ("A_QQQ_2.0", "QQQ", .02),
                    ("A_SOXL_4.5", "SOXL", .045), ("A_SOXL_6.0", "SOXL", .06)]:
    a_days[name] = {d for d in eval_days if (r := r1444(s, d)) is not None and abs(r) >= th}
    res[name] = a_days[name]
res["A_pooled_QQQ1.5_or_SOXL4.5_days"] = a_days["A_QQQ_1.5"] | a_days["A_SOXL_4.5"]
res["A_pooled_symbol_trades"] = {("QQQ", d) for d in a_days["A_QQQ_1.5"]} | {("SOXL", d) for d in a_days["A_SOXL_4.5"]}

# A5 single stocks: R>=3% and close_1444 > vwap_1444
mm = df[(df.symbol.isin(MEGA)) & (df.day.isin(eval_days)) & (df.hm <= "14:44")].copy()
mm["tp"] = (mm.high + mm.low + mm.close) / 3 * mm.volume
g = mm.groupby(["symbol", "day"])
agg = g.agg(tpv=("tp", "sum"), v=("volume", "sum"))
a5 = set()
for (s, d), row in agg.iterrows():
    r = r1444(s, d)
    if r is None or row.v <= 0:
        continue
    vwap = row.tpv / row.v
    if r >= .03 and bar.loc[(s, d, "14:44"), "close"] > vwap:
        a5.add((s, d))
res["A5_mega_up3_above_vwap"] = a5

# D: first bar 10:00-15:00 with close <= VWAP - k*ATR14(t-1)
dd = df[(df.symbol.isin(DSYM)) & (df.day.isin(eval_days))].sort_values(["symbol", "ts_exchange"]).copy()
dd["tpv"] = (dd.high + dd.low + dd.close) / 3 * dd.volume
dd["ctpv"] = dd.groupby(["symbol", "day"]).tpv.cumsum()
dd["cv"] = dd.groupby(["symbol", "day"]).volume.cumsum()
dd["vwap"] = dd.ctpv / dd.cv.replace(0, np.nan)
for k, name in [(0.6, "D1_k0.6"), (1.0, "D2_k1.0")]:
    hits = set()
    for s in DSYM:
        f = feat[s].set_index("next_day")
        sub = dd[(dd.symbol == s) & (dd.hm >= "10:00") & (dd.hm <= "15:00")]
        for d, grp in sub.groupby("day"):
            if d not in f.index or np.isnan(f.loc[d, "atr14"]):
                continue
            if (grp.close <= grp.vwap - k * f.loc[d, "atr14"]).any():
                hits.add((s, d))
    res[name] = hits

print(f"{'arm':38s} {'symbol-days':>11s} {'distinct days':>13s}")
for k, v in res.items():
    if v and isinstance(next(iter(v)), tuple):
        print(f"{k:38s} {len(v):11d} {len({d for _, d in v}):13d}")
    else:
        print(f"{k:38s} {'-':>11s} {len(v):13d}")
