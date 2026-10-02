"""Calendar coverage vs CRSP trading days (Ken French daily file) + Toss adjustment check.
Coverage/quality only. Price ratios are used only to classify adjustment basis; no returns are reported.
Toss comparison is limited to dates <= 2026-01-20 (blind OOS starts 2026-01-21)."""
import io, json, os, sqlite3, statistics, zipfile
from datetime import datetime, timezone

T = os.path.dirname(__file__)
EXT = os.environ.get("AUDIT_EXT_DIR", os.path.join(T, "ext"))
DB = "/Users/eongyupark/Workspace/02_personal/01_ai_trading/ai_trading_system/data/market_history.sqlite"
LAST_ALLOWED = "2026-01-20"

z = zipfile.ZipFile(os.path.join(EXT, "ff_daily.zip"))
txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
crsp = sorted({f"{l[:4]}-{l[4:6]}-{l[6:8]}" for l in (x.split(",")[0].strip() for x in txt) if len(l) == 8 and l.isdigit()})
crsp_set = set(crsp)
FF_LAST = crsp[-1]


def ydates(sym):
    j = json.load(open(os.path.join(EXT, f"yahoo_{sym.replace('^', 'IDX_')}.json")))["chart"]["result"][0]
    ts = j["timestamp"]
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose")
    out = {}
    for i, t in enumerate(ts):
        # Yahoo daily timestamps are the session open in exchange time; take the ET date via UTC-4/5 offset of gmtoffset
        d = datetime.fromtimestamp(t + j["meta"]["gmtoffset"], timezone.utc).strftime("%Y-%m-%d")
        out[d] = (q["close"][i], adj[i] if adj else None)
    return out


def coverage(dates, start, end):
    ref = [d for d in crsp if start <= d <= end]
    have = {d for d in dates if start <= d <= end}
    missing = [d for d in ref if d not in have]
    extra = sorted(d for d in have if d not in crsp_set)
    return dict(ref_days=len(ref), missing=len(missing), missing_sample=missing[:8], extra=len(extra), extra_sample=extra[:40])


rep = {"crsp_calendar": dict(first=crsp[0], last=FF_LAST, days=len(crsp))}
syms = ["SPY", "QQQ", "^GSPC", "^NDX", "^SP500TR", "^VIX", "SSO", "SDS", "SH", "QLD", "QID", "PSQ",
        "SPXL", "UPRO", "SPXU", "TQQQ", "SQQQ", "SOXL", "SOXS"]
Y = {}
for s in syms:
    Y[s] = ydates(s)
    d = sorted(Y[s])
    nulls = [k for k, v in Y[s].items() if v[0] is None]
    rep["Y:" + s] = coverage(d, d[0], FF_LAST)
    rep["Y:" + s]["null_close_dates"] = len(nulls)
    rep["Y:" + s]["null_close_sample"] = sorted(nulls)[:5] + sorted(nulls)[-3:]

# split events inside the Toss comparison window (2016-03-18..2026-01-20)
for s in ["SQQQ", "TQQQ", "SOXL", "SOXS", "QQQ"]:
    j = json.load(open(os.path.join(EXT, f"yahoo_{s}.json")))["chart"]["result"][0]
    ev = [(datetime.fromtimestamp(e["date"], timezone.utc).strftime("%Y-%m-%d"), e["splitRatio"])
          for e in j.get("events", {}).get("splits", {}).values()]
    rep["SPLITS_IN_WINDOW:" + s] = sorted(f"{d} {r}" for d, r in ev if "2016-03-18" <= d <= LAST_ALLOWED)

# Cboe VIX
import csv
rows = list(csv.reader(open(os.path.join(EXT, "VIX_History.csv"))))[1:]
cd = sorted(datetime.strptime(r[0], "%m/%d/%Y").strftime("%Y-%m-%d") for r in rows)
rep["CBOE_VIX"] = coverage(cd, cd[0], FF_LAST)
rep["CBOE_VIX"]["dup"] = len(cd) - len(set(cd))

# Toss daily coverage + adjustment basis
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
for s in ["SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "SOXS"]:
    tr = {r[0]: r[1] for r in c.execute(
        "select substr(ts_exchange,1,10), close from candle_history where interval='1d' and symbol=? order by ts_utc", (s,))}
    dd = sorted(tr)
    cov = coverage(dd, dd[0], min(dd[-1], FF_LAST))
    out = dict(first=dd[0], last=dd[-1], rows=len(dd), dup=0, **cov)
    if s in Y:
        common = [d for d in dd if d <= LAST_ALLOWED and d in Y[s] and Y[s][d][0] and Y[s][d][1]]
        r_raw = [tr[d] / Y[s][d][0] for d in common]
        r_adj = [tr[d] / Y[s][d][1] for d in common]
        cv = lambda xs: statistics.pstdev(xs) / statistics.mean(xs)
        # ratio stability: constant ratio => same adjustment basis (up to a later-event scale factor)
        out["vs_yahoo"] = dict(n=len(common), raw_ratio_first=round(r_raw[0], 6), raw_ratio_last=round(r_raw[-1], 6),
                               raw_ratio_cv=round(cv(r_raw), 6), adj_ratio_first=round(r_adj[0], 6),
                               adj_ratio_last=round(r_adj[-1], 6), adj_ratio_cv=round(cv(r_adj), 6))
    rep["TOSS:" + s] = out

for k, v in rep.items():
    print(k, v)
json.dump(rep, open(os.path.join(EXT, "audit2_summary.json"), "w"), indent=1)
