"""R1 shadow run of the exploratory day-trading Paper rules v1.0 (no orders).

  replay   replay DB sessions minute by minute through the shadow engine and diff against Paper's run_day
  live     run one US session live: Toss REST 1m bars at each minute + WebSocket trades, virtual fills only
  compare  compare a live shadow result with the Paper report of the same session (written next morning);
           --auto does every finished day not compared yet and writes its WebSocket measurement (summary.json)

It never calls an order endpoint and never subscribes to personal:order. Outputs go to
<out-root>/reports/daytrade_shadow/<date>/ (not tracked). `live` needs `websockets` and runs at most once
per session: live.lock stops a second process and an earlier START in events.jsonl stops a rerun.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sys
import threading
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
# replay checks logic, not inputs: fixed prior-day values per risk mode (live fetches the real ones)
REPLAY_EXTERNAL = {"normal": {"vix": {"value": 15.0}, "vix9d": {"value": 14.0}, "gex": {"value": 1.0e9}},
                   "high": {"vix": {"value": 35.0}, "vix9d": {"value": 30.0}, "gex": {"value": -1.0e9}},
                   "unknown": {}}
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
    summary = {"external": args.external, "days": 0, "compared": 0, "identical": 0, "paper_incomplete": 0, "mismatch": [], "situations": {}}
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
        out = paper_vs_shadow(day, data["regular"], data["qqq_pre"], data["prior"], REPLAY_EXTERNAL[args.external],
                              events,
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
        self._lock = threading.Lock()  # REST polls write from a worker thread, WebSocket trades from the loop

    def write(self, row: dict) -> None:
        line = json.dumps({"logged_utc": datetime.now(timezone.utc).isoformat(), **row},
                          ensure_ascii=False, default=str) + "\n"
        with self._lock:
            self._f.write(line)

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
            # page back only while the bars held do not reach 09:24 (late start or restart). A minute with
            # no trades leaves a hole that paging cannot fill, so holes alone never trigger it.
            cursor = result.get("nextBefore")
            for _ in range(3):
                if not cursor or bars.earliest(sym) <= R.GAP_PRE_CUTOFF - 1:
                    break
                page = _fetch(client, sym, before=cursor)
                bars.merge(sym, page.get("candles", []) or [], now)
                cursor = page.get("nextBefore")
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


def claim_day(out_dir: Path, smoke: bool):
    """Hold live.lock for the run; refuse a second live run of the same session.

    Two processes (launchd and a manual start) would interleave one events.jsonl, and a rerun after a
    crash cannot catch up a missed decision minute anyway. Smoke runs only need the lock. An unreadable
    events.jsonl line (a kill mid-write) counts as "maybe started".
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = (out_dir / "live.lock").open("w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunRefused(f"another live run holds {out_dir / 'live.lock'}") from None
        events = out_dir / "events.jsonl"
        if not smoke and events.exists():
            for line in events.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    raise RunRefused(f"{events} has an unreadable line; check it before starting again") from None
                if row.get("event") == "START" and not row.get("smoke"):
                    raise RunRefused(f"{out_dir.name} was already started at {row.get('logged_utc')}")
    except BaseException:
        lock.close()
        raise
    return lock


def check_start(day: str, args, now_et: datetime) -> None:
    """Refuse before touching the day folder: no session, a late start, or no WebSocket library.

    launchd fires a missed 21:50 KST slot when the Mac wakes; a start after the 09:25 ET pre-market
    cutoff would miss the gap source and decision minutes, so it is refused rather than half-run.
    """
    cal = load_events(args.events)
    if not events_for(cal, day)["known"]:
        raise RunRefused(f"{day} is outside the event calendar coverage {cal['coverage']}")
    if not is_trading_day(cal, day):
        raise RunRefused(f"{day} is not an NYSE trading day")
    if args.smoke_seconds:
        return
    if now_et.date().isoformat() != day:
        raise RunRefused(f"ET date is {now_et.date()}, not {day}")
    if _minute(now_et) >= R.GAP_PRE_CUTOFF:
        raise RunRefused(f"late start at {now_et:%H:%M} ET (must start before the 09:25 ET pre-market cutoff)")
    try:
        import websockets  # noqa: F401 - the run would silently fall back to REST only
    except ImportError:
        raise RunRefused("websockets is not installed in this Python (pip install -r requirements.txt)") from None


async def _live(day: str, args) -> int:
    from dotenv import load_dotenv

    load_dotenv(args.env_file)
    check_start(day, args, et_now())
    lock = claim_day(args.out_root / "reports" / "daytrade_shadow" / day, bool(args.smoke_seconds))
    try:
        return await _live_locked(day, args)
    finally:
        lock.close()


async def _live_locked(day: str, args) -> int:
    from scripts.measure_toss_realtime import _Log, _stream
    from src.config import load_settings
    from src.history.exchange_calendar import regular_close_minute
    from src.providers.toss_client import TossClient

    eng, meta = prepare(day, args)
    out_dir = Path(meta["out_dir"])
    if args.smoke_seconds:  # keep the session's WebSocket logs (and their measurement) free of smoke data
        out_dir = out_dir / f"smoke-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    journal = Journal(out_dir / "events.jsonl")
    ws_log = _Log(out_dir)
    close_t = regular_close_minute(date.fromisoformat(day))
    bars = LiveBars(day, close_t)
    journal.write({"event": "START", "day": day, "smoke": bool(args.smoke_seconds),
                   **{k: v for k, v in meta.items() if k != "external"},
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
                await asyncio.sleep(max(0.0, deadline - time.time()))
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
            try:
                result = eng.finish(bars.regular())
            except ValueError as exc:  # e.g. the 15:55 bar of a held symbol never arrived
                journal.write({"event": "FINISH_FAILED", "error": str(exc), "state": eng.base})
                print(f"FINISH_FAILED: {exc}", file=sys.stderr)
                finished = True
                return 1
            result["run_meta"] = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "R1_SHADOW",
                                  "equity_source": meta["equity_source"], "orders_sent": 0,
                                  "bar_source": "Toss REST 1m polled at each minute + 4 s"}
            (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
            journal.write({"event": "END", "situations": result["situations"], "trades": len(result["trades"]),
                           "day_pnl": result["pnl"]["day_pnl"]})
            print(json.dumps({"date": day, "situations": result["situations"], "trades": len(result["trades"]),
                              "day_pnl": round(result["pnl"]["day_pnl"], 4), "out": str(out_dir)}, ensure_ascii=False))
            finished = True
        finally:
            if not finished:  # do not wait for the session deadline on an error path
                ws_task.cancel()
                journal.write({"event": "ABORTED"})
            try:
                await ws_task  # the stream ends at its deadline (normal path) or on cancel
            except asyncio.CancelledError:
                if not ws_task.cancelled():  # this task itself was cancelled (Ctrl-C): not a clean day
                    journal.write({"event": "ABORTED"})
                    raise
            except Exception as exc:  # noqa: BLE001 - a WebSocket failure must not hide the day's result
                journal.write({"event": "WS_TASK_ERROR", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
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
            "equity_start": {"shadow": shadow.get("equity_start"), "paper": paper.get("equity_start"),
                             "shadow_source": (shadow.get("run_meta") or {}).get("equity_source")},
            "shadow": {"situations": shadow.get("situations"), "trades": st},
            "paper": {"situations": paper.get("situations"), "trades": pt},
            "input_diffs": diff(paper.get("inputs") or {}, shadow.get("inputs") or {}),
            "opening_diffs": diff(paper.get("opening") or {}, shadow.get("opening") or {})}


def write_measure(out_dir: Path, day: str) -> dict:
    """The `measure_toss_realtime.py analyze` report of a live day, as summary.json in its folder."""
    from scripts.measure_toss_realtime import analyze

    open_utc = (datetime.combine(date.fromisoformat(day), datetime.min.time(), NY)
                + timedelta(minutes=R.OPEN)).astimezone(timezone.utc)
    report = analyze(out_dir, open_utc, list(R.SYMBOLS))
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def run_compare_auto(args) -> int:
    """Measure and compare every finished shadow day that has no compare.json yet (launchd, after Paper)."""
    root = args.out_root / "reports" / "daytrade_shadow"
    worst, status = 0, {}
    folders = sorted(p for p in root.glob("????-??-??") if p.is_dir()) if root.exists() else []
    for folder in folders:
        day = folder.name
        if not (folder / "result.json").exists():
            status[day] = "NO_RESULT" if (folder / "events.jsonl").exists() else "EMPTY"
            continue
        if not (folder / "summary.json").exists():
            try:
                write_measure(folder, day)
            except Exception as exc:  # noqa: BLE001 - a measurement failure must not block the compare
                print(f"MEASURE_FAILED {day}: {type(exc).__name__}: {exc}", file=sys.stderr)
                worst = max(worst, 1)
        if (folder / "compare.json").exists():
            status[day] = "DONE"
            continue
        if not (args.out_root / "reports" / "daytrade_paper" / f"{day}.json").exists():
            status[day] = "PENDING_PAPER"
            continue
        args.date = day
        code = run_compare(args)
        status[day] = "PASS" if code == 0 else "FAIL"
        worst = max(worst, code)
    print(json.dumps({"auto": status}, ensure_ascii=False))
    return worst


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
    rp.add_argument("--external", choices=sorted(REPLAY_EXTERNAL), default="normal",
                    help="fixed prior-day VIX/VIX9D/GEX for both sides (risk mode NORMAL, HIGH or unknown)")
    lv = sub.add_parser("live")
    lv.add_argument("--date", default=None, help="US session date (default: today in ET)")
    lv.add_argument("--env-file", type=Path, default=ROOT / ".env")
    lv.add_argument("--smoke-seconds", type=float, default=None,
                    help="plumbing check: prepare, one REST poll and N seconds of WebSocket, then exit")
    cp = sub.add_parser("compare")
    which = cp.add_mutually_exclusive_group(required=True)
    which.add_argument("--date")
    which.add_argument("--auto", action="store_true",
                       help="every finished day without compare.json (also writes the WebSocket summary.json)")
    for p in (rp, lv, cp):
        p.add_argument("--db", type=Path, default=ROOT / "data" / "market_history.sqlite")
        p.add_argument("--events", type=Path, default=EVENTS)
        p.add_argument("--out-root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    args.out_root = args.out_root.resolve()
    if args.cmd == "compare" and args.auto:
        return run_compare_auto(args)
    return {"replay": run_replay, "live": run_live, "compare": run_compare}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
