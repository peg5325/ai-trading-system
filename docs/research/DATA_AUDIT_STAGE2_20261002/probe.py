"""Stage-2 data audit probe: coverage/quality only. No returns or performance stats."""
import csv, io, json, os, sys, time, urllib.request, zipfile
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(__file__), "ext")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def save(name, data):
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(data)


res = {}
# FRED
for sid in ["DTB3", "DGS3MO", "VIXCLS"]:
    try:
        b = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}")
        save(f"{sid}.csv", b)
        rows = list(csv.reader(io.StringIO(b.decode())))[1:]
        blank = sum(1 for r in rows if r[1] in ("", "."))
        res[sid] = dict(first=rows[0][0], last=rows[-1][0], rows=len(rows), blank=blank)
    except Exception as e:
        res[sid] = dict(error=repr(e))

# Cboe VIX
try:
    b = get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
    save("VIX_History.csv", b)
    rows = list(csv.reader(io.StringIO(b.decode())))[1:]
    res["CBOE_VIX"] = dict(first=rows[0][0], last=rows[-1][0], rows=len(rows))
except Exception as e:
    res["CBOE_VIX"] = dict(error=repr(e))

# Ken French daily factors
try:
    b = get("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip")
    save("ff_daily.zip", b)
    z = zipfile.ZipFile(io.BytesIO(b))
    txt = z.read(z.namelist()[0]).decode("latin-1").splitlines()
    data = [l.split(",")[0].strip() for l in txt if l[:8].strip().isdigit() and len(l.split(",")[0].strip()) == 8]
    res["FF_RF_daily"] = dict(file=z.namelist()[0], first=data[0], last=data[-1], rows=len(data))
except Exception as e:
    res["FF_RF_daily"] = dict(error=repr(e))

# Yahoo v8 chart
now = int(time.time())
for sym in ["SPY", "QQQ", "^GSPC", "^NDX", "^SP500TR", "^VIX", "SSO", "SDS", "SH", "QLD", "QID", "PSQ",
            "SPXL", "UPRO", "SPXU", "TQQQ", "SQQQ", "SOXL", "SOXS"]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}"
           f"?period1=0&period2={now}&interval=1d&events=div%2Csplit&includeAdjustedClose=true")
    try:
        b = get(url)
        save(f"yahoo_{sym.replace('^', 'IDX_')}.json", b)
        j = json.loads(b)["chart"]["result"][0]
        ts = j["timestamp"]
        q = j["indicators"]["quote"][0]
        nclose_null = sum(1 for v in q["close"] if v is None)
        has_adj = "adjclose" in j["indicators"]
        ev = j.get("events", {})
        d = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        res["Y:" + sym] = dict(first=d(ts[0]), last=d(ts[-1]), rows=len(ts), null_close=nclose_null,
                               adjclose=has_adj, dividends=len(ev.get("dividends", {})),
                               splits=len(ev.get("splits", {})))
    except Exception as e:
        res["Y:" + sym] = dict(error=repr(e))
    time.sleep(1)

for k, v in res.items():
    print(k, v)
json.dump(res, open(os.path.join(OUT, "probe_summary.json"), "w"), indent=1)
