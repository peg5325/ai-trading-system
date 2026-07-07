"""
Toss Open API — 과거 분봉 확보 범위 탐침 (BLOCKER 2, 읽기 전용)

"SOXL 1분봉/일봉이 얼마나 과거까지 있는가"를 확인한다.
백테스트에 최소 1~3년 분봉이 필요하므로, 여러 과거 시점(before)에 데이터가
존재하는지 찔러본다. 주문은 하지 않는다.

실행:
  cd ai_trading_system
  ./.venv/bin/python spikes/toss_history_depth.py
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE = os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com").rstrip("/")
CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "").strip()
SYMBOL = "SOXL"
KST = timezone(timedelta(hours=9))


def token(client: httpx.Client) -> str:
    r = client.post(
        f"{BASE}/oauth2/token",
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def candles(client, tok, interval, count=5, before=None):
    params = {"symbol": SYMBOL, "interval": interval, "count": count}
    if before:
        params["before"] = before
    r = client.get(f"{BASE}/api/v1/candles",
                   headers={"Authorization": f"Bearer {tok}"}, params=params)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    result = r.json().get("result", {})
    return result.get("candles", []), result.get("nextBefore")


def probe_checkpoints(client, tok, interval, checkpoints):
    print(f"\n=== interval={interval} : 과거 시점별 데이터 존재 확인 ===")
    for label, before in checkpoints:
        cs, nxt = candles(client, tok, interval, count=3, before=before)
        if cs is None:
            print(f"  {label:14} before={before}  ✗ {nxt}")
        elif not cs:
            print(f"  {label:14} before={before}  — 데이터 없음")
        else:
            newest = cs[0].get("timestamp")
            oldest = cs[-1].get("timestamp")
            print(f"  {label:14} before={before}  ✓ {len(cs)}개  "
                  f"[{oldest} ~ {newest}]")
        time.sleep(0.4)


def measure_1m_page_span(client, tok):
    """1분봉 한 페이지(count=200)가 실제로 몇 분/며칠을 덮는지 측정."""
    print("\n=== interval=1m : 한 페이지(200개) 커버 범위 ===")
    cs, nxt = candles(client, tok, "1m", count=200)
    if not cs:
        print("  ✗ 데이터 없음")
        return
    print(f"  200개 요청 → {len(cs)}개 수신")
    print(f"  최신: {cs[0].get('timestamp')}")
    print(f"  최古: {cs[-1].get('timestamp')}")
    print(f"  nextBefore(다음 페이지 시작): {nxt}")


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit(".env 키가 비어있습니다.")

    now = datetime.now(KST)
    def iso(days):
        return (now - timedelta(days=days)).replace(microsecond=0).isoformat()

    cps = [
        ("최근",       None),
        ("3개월 전",   iso(90)),
        ("6개월 전",   iso(180)),
        ("1년 전",     iso(365)),
        ("2년 전",     iso(730)),
        ("3년 전",     iso(1095)),
    ]

    print("=" * 60)
    print(f"Toss 과거 데이터 범위 탐침 — {SYMBOL} (읽기 전용)")
    print("=" * 60)
    with httpx.Client(timeout=httpx.Timeout(20.0)) as client:
        tok = token(client)
        print("토큰 OK")
        measure_1m_page_span(client, tok)
        probe_checkpoints(client, tok, "1m", cps)
        probe_checkpoints(client, tok, "1d", cps)
    print("\n" + "=" * 60)
    print("해석: 1분봉이 1~3년 전 시점에도 ✓ 이면 백테스트 데이터를 Toss 로 조달 가능.")
    print("      얕으면 과거 데이터는 KIS/Polygon 필요 (설계상 이미 대비됨).")
    print("=" * 60)


if __name__ == "__main__":
    main()
