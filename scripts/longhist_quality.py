"""Long-history data quality report (DATA ONLY — no strategy returns or signal performance).

Reads every series through `src.longhist.loader` (manifest SHA verified). Date coverage uses
purpose="coverage"; every price-value check uses purpose="performance" with end 2025-07-20, so the
sealed period (on/after 2025-07-21) is never read for prices. Toss daily bars are read with
`research_load(purpose="coverage")` and filtered to dates before 2025-07-21 before any ratio.

Writes docs/research/DATA_LONGHIST_V1.md (Korean) and docs/research/DATA_LONGHIST_V1_summary.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.longhist import default_root
from src.longhist import quality as Q
from src.longhist.loader import (
    SEAL_START,
    SERIES,
    ManifestMismatch,
    load_aux,
    load_daily,
    pinned_manifest_sha256,
)
from src.models import ET

PRICE_END = SEAL_START - timedelta(days=1)  # last date allowed in any price-value check
YAHOO = ["SPY", "QQQ", "^SOX", "SMH", "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS"]
TOSS = ["SPY", "QQQ", "TQQQ", "SQQQ", "SOXL", "SOXS"]
FIDELITY = [("TQQQ", "QQQ", 3), ("SQQQ", "QQQ", -3), ("SPXL", "SPY", 3), ("SPXS", "SPY", -3),
            ("SOXL", "^SOX", 3), ("SOXS", "^SOX", -3), ("SOXL", "SMH", 3), ("SOXS", "SMH", -3)]
EXPECTED_PER_YEAR = {"CPI": 12, "NFP": 12, "PCE": 12, "FOMC": 8}
REF_YAMLS = ["config/daytrade_events_history_v1.yaml", "config/daytrade_events_v1.yaml"]


def safe(sym: str) -> str:
    return sym.replace("^", "IDX_")


def f(x, nd=6):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}g}" if abs(x) < 1e-3 and x != 0 else f"{x:.{nd}f}".rstrip("0").rstrip(".")
    return f"{x:,}" if isinstance(x, int) else str(x)


def oc_map(df) -> dict[str, tuple[float, float]]:
    return {d: (o, c) for d, o, c in zip(df["date"], df["open"], df["close"])}


def run(root: Path, db: Path, events_path: Path) -> dict:
    man_bytes = (root / "manifest.json").read_bytes()
    man_sha = pinned_manifest_sha256()
    if hashlib.sha256(man_bytes).hexdigest() != man_sha:
        raise ManifestMismatch("manifest.json differs from config/longhist_data_v1.json")
    man = json.loads(man_bytes)
    cal = list(load_aux("norm/calendar/nyse_sessions.csv", root=root)["date"])
    out: dict = {"manifest_sha256": man_sha, "manifest_files": len(man["files"]),
                 "fetch_failures": man.get("fetch_failures", []), "last_run_utc": man.get("last_run_utc"),
                 "seal_start": SEAL_START.isoformat(), "price_check_end": PRICE_END.isoformat(),
                 "calendar": {"first": cal[0], "last": cal[-1], "sessions": len(cal)}}

    # 1) coverage (dates only) + OHLC sanity on pre-seal rows
    cov, ohlc = {}, {}
    for sym, rel in SERIES.items():
        dfc = load_daily(sym, purpose="coverage", root=root)
        c = Q.coverage(list(dfc["date"]), cal)
        meta = man["files"][rel]
        c.update(source=meta["source"], null_rows_dropped=meta.get("null_rows_dropped"),
                 fetch_day_rows_dropped=meta.get("fetch_day_rows_dropped"))
        cov[sym] = c
        dfp = load_daily(sym, start=dfc["date"].iloc[0], end=PRICE_END, purpose="performance", root=root)
        if "open" in dfp and dfp["open"].notna().any():
            px = dfp[["open", "high", "low", "close"]].astype(float)
            px.insert(0, "date", dfp["date"])
            rows = px.to_dict("records")
            ohlc[sym] = Q.ohlc_checks(rows)
        else:
            ohlc[sym] = {"rows": len(dfp), "note": "close-only or no OHLC columns"}
    out["coverage"], out["ohlc_pre_seal"] = cov, ohlc

    # 2) splits / dividends
    ev = {}
    for sym in YAHOO:
        sp = load_aux(f"norm/yahoo/{safe(sym)}_splits.csv", root=root)
        dv = load_aux(f"norm/yahoo/{safe(sym)}_dividends.csv", root=root)
        ev[sym] = {"splits": [f"{d} {int(n) if n == int(n) else n}:{int(m) if m == int(m) else m}"
                              for d, n, m in zip(sp["date"], sp["numerator"], sp["denominator"])],
                   "dividends": len(dv), "div_first": dv["date"].iloc[0] if len(dv) else None,
                   "div_last": dv["date"].iloc[-1] if len(dv) else None}
    out["corporate_actions"] = ev

    # 3) Yahoo vs Toss (pre-seal only)
    toss = {}
    for sym in TOSS:
        # coverage purpose bypasses the track gate; the pre-seal date filter below is the guard
        candles = research_load(str(db), sym, "1d", purpose="coverage", track=TRACK_INTRADAY)
        t = {c.ts_exchange.date().isoformat(): (c.open, c.close) for c in candles
             if c.ts_exchange.date() < SEAL_START}
        y = oc_map(load_daily(sym, start="1990-01-01", end=PRICE_END, purpose="performance", root=root))
        common = sorted(set(t) & set(y))
        toss[sym] = {"toss_rows_pre_seal": len(t), "toss_first": min(t) if t else None,
                     "toss_last_pre_seal": max(t) if t else None, "overlap": len(common),
                     "toss_only": len(set(t) - set(y)),
                     "yahoo_only_in_toss_span": len([d for d in y if t and min(t) <= d <= max(t) and d not in t]),
                     "close": Q.ratio_deviation([(t[d][1], y[d][1]) for d in common]),
                     "open": Q.ratio_deviation([(t[d][0], y[d][0]) for d in common])}
    out["toss_crosscheck"] = toss

    # 4) leveraged-ETF fidelity (data fidelity only, pre-seal)
    fid = []
    for etf, ref, mult in FIDELITY:
        e = oc_map(load_daily(etf, start="1990-01-01", end=PRICE_END, purpose="performance", root=root))
        r = oc_map(load_daily(ref, start="1990-01-01", end=PRICE_END, purpose="performance", root=root))
        fid.append({"etf": etf, "ref": ref, "mult": mult, **Q.fidelity(e, r, mult)})
    out["fidelity"] = fid

    # 5) events
    evy = yaml.safe_load(events_path.read_text())
    calset = set(cal)
    last_full_year = int(cal[-1][:4]) - 1
    evq = {}
    for typ, body in evy["types"].items():
        dates = body.get("dates") or [e["date"] for e in body.get("events", [])]
        in_span = [d for d in dates if cal[0] <= d <= cal[-1]]
        row = {"total": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
               "coverage": body["coverage"], "in_calendar_span": len(in_span),
               "on_non_session": sorted(d for d in in_span if d not in calset)}
        if typ in EXPECTED_PER_YEAR:
            py = Q.per_year_counts(dates, 1993, last_full_year)
            row["years_off_expected"] = {y: n for y, n in py.items() if n != EXPECTED_PER_YEAR[typ]}
        evq[typ] = row
    out["events"] = evq
    out["events_unresolved"] = evy.get("unresolved", [])
    out["events_excluded_fomc"] = evy.get("excluded_fomc_items", [])
    out["events_yaml_sha256"] = hashlib.sha256(events_path.read_bytes()).hexdigest()

    refs = []
    for rp in REF_YAMLS:
        ref = yaml.safe_load((ROOT / rp).read_text())
        rs, re_ = str(ref["coverage"]["start"]), str(ref["coverage"]["end"])
        for typ in ("CPI", "NFP", "PCE", "FOMC", "EARLY_CLOSE"):
            body = evy["types"][typ]
            ours = body.get("dates") or [e["date"] for e in body["events"]]
            theirs = [str(e["date"]) for e in ref.get("events", []) if e["type"] == typ]
            end = min(re_, cal[-1])
            refs.append({"ref_file": rp, "type": typ, **Q.compare_dates(ours, theirs, rs, end)})
        hol = [str(h["date"]) for h in ref.get("holidays") or [] if str(h["date"]) <= cal[-1]]
        refs.append({"ref_file": rp, "type": "HOLIDAY_IN_OUR_CALENDAR", "window": f"{rs}..{min(re_, cal[-1])}",
                     "checked": len(hol), "violations": sorted(d for d in hol if d in calset)})
        # weekdays inside the reference window that are neither our session nor a listed holiday
        d0, d1 = date.fromisoformat(rs), date.fromisoformat(min(re_, cal[-1]))
        holset = set(hol)
        gaps = []
        while d0 <= d1:
            s = d0.isoformat()
            if d0.weekday() < 5 and s not in calset and s not in holset:
                gaps.append(s)
            d0 += timedelta(days=1)
        refs[-1]["weekday_not_session_not_holiday"] = gaps
    out["event_crosscheck"] = refs
    return out


# ------------------------------------------------------------------ rendering (Korean)


def render(o: dict) -> str:
    L = []
    a = L.append
    a("# 장기 일봉 데이터 기반 v1 — 수집·품질 점검")
    a("")
    a(f"작성: `scripts/longhist_quality.py` 자동 생성 · 수집 실행(UTC): {o['last_run_utc']} · "
      "단계: ROADMAP 다음 연구 단계(장기 일봉 스크리닝)의 데이터 준비")
    a("")
    a("**이 문서는 데이터 점검만 한다. 전략 수익률·신호 성과는 계산하지 않았다.** 표의 모든 숫자는 스크립트가 계산했다.")
    a("")
    a("## 읽는 법")
    a("")
    a(f"- 봉인 구간: **{o['seal_start']} 이후**(swing 1d blind OOS 시작일, `src/history/oos_guard.py`). "
      f"가격 값을 쓰는 점검(OHLC 이상치, Toss 대조, 레버리지 충실도)은 **{o['price_check_end']}까지만** 읽었다. "
      "로더가 `purpose=\"performance\"`로 봉인 구간에 닿는 요청을 거부한다.")
    a("- 행 수·첫날·마지막 날·결측 같은 **날짜 존재 여부**는 봉인 구간까지 셌다(`purpose=\"coverage\"`). 가격은 보지 않았다.")
    a(f"- 원본·정규화 파일은 git 밖 `data/external/longhist/`에 있고 `manifest.json`이 SHA-256으로 고정한다. "
      f"manifest SHA-256: `{o['manifest_sha256']}` (파일 {o['manifest_files']}개). "
      "이 값은 `config/longhist_data_v1.json`에 고정돼 있다. 로더는 호출마다 manifest를 이 값과, 읽는 파일을 "
      "manifest의 SHA와 대조하고 하나라도 다르면 거부한다. `purpose=\"coverage\"`는 날짜 열만 돌려준다.")
    a("- Yahoo는 비공식 API라 원본을 커밋하지 않는다. `close`는 분할만 반영된 원종가, `adjclose`는 받은 날 기준 배당·분할 조정값이다.")
    a("- 주의: Yahoo `close`·`adjclose`의 **수준**은 받은 날(2026-10-12) 기준으로 조정돼 있어, 봉인 시작일 이전 값에도 "
      "봉인 구간 이후의 분할(예: TQQQ 2025-11-20, SOXS 2026-03-05·07-15, SPXS 2025-09-29)과 배당이 반영돼 있다. "
      "같은 날 시가→종가 비율은 영향을 받지 않는다. 가격 수준 자체를 쓰는 연구는 이 점을 감안해야 한다.")
    a(f"- 기준 달력: Yahoo SPY·QQQ 일봉 날짜의 합집합(1999-03-10 이전은 SPY만). "
      f"{o['calendar']['first']} ~ {o['calendar']['last']}, {f(o['calendar']['sessions'])}거래일.")
    if o["fetch_failures"]:
        a(f"- 수집 실패: {o['fetch_failures']}")
    else:
        a("- 수집 실패: 없음(요청한 시리즈 전부 받음, `^SOX` 포함).")
    a("")
    a("## 1. 시리즈별 기간과 결측")
    a("")
    a("결측 = 시리즈 첫날~마지막 날 사이의 기준 달력 거래일 중 행이 없는 날. 추가 = 기준 달력 기간 안인데 달력에 없는 날짜의 행"
      "(휴장일 등). 달력 시작(1993-01-29) 전 행은 따로 셌다.")
    a("")
    a("| 시리즈 | 출처 | 첫날 | 마지막 날 | 행 | 달력 시작 전 행 | 결측 | 추가 | 중복 | 버린 빈 행 | 결측·추가 예시 |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sym, c in o["coverage"].items():
        ex = ", ".join(c["missing_sample"][:4] + (["추가: " + ", ".join(c["extra_sample"][:6])] if c["extra"] else []))
        a(f"| {sym} | {c['source'].split(' ')[0]} | {c['first']} | {c['last']} | {f(c['rows'])} | {f(c['before_calendar'])} | {f(c['missing'])} | "
          f"{f(c['extra'])} | {f(c['duplicates'])} | {f(c.get('null_rows_dropped') or 0)} | {ex or '—'} |")
    a("")
    a("수집일 당일 봉(미완성 가능)은 정규화에서 뺐다.")
    a("")
    a(f"### 가격 형태 점검 ({o['price_check_end']}까지)")
    a("")
    a("| 시리즈 | 행 | OHLC 모순·빈 값 | 0 이하 | 평평한 봉(O=H=L=C) | 평평한 봉 기간 | 시가 = 전일 종가 | 그중 2010년 이후 | 마지막 발생일 |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sym, c in o["ohlc_pre_seal"].items():
        if "note" in c:
            a(f"| {sym} | {f(c['rows'])} | 시가 없음(종가 전용) | — | — | — | — | — | — |")
        else:
            span = f"{c['flat_first']}~{c['flat_last']}" if c["flat_first"] else "—"
            a(f"| {sym} | {f(c['rows'])} | {f(c['ohlc_inconsistent_or_null'])} | {f(c['non_positive'])} | "
              f"{f(c['flat_bars'])} | {span} | {f(c['open_equals_prior_close'])} | "
              f"{f(c['open_eq_prev_since_2010'])} | {c['open_eq_prev_last'] or '—'} |")
    a("")
    a("평평한 봉은 그날 종가 하나만 기록됐다는 뜻이라 그 기간에는 시가→종가를 쓸 수 없다. 시가 = 전일 종가가 많은 시리즈는 "
      "그 기간의 시가가 실제 개장가가 아닐 수 있다(지수 초기 자료에서 흔함). ETF는 우연히 같을 수 있어 적은 수는 정상 범위다. "
      "시가→종가 연구에 쓰기 전에 해당 기간을 사전등록에서 정해야 한다.")
    a("")
    a("## 2. 분할·배당 이벤트 (Yahoo)")
    a("")
    a("| 종목 | 분할(날짜 비율) | 배당 횟수 | 첫 배당 | 마지막 배당 |")
    a("| --- | --- | --- | --- | --- |")
    for sym, e in o["corporate_actions"].items():
        a(f"| {sym} | {'; '.join(e['splits']) or '없음'} | {f(e['dividends'])} | {e['div_first'] or '—'} | {e['div_last'] or '—'} |")
    a("")
    a("`close`는 이미 분할을 반영한 값이라 분할일 전후 시가→종가 비율은 영향을 받지 않는다. 배당은 시가 전에 빠지므로 같은 날 시가→종가에는 영향이 없다.")
    a("")
    a(f"## 3. Yahoo 일봉 vs 저장소 Toss 일봉 ({o['price_check_end']}까지)")
    a("")
    a("Toss는 `candle_history` interval 1d를 `research_load(purpose=\"coverage\")`로 읽고 봉인 시작일 이전 날짜만 썼다. "
      "편차 = |Toss ÷ Yahoo − 1|.")
    a("")
    a("| 종목 | Toss 행 | Toss 기간 | 겹친 날 | Toss만 | Yahoo만(Toss 기간 안) | 종가 편차 중앙값 | 종가 편차 최대 | 종가 0.1% 초과 일수 | 시가 편차 중앙값 | 시가 편차 최대 | 시가 0.1% 초과 일수 |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sym, t in o["toss_crosscheck"].items():
        a(f"| {sym} | {f(t['toss_rows_pre_seal'])} | {t['toss_first']}~{t['toss_last_pre_seal']} | {f(t['overlap'])} | "
          f"{f(t['toss_only'])} | {f(t['yahoo_only_in_toss_span'])} | {f(t['close']['median_abs_dev'])} | "
          f"{f(t['close']['max_abs_dev'])} | {f(t['close']['n_over_0_1pct'])} | {f(t['open']['median_abs_dev'])} | "
          f"{f(t['open']['max_abs_dev'])} | {f(t['open']['n_over_0_1pct'])} |")
    a("")
    a("## 4. 레버리지 ETF 충실도 (데이터 점검, 전략 성과 아님)")
    a("")
    a("**이 표는 전략 수익률이 아니다.** 같은 날 3배 ETF의 시가→종가 변화와 기준(지수/ETF) 시가→종가 변화 × 배수가 "
      "얼마나 일치하는지 보는 데이터 충실도 지표다. 나중에 ETF 상장 전 구간을 '기준 × 3'으로 가상 계산할 때 "
      f"그 근사가 얼마나 맞는지 가늠하는 용도다. {o['price_check_end']}까지만 계산했다.")
    a("")
    a("| ETF | 기준 | 배수 | 겹친 날 | 기간 | 상관계수 | 평균 절대 차이 |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for r in o["fidelity"]:
        a(f"| {r['etf']} | {r['ref']} | {r['mult']} | {f(r['n'])} | {r.get('first', '—')}~{r.get('last', '—')} | "
          f"{f(r['corr'], 4)} | {f(r['mean_abs_diff'], 6)} |")
    a("")
    a("주의: 시가는 ETF와 기준의 개장 체결 시각이 달라 차이가 생긴다. SOXL·SOXS의 추종 지수는 운용 기간 중 바뀐 적이 있어 "
      "`^SOX`·SMH 어느 쪽과도 완전히 같지 않을 수 있다(이 문서는 그 변경일을 검증하지 않았다).")
    a("")
    a("## 5. 이벤트 날짜 (`config/longhist_events_v1.yaml`)")
    a("")
    a(f"events yaml SHA-256: `{o['events_yaml_sha256']}`")
    a("")
    a("| 종류 | 출처 | 날짜 수 | 첫날 | 마지막 날 | 기준 달력 기간 안 | 그중 휴장일 | 연간 개수가 기대값과 다른 해(1993~) |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    srcs = {"CPI": "FRED 10", "NFP": "FRED 50", "PCE": "FRED 54", "FOMC": "연준 웹페이지",
            "FOMC_UNSCHEDULED": "연준 웹페이지", "EARLY_CLOSE": "규칙 유도(J)"}
    for typ, e in o["events"].items():
        off = e.get("years_off_expected")
        offs = "—" if off is None else (", ".join(f"{y}:{n}" for y, n in off.items()) or "없음")
        a(f"| {typ} | {srcs.get(typ, '')} | {f(e['total'])} | {e['first']} | {e['last']} | {f(e['in_calendar_span'])} | "
          f"{len(e['on_non_session'])}{' (' + ', '.join(e['on_non_session'][:6]) + ')' if e['on_non_session'] else ''} | {offs} |")
    a("")
    a("기대값: CPI·NFP·PCE 연 12회, FOMC 정례회의 연 8회. 다르면 날짜가 틀렸다는 뜻이 아니라 확인 대상이라는 뜻이다"
      "(셧다운으로 발표가 빠진 해, 1990년대 FRED 기록 방식 등). CPI·NFP·PCE는 1993-01-01 이전 날짜를 쓰지 않고(신뢰도 낮음), "
      "FRED가 한 달에 두 날짜 이상 올린 달은 날짜 목록에서 뺐으므로 그런 해는 12보다 적게 나온다(아래 미해결 표).")
    a("")
    a(f"**미해결(unresolved) {len(o['events_unresolved'])}건**" + (":" if o["events_unresolved"] else " — 없음."))
    fred_un = [u for u in o["events_unresolved"] if u["type"] != "FOMC"]
    for u in o["events_unresolved"]:
        if u["type"] == "FOMC":
            a(f"- FOMC {u['year']} `{u['heading']}` — {u['reason']} ({u['source']})")
    if fred_un:
        a("")
        a("FRED가 한 달에 날짜를 두 개 이상 올린 달(1993-01-01 이후). 어느 쪽이 정규 월간 발표인지 정하지 않고 "
          "이 달들의 날짜는 yaml `types.<종류>.dates`에서 모두 뺐다"
          "(하나는 계절조정 개정·벤치마크 등 데이터만 갱신한 날일 수 있다). **이 달들은 '발표 없음'이 아니라 '모름'이다.** "
          "이벤트 연구에 쓰기 전에 BLS·BEA 원문으로 발표일을 확정해야 하며, 그 전에는 이 달을 이벤트 없는 달로 취급하면 안 된다.")
        a("")
        a("| 종류 | 달 | 날짜 |")
        a("| --- | --- | --- |")
        for u in fred_un:
            a(f"| {u['type']} | {u['month']} | {', '.join(u['dates'])} |")
    a("")
    a(f"FOMC에서 뺀 항목 {len(o['events_excluded_fomc'])}건(취소된 회의, 서면 표결): "
      + ", ".join(f"{x['date']}({x['reason'].replace('EXCLUDED_', '').lower()})" for x in o["events_excluded_fomc"]))
    a("")
    a("FRED release 101 'FOMC Press Release'는 날짜 대부분이 매일 데이터 갱신이라 결정일로 쓰지 않았다(yaml `notes`).")
    a("FOMC 1994-02-04 이전에는 회의 당일 결정 발표가 없었다(yaml 정의에 적음).")
    a("")
    a("### 출처가 붙은 기존 달력과 대조")
    a("")
    a("기존 `config/daytrade_events_history_v1.yaml`(BLS·BEA·연준·NYSE 출처, 2023-04~2026-01)과 "
      "`config/daytrade_events_v1.yaml`(2026-10~12)과 겹치는 기간을 비교했다.")
    a("")
    a("| 기준 파일 | 종류 | 기간 | 우리 | 기준 | 일치 | 우리만 | 기준만 |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in o["event_crosscheck"]:
        if r["type"] == "HOLIDAY_IN_OUR_CALENDAR":
            continue
        a(f"| {Path(r['ref_file']).name} | {r['type']} | {r['window']} | {r['ours']} | {r['reference_count']} | "
          f"{r['matched']} | {', '.join(r['only_ours']) or '—'} | {', '.join(r['only_reference']) or '—'} |")
    a("")
    for r in o["event_crosscheck"]:
        if r["type"] == "HOLIDAY_IN_OUR_CALENDAR":
            a(f"- {Path(r['ref_file']).name} 휴장일 {r['checked']}개 중 우리 거래일 달력에 들어 있는 날: "
              f"{', '.join(r['violations']) or '없음'}. 평일인데 거래일도 휴장일 목록도 아닌 날: "
              f"{', '.join(r['weekday_not_session_not_holiday']) or '없음'}.")
    a("")
    ec = o["events"]["EARLY_CLOSE"]
    a(f"### 조기 폐장(1:00 p.m.) {ec['total']}일")
    a("")
    a("규칙: 7월 3일, 추수감사절 다음 날, 12월 24일 — 각각 평일이고 기준 달력의 거래일일 때만(J = 규칙 유도). "
      "독립기념일·성탄절이 토요일이면 전날 금요일이 휴장이라 거래일 필터에서 빠진다. "
      "규칙 밖 예외 조기 폐장(예: 과거 7월 5일·12월 26일 같은 특별 공지)은 공식 출처로 확인하지 못해 넣지 않았다. "
      "2023-04 이전 날짜는 출처 대조가 없다.")
    a("")
    a("## 6. 판단과 남은 일")
    a("")
    a("- 거래일 달력은 Yahoo 날짜에서 유도했다. 독립 공식 달력과의 전 기간 대조는 하지 않았다(2023-04~ 휴장일 대조만 위 표).")
    a("- CPI·NFP·PCE 날짜는 FRED가 그 발표를 올린 날이다. 2023-04 이전은 BLS·BEA 원문과 대조하지 않았다. "
      "모호한 달(미해결 표)은 BLS·BEA 원문으로 확인하기 전까지 이벤트일로 쓰지 않는다.")
    a("- FOMC 2003-09-15 'Meeting'은 연준 페이지에 성명(Statement)이 없고 다음 날 2003-09-16에 성명이 있어 "
      "결정일에서 빼고 미해결로 옮겼다(2000년 이후 성명 없는 정례회의 제목은 같은 규칙).")
    a("- `^SOX` 시가 품질, 기준 달력 밖 Cboe 행(휴장일 값)의 처리, VIX 결측일 보충 여부는 사전등록에서 정한다.")
    a("- 이 문서의 어떤 숫자도 전략 성과가 아니다.")
    a("")
    a("## 재현")
    a("")
    a("```")
    a(".venv/bin/python scripts/longhist_fetch.py    # 수집(원본 불변 저장, manifest 갱신, events yaml 생성)")
    a(".venv/bin/python scripts/longhist_quality.py  # 이 문서와 요약 JSON 생성")
    a("```")
    a("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--db", type=Path, default=None, help="market_history.sqlite (default: main checkout data/)")
    ap.add_argument("--events", type=Path, default=ROOT / "config" / "longhist_events_v1.yaml")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "research" / "DATA_LONGHIST_V1.md")
    a = ap.parse_args()
    root = a.root or default_root()
    db = a.db or root.parents[1] / "market_history.sqlite"
    o = run(root, db, a.events)
    a.out.write_text(render(o))
    a.out.with_name(a.out.stem + "_summary.json").write_text(
        json.dumps(o, indent=1, ensure_ascii=False, default=str) + "\n")
    print(f"wrote {a.out} manifest_sha256={o['manifest_sha256']} generated {datetime.now(ET).isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
