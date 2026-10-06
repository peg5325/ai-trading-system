"""Read-only measurement of Toss realtime quotes against REST 1-minute candles (design R0, no orders).

`record` keeps one WebSocket connection on trade:<market> / orderbook:<market> (us or kr),
logs every trade and every orderbook arrival,
polls REST /api/v1/candles 1m (every 15 s by default), and drops the connection once on purpose to time the reconnect.
`analyze` turns the logs into the R0 numbers: per-minute WebSocket volume vs completed REST bar volume (missing %),
trade/orderbook gaps (open vs later), reconnect time, and whether in-progress REST bars are labelled by their end minute.

It never subscribes to personal:order and never calls an order endpoint. Logs hold quotes only (no token,
no account data); keep them outside git. Requires `websockets` (not a project dependency; pass it via PYTHONPATH).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
PING_EVERY_S = 60.0  # server drops the connection after 180 s without client traffic
MINUTE = timedelta(minutes=1)


# ---- pure helpers (tested) ----

def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def floor_minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def bar_start(label: datetime) -> datetime:
    """Toss 1m candles are labelled by the minute the bar ends; the bar covers [label - 1m, label)."""
    return label - MINUTE


def ws_minute_volumes(trades: list[dict]) -> dict[tuple[str, datetime], float]:
    """Sum WebSocket trade volume per (symbol, bar start) using the trade's own timestamp."""
    out: dict[tuple[str, datetime], float] = defaultdict(float)
    for t in trades:
        out[(t["symbol"], floor_minute(parse_ts(t["ts"])))] += float(t["volume"])
    return dict(out)


def completed_rest_bars(polls: list[dict], settle_s: float = 3.0) -> dict[tuple[str, datetime], dict]:
    """Volume of each REST bar once complete: the last poll fetched at least `settle_s` after its end label.

    Also reports whether the volume changed between the first and last completed observation (late revision).
    """
    seen: dict[tuple[str, datetime], list[tuple[datetime, float]]] = defaultdict(list)
    for p in polls:
        fetched = parse_ts(p["fetched"])
        for c in p["candles"]:
            label = parse_ts(c["timestamp"])
            if fetched >= label + timedelta(seconds=settle_s):
                seen[(p["symbol"], bar_start(label))].append((fetched, float(c["volume"])))
    out = {}
    for key, obs in seen.items():
        obs.sort()
        out[key] = {"volume": obs[-1][1], "first_volume": obs[0][1], "revised": obs[0][1] != obs[-1][1],
                    "observations": len(obs)}
    return out


def gap_stats(times: list[float]) -> dict:
    times = sorted(times)
    gaps = [b - a for a, b in pairwise(times)]
    if not gaps:
        return {"n": len(times), "gap_median_s": None, "gap_p95_s": None, "gap_max_s": None}
    ordered = sorted(gaps)
    return {"n": len(times), "gap_median_s": round(statistics.median(gaps), 3),
            "gap_p95_s": round(ordered[int(0.95 * (len(ordered) - 1))], 3), "gap_max_s": round(ordered[-1], 3)}


def stale_candidates(times: list[float], thresholds: tuple[float, ...] = (10.0, 15.0)) -> dict:
    """Connection-level silence: gaps between ANY frames (trades and orderbooks, all symbols).

    For each STALE candidate threshold, how many gaps exceeded it and the longest gap (design §4/§8).
    """
    times = sorted(times)
    gaps = [b - a for a, b in pairwise(times)]
    return {"frames": len(times), "gap_max_s": round(max(gaps), 3) if gaps else None,
            **{f"gaps_over_{int(th)}s": sum(1 for g in gaps if g > th) for th in thresholds}}


def capture_rows(ws: dict, rest: dict, symbols: list[str], start: datetime, end: datetime) -> list[dict]:
    """One row per (symbol, minute in [start, end)) with a completed REST bar: WS volume / REST volume."""
    rows = []
    minute = start
    while minute < end:
        for sym in symbols:
            bar = rest.get((sym, minute))
            if bar is None:
                continue
            w = ws.get((sym, minute), 0.0)
            r = bar["volume"]
            rows.append({"symbol": sym, "minute": minute.isoformat(), "ws_volume": w, "rest_volume": r,
                         "capture": (w / r) if r else None})
        minute += MINUTE
    return rows


