"""
Toss Open API — 참조지표/벤치마크 심볼 제공 여부 탐침 (BLOCKER 3, 읽기 전용)

Rule Engine / Screener 가 쓰려는 참조 심볼들을 토스가 캔들로 주는지 확인한다.
- 섹터 벤치마크 ETF: SOXX, SMH, QQQ, SPY
- 변동성 지수: VIX (지수라 미지원 가능 → VIXY ETF 대체 확인)
- 반도체 개별주: NVDA, AMD, MU, AVGO
주문은 하지 않는다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
BASE = os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com").rstrip("/")
CID = os.getenv("TOSS_CLIENT_ID", "").strip()
CSEC = os.getenv("TOSS_CLIENT_SECRET", "").strip()

SYMBOLS = [
    ("SOXX", "섹터 ETF(반도체)"),
    ("SMH", "섹터 ETF(반도체)"),
    ("QQQ", "나스닥100 ETF"),
    ("SPY", "S&P500 ETF"),
    ("VIX", "변동성지수(원본)"),
    ("^VIX", "변동성지수(^표기)"),
    ("VIXY", "변동성 ETF(대체)"),
    ("NVDA", "반도체 개별주"),
    ("AMD", "반도체 개별주"),
    ("MU", "반도체 개별주"),
    ("AVGO", "반도체 개별주"),
]


def token(c: httpx.Client) -> str:
    r = c.post(f"{BASE}/oauth2/token",
               data={"grant_type": "client_credentials",
                     "client_id": CID, "client_secret": CSEC})
    r.raise_for_status()
    return r.json()["access_token"]


def check(c, tok, symbol):
    r = c.get(f"{BASE}/api/v1/candles",
              headers={"Authorization": f"Bearer {tok}"},
              params={"symbol": symbol, "interval": "1d", "count": 1})
    if r.status_code != 200:
        return f"✗ HTTP {r.status_code} ({r.text[:80]})"
    result = r.json().get("result", {})
    cs = result.get("candles", [])
    if not cs:
        return "— 데이터 없음"
    cur = cs[0].get("currency", "?")
    close = cs[0].get("closePrice", "?")
    return f"✓ close={close} {cur} ({cs[0].get('timestamp','')[:10]})"


def main():
    if not CID or not CSEC:
        raise SystemExit(".env 키가 비어있습니다.")
    print("=" * 60)
    print("Toss 참조 심볼 제공 여부 (읽기 전용, 1일봉 1개 조회)")
    print("=" * 60)
    with httpx.Client(timeout=httpx.Timeout(20.0)) as c:
        tok = token(c)
        print("토큰 OK\n")
        for sym, desc in SYMBOLS:
            print(f"  {sym:6} {desc:16} {check(c, tok, sym)}")
            time.sleep(0.4)
    print("\n" + "=" * 60)
    print("해석: VIX 원본지수가 ✗ 이면 VIXY 등 ETF 대체 or 별도 소스 필요.")
    print("      벤치마크 ETF(SOXX/SMH/QQQ)가 ✓ 이면 Screener 참조를 Toss 로 조달 가능.")
    print("=" * 60)


if __name__ == "__main__":
    main()
