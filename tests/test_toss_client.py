"""TossClient 테스트: 401(stale-token) 시 강제 재발급 + 1회 재시도(자동 복구).

실 Toss 미호출 — self._http 를 가짜로 교체해 검증.
"""
from __future__ import annotations

import httpx
import pytest

from src.config import Settings
from src.providers.toss_client import TossClient


class FakeResp:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=httpx.Request("GET", "http://x"),
                response=httpx.Response(self.status_code))

    def json(self):
        return self._json


class FakeHttp:
    """post=토큰발급, get=candles. get 시퀀스를 스크립트로 제어."""
    def __init__(self, get_statuses):
        self.token_calls = 0
        self.get_calls = 0
        self._get_statuses = list(get_statuses)

    def post(self, url, data=None):
        self.token_calls += 1
        return FakeResp(200, {"access_token": f"tok{self.token_calls}", "expires_in": 86399})

    def get(self, url, headers=None, params=None):
        st = self._get_statuses[min(self.get_calls, len(self._get_statuses) - 1)]
        self.get_calls += 1
        if st == 200:
            return FakeResp(200, {"result": {"candles": [{"c": 1}]}})
        return FakeResp(st, {})

    def close(self):
        pass


def _client(fake):
    s = Settings(toss_base="http://x", toss_client_id="id",
                 toss_client_secret="sec", db_path=":memory:")
    c = TossClient(s)
    c._http = fake
    return c


def test_get_candles_refreshes_token_on_401():
    fake = FakeHttp([401, 200])          # 첫 GET 401 → 재발급 → 재시도 200
    c = _client(fake)
    result = c.get_candles("005930", "1m", 5)
    assert result == {"candles": [{"c": 1}]}
    assert fake.token_calls == 2         # 최초 + 401 후 강제 재발급
    assert fake.get_calls == 2           # 401 + 재시도


def test_get_candles_happy_path_single_token():
    fake = FakeHttp([200])
    c = _client(fake)
    assert c.get_candles("005930", "1m", 5) == {"candles": [{"c": 1}]}
    assert fake.token_calls == 1 and fake.get_calls == 1


def test_get_candles_non_401_not_retried():
    fake = FakeHttp([400, 400])          # 400 은 재시도 없이 전파
    c = _client(fake)
    with pytest.raises(httpx.HTTPStatusError):
        c.get_candles("005930", "1m", 5)
    assert fake.get_calls == 1           # 재시도 안 함


def test_get_candles_persistent_401_retries_once_then_raises():
    fake = FakeHttp([401, 401])          # 재시도도 401 이면 전파(무한재시도 금지)
    c = _client(fake)
    with pytest.raises(httpx.HTTPStatusError):
        c.get_candles("005930", "1m", 5)
    assert fake.get_calls == 2           # 최초 + 1회 재시도까지만
