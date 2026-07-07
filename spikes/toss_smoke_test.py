"""
Toss Securities Open API — 읽기 전용 스모크 테스트 (BLOCKER 1 검증)

주문(매매)은 절대 하지 않는다. 인증 + 조회만으로 다음을 확인한다:
  1) OAuth2 토큰 발급이 되는가
  2) 계좌 조회가 되는가 (accountSeq 확보)
  3) 보유 종목 조회가 되는가
  4) 미국주식(SOXL) 1분봉 데이터가 나오는가  <- 우리 설계의 핵심 미확인 항목

실행:
  cd ai_trading_system
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  python spikes/toss_smoke_test.py

.env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 채워져 있어야 한다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---- config -----------------------------------------------------------------
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

BASE = os.getenv("TOSS_API_BASE", "https://openapi.tossinvest.com").rstrip("/")
CLIENT_ID = os.getenv("TOSS_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TOSS_CLIENT_SECRET", "").strip()

# 미국주식 분봉 확인용 (대표 검증 종목)
TEST_US_SYMBOL = "SOXL"

TIMEOUT = httpx.Timeout(15.0)


# ---- helpers ----------------------------------------------------------------
def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def preview(obj, limit: int = 600) -> str:
    s = json.dumps(obj, ensure_ascii=False, indent=2)
    return s if len(s) <= limit else s[:limit] + "\n  … (truncated)"


def die(msg: str) -> None:
    print(f"\n\033[31mSMOKE TEST FAILED\033[0m — {msg}")
    sys.exit(1)


# ---- steps ------------------------------------------------------------------
def get_token(client: httpx.Client) -> str:
    step(1, "OAuth2 토큰 발급  POST /oauth2/token")
    resp = client.post(
        f"{BASE}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
        die("토큰 발급 실패 — CLIENT_ID/SECRET 또는 키 승인 상태를 확인하세요.")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        fail(f"응답에 access_token 없음: {preview(data)}")
        die("토큰 응답 형식 예상과 다름.")
    ok(f"access_token 확보 (type={data.get('token_type')}, expires_in={data.get('expires_in')})")
    return token


def get_accounts(client: httpx.Client, token: str):
    step(2, "계좌 조회  GET /api/v1/accounts")
    resp = client.get(
        f"{BASE}/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
        die("계좌 조회 실패.")
    data = resp.json()
    accounts = data.get("result", data)
    ok("계좌 조회 성공")
    print("   " + preview(accounts).replace("\n", "\n   "))
    # accountSeq 추출 (형식이 다를 수 있어 방어적으로)
    seq = None
    if isinstance(accounts, list) and accounts:
        seq = accounts[0].get("accountSeq")
    if seq is None:
        fail("accountSeq 를 자동 추출하지 못함 — 위 응답 구조를 확인하세요.")
    else:
        ok(f"accountSeq = {seq} 사용")
    return seq


def get_holdings(client: httpx.Client, token: str, account_seq) -> None:
    step(3, "보유 종목 조회  GET /api/v1/holdings")
    if account_seq is None:
        fail("accountSeq 없음 → 건너뜀")
        return
    resp = client.get(
        f"{BASE}/api/v1/holdings",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tossinvest-Account": str(account_seq),
        },
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return
    ok("보유 종목 조회 성공")
    print("   " + preview(resp.json()).replace("\n", "\n   "))


def get_us_candles(client: httpx.Client, token: str) -> None:
    step(4, f"미국주식 1분봉 조회  GET /api/v1/candles  (symbol={TEST_US_SYMBOL})")
    resp = client.get(
        f"{BASE}/api/v1/candles",
        headers={"Authorization": f"Bearer {token}"},
        params={"symbol": TEST_US_SYMBOL, "interval": "1m", "count": 5},
    )
    if resp.status_code != 200:
        fail(f"HTTP {resp.status_code}: {resp.text[:300]}")
        fail("→ 미국주식 분봉 데이터 확인 실패. (심볼 표기/시장 파라미터가 다를 수 있음)")
        return
    data = resp.json()
    result = data.get("result", data)
    candles = result.get("candles") if isinstance(result, dict) else result
    if candles:
        ok(f"{TEST_US_SYMBOL} 1분봉 {len(candles)}개 수신")
        print("   " + preview(candles).replace("\n", "\n   "))
    else:
        fail(f"candles 비어있음: {preview(data)}")


def main() -> None:
    print("=" * 60)
    print("Toss Open API 스모크 테스트 (읽기 전용 · 주문 없음)")
    print("=" * 60)
    print(f"BASE = {BASE}")

    if not CLIENT_ID or not CLIENT_SECRET:
        die(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.")
    print(f"CLIENT_ID = {CLIENT_ID[:6]}…{CLIENT_ID[-3:]} (masked)")

    with httpx.Client(timeout=TIMEOUT) as client:
        token = get_token(client)
        seq = get_accounts(client, token)
        get_holdings(client, token, seq)
        get_us_candles(client, token)

    print("\n" + "=" * 60)
    print("완료. 위 4단계 결과(✓/✗)를 확인하세요.")
    print("특히 [4] 미국주식 분봉이 ✓ 인지, 응답의 symbol/market 표기가")
    print("어떻게 나오는지 알려주시면 다음 단계를 정하겠습니다.")
    print("=" * 60)


if __name__ == "__main__":
    main()
