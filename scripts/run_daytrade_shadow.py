"""R1 shadow run of the exploratory day-trading Paper rules v1.0 (no orders).

  replay   replay DB sessions minute by minute through the shadow engine and diff against Paper's run_day
  live     run one US session live: Toss REST 1m bars at each minute + WebSocket trades, virtual fills only
  compare  compare a live shadow result with the Paper report of the same session (written next morning)

It never calls an order endpoint and never subscribes to personal:order. Outputs go to
<out-root>/reports/daytrade_shadow/<date>/ (not tracked). Requires `websockets` for `live` (not a
project dependency; pass it via PYTHONPATH).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_paper import rules as R
from src.daytrade_paper.external import (
    events_for,
    fetch_external,
    is_trading_day,
    load_events,
    prev_trading_day,
)
from src.daytrade_paper.store import (
    RunRefused,
    check_chain,
    load_bars,
    load_ledger,
)
from src.daytrade_shadow.bars import NY, LiveBars
from src.daytrade_shadow.engine import ShadowDay
from src.daytrade_shadow.replay import diff, paper_vs_shadow

EVENTS = ROOT / "config" / "daytrade_events_v1.yaml"
REPLAY_EXTERNAL = {"vix": {"value": 15.0}, "vix9d": {"value": 14.0}, "gex": {"value": 1.0e9}}
NO_EVENTS = {"known": True, "types": [], "fomc": False, "early_close": False, "indicator": False, "items": []}
POLL_DELAY_S = 4.0  # seconds after each minute boundary: the finished bar is final, the new one has its open
FETCH_COUNT = 200


# ---------------------------------------------------------------- replay

def _db_days(db: Path, start: str, end: str) -> list[str]:
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT substr(ts_exchange,1,10) FROM candle_history WHERE symbol='QQQ' "
                           "AND interval='1m' AND session='REGULAR' AND substr(ts_exchange,1,10) BETWEEN ? AND ? "
                           "ORDER BY 1", (start, end)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def run_replay(args) -> int:
    cal = load_events(args.events)
    days = _db_days(args.db, args.start, args.end)
    summary = {"days": 0, "compared": 0, "identical": 0, "paper_incomplete": 0, "mismatch": [], "situations": {}}
    prev: str | None = None
    for day in days:
        data = load_bars(args.db, day)
        expected_prev = data["prev_session"]
        if expected_prev is None or prev is None:
            prev = day
            continue
        ev = events_for(cal, day)
        events = ev if ev["known"] else NO_EVENTS
        prev_early = events_for(cal, expected_prev)["early_close"] if events_for(cal, expected_prev)["known"] else False
        out = paper_vs_shadow(day, data["regular"], data["qqq_pre"], data["prior"], REPLAY_EXTERNAL, events,
                              R.START_EQUITY, prev_times=data["prev_times"], expected_prev=expected_prev,
                              prev_early_close=prev_early)
        summary["days"] += 1
        if not out["paper_quality_ok"]:
            summary["paper_incomplete"] += 1
        else:
            summary["compared"] += 1
            key = "+".join(out["paper_situations"])
            summary["situations"][key] = summary["situations"].get(key, 0) + 1
            if out["diffs"]:
                summary["mismatch"].append({"date": day, "diffs": out["diffs"][:10]})
            else:
                summary["identical"] += 1
        prev = day
        if args.verbose:
            print(day, out["paper_situations"], "OK" if not out["diffs"] else f"DIFF {len(out['diffs'])}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    return 0 if not summary["mismatch"] else 1


# ---------------------------------------------------------------- live

def et_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(NY)


def _minute(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


class Journal:
    """Append-only JSONL of shadow decisions (events.jsonl) next to the WebSocket logs."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", buffering=1)

    def write(self, row: dict) -> None:
        self._f.write(json.dumps({"logged_utc": datetime.now(timezone.utc).isoformat(), **row},
                                 ensure_ascii=False, default=str) + "\n")

    def close(self) -> None:
        self._f.close()


