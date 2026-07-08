"""Toss Open API 저수준 HTTP 클라이언트.

- OAuth2 client_credentials 토큰 발급 + 메모리 캐시(만료 전 재사용)
- candles 조회 (raw dict 반환)
- 이 클라이언트는 데이터 fetch 만 담당한다. (설계: Provider 는 fetch/주문만)
- 읽기 전용. 주문 메서드는 여기 없다.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from src.config import Settings


class TossClient:
    def __init__(self, settings: Settings, timeout: float = 20.0):
        self._s = settings
        self._http = httpx.Client(timeout=httpx.Timeout(timeout))
        self._token: str | None = None
        self._token_exp: float = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "TossClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- auth ----
    def _access_token(self) -> str:
        # 만료 60초 전이면 재발급
        if self._token and time.monotonic() < self._token_exp - 60:
            return self._token
        if not self._s.has_credentials:
            raise RuntimeError(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.")
        r = self._http.post(
            f"{self._s.toss_base}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._s.toss_client_id,
                "client_secret": self._s.toss_client_secret,
            },
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.monotonic() + float(data.get("expires_in", 3600))
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _invalidate_token(self) -> None:
        """캐시된 토큰 폐기 → 다음 요청 시 강제 재발급."""
        self._token = None
        self._token_exp = 0.0

    # ---- market data ----
    def get_candles(self, symbol: str, interval: str, count: int = 200,
                    before: str | None = None) -> dict[str, Any]:
        """GET /api/v1/candles. result 객체(candles[], nextBefore)를 반환.
        Toss candles API 는 count 최대 200 → 초과 시 400. 방어적으로 클램프한다.

        401(Unauthorized)은 서버측 토큰 무효화(만료 전 revoke)로 발생할 수 있다 — 이때
        expires_in 만 믿고 있으면 죽은 토큰을 계속 쓰게 되므로, 토큰을 강제 재발급하고 1회
        재시도한다(장시간 운영 stale-token 자동 복구; 생존 우선)."""
        count = max(1, min(int(count), 200))
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        try:
            return self._fetch_candles(params)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 401:
                self._invalidate_token()          # 강제 재발급 후 1회 재시도
                return self._fetch_candles(params)
            raise

    def _fetch_candles(self, params: dict[str, Any]) -> dict[str, Any]:
        r = self._http.get(
            f"{self._s.toss_base}/api/v1/candles",
            headers=self._auth_headers(),
            params=params,
        )
        r.raise_for_status()
        return r.json().get("result", {})