def coverage(ws: dict, rest: dict, symbols: list[str], start: datetime, end: datetime) -> dict:
    """How many (symbol, minute) cells were actually compared, so a REST outage cannot pass as "0% missing"."""
    expected = compared = ws_without_rest = 0
    minute = start
    while minute < end:
        for sym in symbols:
            expected += 1
            bar = rest.get((sym, minute))
            if bar is not None and bar["volume"]:
                compared += 1
            elif ws.get((sym, minute), 0.0) > 0:
                ws_without_rest += 1
        minute += MINUTE
    return {"expected": expected, "compared": compared, "ws_volume_without_rest_bar": ws_without_rest}


def capture_summary(rows: list[dict]) -> dict:
    ws = sum(r["ws_volume"] for r in rows)
    rest = sum(r["rest_volume"] for r in rows)
    caps = sorted(r["capture"] for r in rows if r["capture"] is not None)
    if not caps:
        return {"minutes": len(rows), "ws_volume": ws, "rest_volume": rest, "missing_pct": None}
    return {"minutes": len(rows), "ws_volume": ws, "rest_volume": rest,
            "missing_pct": round(100 * (1 - ws / rest), 2) if rest else None,
            "capture_median": round(statistics.median(caps), 4),
            "capture_p05": round(caps[int(0.05 * (len(caps) - 1))], 4),
            "capture_min": round(caps[0], 4), "capture_max": round(caps[-1], 4),
            "capture_cv": round(statistics.pstdev(caps) / statistics.mean(caps), 4) if statistics.mean(caps) else None}


def label_check(polls: list[dict]) -> dict:
    """Is the newest REST bar the in-progress one, labelled by its end minute, and does it update live?"""
    newest_is_end_label = 0
    newest_total = 0
    in_progress: dict[tuple[str, datetime], list[tuple[datetime, float]]] = defaultdict(list)
    after_end: dict[tuple[str, datetime], set[float]] = defaultdict(set)
    for p in polls:
        if not p["candles"]:
            continue
        fetched = parse_ts(p["fetched"])
        labels = [parse_ts(c["timestamp"]) for c in p["candles"]]
        newest_total += 1
        if max(labels) == floor_minute(fetched) + MINUTE:
            newest_is_end_label += 1
        for c, label in zip(p["candles"], labels):
            if fetched < label:
                in_progress[(p["symbol"], label)].append((fetched, float(c["volume"])))
            else:
                after_end[(p["symbol"], label)].add(float(c["volume"]))
    multi = {k: sorted(v) for k, v in in_progress.items() if len(v) >= 2}
    grew = sum(1 for v in multi.values() if v[-1][1] > v[0][1])
    return {"polls": newest_total, "newest_label_is_current_end_minute": newest_is_end_label,
            "bars_seen_in_progress_twice": len(multi), "of_which_volume_grew": grew,
            "bars_seen_after_end": len(after_end),
            "of_which_changed_after_end": sum(1 for v in after_end.values() if len(v) > 1)}


# ---- recording ----