def _fetch(client, symbol: str, before: str | None = None) -> dict:
    """GET candles 1m with up to 3 retries on 429/5xx (REST limits are shared with the collector)."""
    import httpx

    for attempt in range(4):
        try:
            return client.get_candles(symbol, "1m", FETCH_COUNT, before=before)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if attempt == 3 or not (code == 429 or code >= 500):
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _poll(client, bars: LiveBars, now: int, journal: Journal, candle_log) -> None:
    for sym in R.SYMBOLS:
        try:
            result = _fetch(client, sym)
            candles = result.get("candles", []) or []
            bars.merge(sym, candles, now)
            # backfill once if the session so far has holes (late start, restart)
            if bars.missing(sym, R.OPEN, min(now, bars.close_t)) and result.get("nextBefore"):
                cursor = result["nextBefore"]
                for _ in range(3):
                    page = _fetch(client, sym, before=cursor)
                    bars.merge(sym, page.get("candles", []) or [], now)
                    cursor = page.get("nextBefore")
                    if not cursor or not bars.missing(sym, R.OPEN, min(now, bars.close_t)):
                        break
            if candle_log is not None:
                candle_log.write("candles", {"fetched": datetime.now(timezone.utc).isoformat(), "symbol": sym,
                                             "candles": [{k: c.get(k) for k in ("timestamp", "openPrice",
                                                                                "closePrice", "volume")}
                                                         for c in candles[:3]]})
        except Exception as exc:  # noqa: BLE001 - a shadow run records failures and keeps going
            journal.write({"event": "REST_ERROR", "symbol": sym, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})


def prepare(day: str, args) -> tuple[ShadowDay, dict]:
    cal = load_events(args.events)
    events = events_for(cal, day)
    if not events["known"]:
        raise RunRefused(f"{day} is outside the event calendar coverage {cal['coverage']}")
    if not is_trading_day(cal, day):
        raise RunRefused(f"{day} is not an NYSE trading day")
    prev_day = prev_trading_day(cal, day)
    prev_early = events_for(cal, prev_day)["early_close"]
    data = load_bars(args.db, day)
    ledger = load_ledger(args.out_root / "reports" / "daytrade_paper" / "ledger.json")
    meta = {"prev_day": prev_day, "db_prev_session": data["prev_session"]}
    try:
        equity = check_chain(ledger, day, prev_day)
        meta["equity_source"] = f"Paper ledger (last day {prev_day})"
    except RunRefused as exc:
        last = ledger["days"][-1] if ledger["days"] else None
        equity = float(last["equity_end"]) if last else R.START_EQUITY
        meta["equity_source"] = f"UNVERIFIED: {exc}"
    out_dir = args.out_root / "reports" / "daytrade_shadow" / day
    external = fetch_external(day, out_dir / "external")
    eng = ShadowDay(day, data["prior"], external, events, equity, prev_times=data["prev_times"],
                    expected_prev=prev_day, prev_early_close=prev_early)
    meta.update({"equity_start": equity, "events": events, "external": external, "out_dir": str(out_dir)})
    return eng, meta


