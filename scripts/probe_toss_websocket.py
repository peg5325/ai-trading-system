"""Read-only probe of the Toss Open API realtime WebSocket (quotes only, no orders).

Connects to wss://openapi-ws.tossinvest.com/ws/v1, subscribes trade:us / orderbook:us for a few
symbols, and records message counts, inter-arrival gaps and one sample payload per topic.
It never subscribes to personal:order and never calls an order endpoint.

Requires the `websockets` package (not a project dependency; pass it via PYTHONPATH).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
PING_EVERY_S = 60.0  # server drops the connection after 180 s without client traffic


def _token(env_file: Path) -> str:
    load_dotenv(env_file)
    from src.config import load_settings
    from src.providers.toss_client import TossClient

    with TossClient(load_settings()) as client:
        return client._access_token()


def _summary(stats: dict) -> dict:
    out = {}
    for topic, s in sorted(stats.items()):
        gaps = s["gaps"]
        out[topic] = {
            "messages": s["n"],
            "gap_median_s": round(statistics.median(gaps), 4) if gaps else None,
            "gap_p95_s": round(sorted(gaps)[int(0.95 * (len(gaps) - 1))], 4) if gaps else None,
            "gap_max_s": round(max(gaps), 4) if gaps else None,
            "sample": s["sample"],
        }
    return out


async def probe(token: str, symbols: list[str], channels: list[str], seconds: float) -> dict:
    import websockets

    decl = [{"type": f"{ch}:us", "codes": symbols} for ch in channels]
    stats: dict = {}
    events: list = []
    started = time.monotonic()
    async with websockets.connect(WS_URL, additional_headers={"Authorization": f"Bearer {token}"}) as ws:
        await ws.send(json.dumps(decl))
        last_ping = time.monotonic()
        while time.monotonic() - started < seconds:
            if time.monotonic() - last_ping >= PING_EVERY_S:
                await ws.send("PING")
                last_ping = time.monotonic()
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            now = time.monotonic()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                events.append({"t": round(now - started, 3), "raw": str(raw)[:200]})
                continue
            if msg.get("type") != "message":
                events.append({"t": round(now - started, 3), "msg": msg})
                continue
            s = stats.setdefault(msg.get("topic", "?"), {"n": 0, "last": None, "gaps": [], "sample": None})
            if s["last"] is not None:
                s["gaps"].append(now - s["last"])
            s["last"] = now
            s["n"] += 1
            if s["sample"] is None:
                s["sample"] = msg.get("data")
    return {"control_events": events[:50], "topics": _summary(stats)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--env-file", type=Path, default=ROOT / ".env", help="file holding TOSS_CLIENT_ID/SECRET")
    ap.add_argument("--symbols", default="QQQ,TQQQ,SQQQ,SPY")
    ap.add_argument("--channels", default="trade,orderbook")
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--out", type=Path, required=True, help="JSON report path (keep outside git)")
    args = ap.parse_args()

    started = datetime.now(timezone.utc).isoformat()
    report: dict = {"started_utc": started, "url": WS_URL, "symbols": args.symbols.split(","),
                    "channels": args.channels.split(","), "seconds": args.seconds}
    try:
        token = _token(args.env_file)
        report.update(asyncio.run(probe(token, report["symbols"], report["channels"], args.seconds)))
        report["status"] = "OK"
    except Exception as exc:  # noqa: BLE001 - a probe records any failure (without secrets) instead of crashing
        report["status"] = "ERROR"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    report["ended_utc"] = datetime.now(timezone.utc).isoformat()
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": report["status"], "error": report.get("error"),
                      "topics": {k: v["messages"] for k, v in report.get("topics", {}).items()}}))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