class _Log:
    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self._files = {name: (out_dir / f"{name}.jsonl").open("a", buffering=1)
                       for name in ("trades", "books", "control", "candles")}
        self._lock = threading.Lock()  # REST polls may write from a worker thread

    def write(self, name: str, row: dict) -> None:
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with self._lock:
            self._files[name].write(line)

    def close(self) -> None:
        for f in self._files.values():
            f.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _poll_rest(client, symbols: list[str], every_s: float, deadline: float, log: _Log) -> None:
    while time.time() < deadline:
        tick = time.time()
        for sym in symbols:
            try:
                result = await asyncio.to_thread(client.get_candles, sym, "1m", 5)
                log.write("candles", {"fetched": _now_iso(), "symbol": sym, "candles": [
                    {k: c.get(k) for k in ("timestamp", "openPrice", "closePrice", "volume")}
                    for c in result.get("candles", []) or []]})
            except Exception as exc:  # noqa: BLE001 - a measurement logs failures and keeps going
                log.write("control", {"t": _now_iso(), "event": "rest_error", "symbol": sym,
                                      "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        await asyncio.sleep(max(0.0, every_s - (time.time() - tick)))


async def _stream(token_fn, symbols: list[str], market: str, deadline: float, drop_at: float | None,
                  log: _Log, on_trade=None) -> None:
    """on_trade(symbol, price, recv_epoch) is called for every trade frame (used by the shadow run)."""
    import websockets

    decl = [{"type": f"{ch}:{market}", "codes": symbols} for ch in ("trade", "orderbook")]
    backoff = 1.0
    dropped = drop_at is None
    while time.time() < deadline:
        planned_drop = False
        try:
            log.write("control", {"t": _now_iso(), "event": "connecting"})
            async with websockets.connect(WS_URL, additional_headers={"Authorization": f"Bearer {token_fn()}"},
                                          ping_interval=None) as ws:
                await ws.send(json.dumps(decl))
                last_ping = time.time()
                while time.time() < deadline:
                    if not dropped and time.time() >= drop_at:
                        dropped = planned_drop = True
                        log.write("control", {"t": _now_iso(), "event": "planned_drop"})
                        break
                    if time.time() - last_ping >= PING_EVERY_S:
                        await ws.send("PING")
                        last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    recv = time.time()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        log.write("control", {"t": _now_iso(), "event": "raw", "raw": str(raw)[:100]})
                        continue
                    if msg.get("type") != "message":
                        log.write("control", {"t": _now_iso(), "event": "control", "msg": msg})
                        continue
                    backoff = 1.0
                    channel, _, sym = str(msg.get("topic", "")).rpartition(":")
                    data = msg.get("data") or {}
                    if channel.startswith("trade"):
                        log.write("trades", {"recv": recv, "symbol": sym, "ts": data.get("timestamp"),
                                             "price": data.get("price"), "volume": data.get("volume")})
                        if on_trade is not None and data.get("price") is not None:
                            on_trade(sym, float(data["price"]), recv)
                    else:
                        asks, bids = data.get("asks") or [{}], data.get("bids") or [{}]
                        log.write("books", {"recv": recv, "symbol": sym, "ts": data.get("timestamp"),
                                            "ask": asks[0].get("price"), "bid": bids[0].get("price")})
            log.write("control", {"t": _now_iso(), "event": "closed", "planned": planned_drop})
        except Exception as exc:  # noqa: BLE001 - reconnect on any transport failure, log it
            log.write("control", {"t": _now_iso(), "event": "ws_error",
                                  "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)


async def record(symbols: list[str], market: str, seconds: float, drop_after_s: float | None, poll_every_s: float,
                 out_dir: Path, env_file: Path) -> None:
    from dotenv import load_dotenv

    load_dotenv(env_file)
    from src.config import load_settings
    from src.providers.toss_client import TossClient

    log = _Log(out_dir)
    start = time.time()
    deadline = start + seconds
    drop_at = start + drop_after_s if drop_after_s is not None else None
    log.write("control", {"t": _now_iso(), "event": "start", "market": market, "symbols": symbols, "seconds": seconds,
                          "drop_after_s": drop_after_s, "poll_every_s": poll_every_s})
    with TossClient(load_settings()) as client:
        try:
            await asyncio.gather(_stream(client._access_token, symbols, market, deadline, drop_at, log),
                                 _poll_rest(client, symbols, poll_every_s, deadline, log))
        finally:
            log.write("control", {"t": _now_iso(), "event": "end"})
            log.close()


# ---- analysis ----

def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _gap_by_symbol(rows: list[dict], lo: float, hi: float) -> dict:
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if lo <= r["recv"] < hi:
            by[r["symbol"]].append(r["recv"])
    return {sym: gap_stats(ts) for sym, ts in sorted(by.items())}


def _reconnect(control: list[dict], trades: list[dict]) -> dict | None:
    drop = next((c for c in control if c["event"] == "planned_drop"), None)
    if drop is None:
        return None
    t0 = parse_ts(drop["t"])
    sub = next((c for c in control if c["event"] == "control" and parse_ts(c["t"]) > t0
                and c["msg"].get("type") == "subscriptions"), None)
    first = min((r["recv"] for r in trades if r["recv"] > t0.timestamp()), default=None)
    last_before = max((r["recv"] for r in trades if r["recv"] <= t0.timestamp()), default=None)
    return {"dropped_at": drop["t"], "resubscribed_after_s": round(parse_ts(sub["t"]).timestamp() - t0.timestamp(), 3)
            if sub else None, "rejected": sub["msg"].get("rejected") if sub else None,
            "first_trade_after_s": round(first - t0.timestamp(), 3) if first else None,
            "trade_blackout_s": round(first - last_before, 3) if first and last_before else None,
            "drop_minute": floor_minute(t0).isoformat()}


def analyze(out_dir: Path, open_utc: datetime, symbols: list[str]) -> dict:
    trades, books = _read(out_dir / "trades.jsonl"), _read(out_dir / "books.jsonl")
    control, polls = _read(out_dir / "control.jsonl"), _read(out_dir / "candles.jsonl")
    ws = ws_minute_volumes(trades)
    rest = completed_rest_bars(polls)
    first_trade = min(floor_minute(parse_ts(t["ts"])) for t in trades)
    last_trade = max(floor_minute(parse_ts(t["ts"])) for t in trades)
    # skip the first and last partially recorded minutes
    start, end = max(first_trade + MINUTE, open_utc - 2 * MINUTE), last_trade
    early_end = open_utc + 5 * MINUTE
    rows = capture_rows(ws, rest, symbols, start, end)
    reconnect = _reconnect(control, trades)

    def phase(lo: datetime, hi: datetime, exclude: str | None = None) -> dict:
        sel = [r for r in rows if lo.isoformat() <= r["minute"] < hi.isoformat() and r["minute"] != exclude]
        return {sym: capture_summary([r for r in sel if r["symbol"] == sym]) for sym in symbols} | {
            "ALL": capture_summary(sel)}

    drop_minute = reconnect["drop_minute"] if reconnect else None
    lag = [r["recv"] - parse_ts(r["ts"]).timestamp() for r in trades if r.get("ts")]
    lag.sort()
    o, e5 = open_utc.timestamp(), early_end.timestamp()
    return {
        "window_utc": [start.isoformat(), end.isoformat()], "open_utc": open_utc.isoformat(),
        "counts": {"trades": len(trades), "books": len(books), "polls": len(polls),
                   "rest_errors": sum(1 for c in control if c["event"] == "rest_error"),
                   "ws_errors": [c for c in control if c["event"] == "ws_error"],
                   "rejected": [c["msg"].get("rejected") for c in control
                                if c["event"] == "control" and c["msg"].get("type") == "subscriptions"]},
        "missing": {"pre_open": phase(start, open_utc), "first5": phase(open_utc, early_end),
                    "after5_excl_drop_minute": phase(early_end, end, drop_minute),
                    "drop_minute": phase(datetime.fromisoformat(drop_minute), datetime.fromisoformat(drop_minute)
                                         + MINUTE) if drop_minute else None},
        "rest_revised_after_complete": sum(1 for b in rest.values() if b["revised"]),
        "trade_gaps": {"first5": _gap_by_symbol(trades, o, e5), "after5": _gap_by_symbol(trades, e5, 1e12)},
        "book_gaps": {"first5": _gap_by_symbol(books, o, e5), "after5": _gap_by_symbol(books, e5, 1e12)},
        "stale_any_frame": {
            "regular": stale_candidates([r["recv"] for r in trades + books if r["recv"] >= o]),
            "trades_only": stale_candidates([r["recv"] for r in trades if r["recv"] >= o])},
        # trade timestamps are whole seconds, so recv - ts is an upper bound with up to +1 s truncation
        "trade_lag_upper_s": {"median": round(statistics.median(lag), 3),
                              "p95": round(lag[int(0.95 * (len(lag) - 1))], 3),
                              "max": round(lag[-1], 3)} if lag else None,
        "reconnect": reconnect,
        "labels": label_check(polls),
        "coverage": coverage(ws, rest, symbols, start, end),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--env-file", type=Path, default=ROOT / ".env", help="file holding TOSS_CLIENT_ID/SECRET")
    rec.add_argument("--market", choices=("us", "kr"), default="us")
    rec.add_argument("--symbols", default="QQQ,TQQQ,SQQQ,SPY")
    rec.add_argument("--seconds", type=float, default=2700.0)
    rec.add_argument("--drop-after-s", type=float, default=None, help="close the socket once after this many seconds")
    rec.add_argument("--poll-every-s", type=float, default=15.0)
    rec.add_argument("--out-dir", type=Path, required=True, help="log directory (keep outside git)")
    ana = sub.add_parser("analyze")
    ana.add_argument("--out-dir", type=Path, required=True)
    ana.add_argument("--open-utc", required=True, help="regular-session open, e.g. 2026-10-05T13:30:00+00:00")
    ana.add_argument("--symbols", default="QQQ,TQQQ,SQQQ,SPY")
    args = ap.parse_args()

    if args.cmd == "record":
        asyncio.run(record(args.symbols.split(","), args.market, args.seconds, args.drop_after_s, args.poll_every_s,
                           args.out_dir, args.env_file))
        return 0
    report = analyze(args.out_dir, parse_ts(args.open_utc), args.symbols.split(","))
    (args.out_dir / "summary.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