async def _live(day: str, args) -> int:
    from dotenv import load_dotenv

    load_dotenv(args.env_file)
    from scripts.measure_toss_realtime import _Log, _stream
    from src.config import load_settings
    from src.history.exchange_calendar import regular_close_minute
    from src.providers.toss_client import TossClient

    eng, meta = prepare(day, args)
    out_dir = Path(meta["out_dir"])
    journal = Journal(out_dir / "events.jsonl")
    ws_log = _Log(out_dir)
    close_t = regular_close_minute(date.fromisoformat(day))
    bars = LiveBars(day, close_t)
    journal.write({"event": "START", "day": day, **{k: v for k, v in meta.items() if k != "external"},
                   "external_values": {k: (v or {}).get("value") for k, v in meta["external"].items()},
                   "orders_sent": 0})
    touched: set[str] = set()

    def on_trade(sym: str, price: float, recv: float) -> None:
        for tr in eng.open_positions():
            key = f"{tr['kind']}:{tr['symbol']}"
            if tr["symbol"] == sym and price <= tr["stop"] and key not in touched:
                touched.add(key)
                journal.write({"event": "WS_STOP_TOUCH", "kind": tr["kind"], "symbol": sym, "price": price,
                               "stop": tr["stop"], "recv_utc": datetime.fromtimestamp(recv, timezone.utc)})

    session_end = datetime.combine(date.fromisoformat(day), datetime.min.time(), NY) + timedelta(minutes=close_t + 2)
    deadline = time.time() + args.smoke_seconds if args.smoke_seconds else session_end.timestamp()
    with TossClient(load_settings()) as client:
        ws_task = asyncio.create_task(_stream(client._access_token, list(R.SYMBOLS), "us", deadline, None,
                                              ws_log, on_trade))
        finished = False
        try:
            if args.smoke_seconds:  # plumbing check outside the session: one poll + WebSocket, no decisions
                now = _minute(et_now())
                await asyncio.to_thread(_poll, client, bars, now, journal, ws_log)
                await ws_task
                finished = True
                n = {s: len(v) for s, v in bars.bars.items()}
                journal.write({"event": "SMOKE_END", "bars_merged": n, "in_progress": sorted(bars.in_progress)})
                print(json.dumps({"smoke": "OK", "bars_merged": n, "out": str(out_dir)}))
                return 0
            while True:
                now_dt = et_now()
                if now_dt.date().isoformat() != day:
                    raise RunRefused(f"ET date is {now_dt.date()}, not {day}")
                nxt = now_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
                await asyncio.sleep(max(0.0, (nxt - now_dt).total_seconds() + POLL_DELAY_S))
                now = _minute(nxt)
                if now < R.GAP_PRE_CUTOFF:
                    continue
                await asyncio.to_thread(_poll, client, bars, now, journal, ws_log)
                if now >= R.OPEN:
                    for ev in eng.step(now, bars.complete(now), bars.opens(now), bars.qqq_pre()):
                        journal.write(ev)
                if now > close_t:
                    break
            result = eng.finish(bars.regular())
            result["run_meta"] = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "R1_SHADOW",
                                  "equity_source": meta["equity_source"], "orders_sent": 0,
                                  "bar_source": "Toss REST 1m polled at each minute + 4 s"}
            (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
            journal.write({"event": "END", "situations": result["situations"], "trades": len(result["trades"]),
                           "day_pnl": result["pnl"]["day_pnl"]})
            print(json.dumps({"date": day, "situations": result["situations"], "trades": len(result["trades"]),
                              "day_pnl": round(result["pnl"]["day_pnl"], 4), "out": str(out_dir)}, ensure_ascii=False))
            await ws_task
            finished = True
        finally:
            if not finished:  # do not wait for the session deadline on an error path
                ws_task.cancel()
                journal.write({"event": "ABORTED"})
            ws_log.close()
            journal.close()
    return 0


def run_live(args) -> int:
    day = args.date or et_now().date().isoformat()
    try:
        return asyncio.run(_live(day, args))
    except RunRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------- compare

KEY_TRADE_FIELDS = ("kind", "symbol", "entry_time", "entry_fill", "notional", "stop", "exit_time", "exit_reason",
                    "pnl")


def compare(shadow: dict, paper: dict) -> dict:
    """R1 pass check (design §6): same situations, entry time and size; everything else reported."""
    def trades(r):
        return [{k: t.get(k) for k in KEY_TRADE_FIELDS} for t in r.get("trades", [])]

    st, pt = trades(shadow), trades(paper)
    same_sit = shadow.get("situations") == paper.get("situations")
    same_entries = [(t["kind"], t["symbol"], t["entry_time"]) for t in st] == \
                   [(t["kind"], t["symbol"], t["entry_time"]) for t in pt]
    size_close = len(st) == len(pt) and all(abs(a["notional"] - b["notional"]) <= 0.01 * max(b["notional"], 1e-9)
                                            for a, b in zip(st, pt))
    return {"date": paper.get("date"), "pass": same_sit and same_entries and size_close,
            "same_situations": same_sit, "same_entries": same_entries, "size_within_1pct": size_close,
            "shadow": {"situations": shadow.get("situations"), "trades": st},
            "paper": {"situations": paper.get("situations"), "trades": pt},
            "input_diffs": diff(paper.get("inputs") or {}, shadow.get("inputs") or {}),
            "opening_diffs": diff(paper.get("opening") or {}, shadow.get("opening") or {})}


def run_compare(args) -> int:
    day = args.date
    sp = args.out_root / "reports" / "daytrade_shadow" / day / "result.json"
    pp = args.out_root / "reports" / "daytrade_paper" / f"{day}.json"
    for p in (sp, pp):
        if not p.exists():
            print(f"MISSING: {p}", file=sys.stderr)
            return 2
    out = compare(json.loads(sp.read_text()), json.loads(pp.read_text()))
    (sp.parent / "compare.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({k: out[k] for k in ("date", "pass", "same_situations", "same_entries", "size_within_1pct")}
                     | {"input_diffs": len(out["input_diffs"]), "opening_diffs": len(out["opening_diffs"])},
                     ensure_ascii=False))
    return 0 if out["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("--start", default="2021-01-01")
    rp.add_argument("--end", default="2099-12-31")
    rp.add_argument("--out", type=Path, default=None)
    rp.add_argument("--verbose", action="store_true")
    lv = sub.add_parser("live")
    lv.add_argument("--date", default=None, help="US session date (default: today in ET)")
    lv.add_argument("--env-file", type=Path, default=ROOT / ".env")
    lv.add_argument("--smoke-seconds", type=float, default=None,
                    help="plumbing check: prepare, one REST poll and N seconds of WebSocket, then exit")
    cp = sub.add_parser("compare")
    cp.add_argument("--date", required=True)
    for p in (rp, lv, cp):
        p.add_argument("--db", type=Path, default=ROOT / "data" / "market_history.sqlite")
        p.add_argument("--events", type=Path, default=EVENTS)
        p.add_argument("--out-root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    args.out_root = args.out_root.resolve()
    return {"replay": run_replay, "live": run_live, "compare": run_compare}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
