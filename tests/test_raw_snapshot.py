"""Immutable raw 스냅샷 테스트 (계약 §4.1 · 이슈 #75 Phase 1B).

검증(작업 테스트 11~16):
  11. atomic write(완료본 존재·partial 미잔존·round-trip)
  12. 동일 payload 멱등
  13. 다른 payload hash 충돌 방지
  14. raw snapshot·provenance 에 secret 없음
  15. 중단된 partial 파일 미승격
  16. manifest 결정론 및 content hash 재현
  + provider fetch_page 가 **parse 전** 원본 payload 를 보존하는지(진짜 raw)
"""
from __future__ import annotations

import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.raw_snapshot import (  # noqa: E402
    RECEIPTS_BASENAME,
    RawSnapshotStore,
    _assert_no_secret_keys,
    canonical_bytes,
    content_sha256,
)
from src.providers.toss_market_data import TossMarketDataProvider  # noqa: E402


def _payload(*prices) -> dict:
    """raw Toss 응답 모사: 문자열 가격 + 여분 키(파싱 시 유실될 필드)."""
    candles = [{
        "timestamp": f"2026-07-06T{10 + i:02d}:00:00.000+09:00",
        "openPrice": str(p), "highPrice": str(p + 1), "lowPrice": str(p - 1),
        "closePrice": str(p), "volume": "1000", "currency": "USD",
        "marketPhase": "REGULAR",   # ★파싱 후 Candle 에는 없는 여분 필드(raw 보존 증거)
    } for i, p in enumerate(prices)]
    return {"candles": candles, "nextBefore": "2026-07-06T00:00:00.000+09:00",
            "meta": {"source": "toss"}}


def _emit(store, payload, *, before=None):
    return store.emit(symbol="SPY", interval="1m", before=before,
                      requested_at="2026-07-23T00:00:00+00:00",
                      received_at="2026-07-23T00:00:01+00:00",
                      source="toss", payload=payload)


def test_atomic_write_roundtrip_no_partial(tmp_path):
    """11. 완료본 존재·partial 미잔존·gzip round-trip 이 원본 payload 와 동일."""
    store = RawSnapshotStore(str(tmp_path / "raw"))
    payload = _payload(100, 101, 102)
    rec = _emit(store, payload)
    assert rec.written is True
    final = os.path.join(store.root, rec.rel_path)
    assert os.path.exists(final)
    # partial 잔존 없음.
    partials = [p for _d, _s, fs in os.walk(store.root) for p in fs if p.endswith(".partial")]
    assert partials == []
    # round-trip: 저장본 == 원본 payload(문자열 가격·여분 키 그대로).
    assert store.read_payload(rec.rel_path) == payload
    # 파일명 = content sha.
    assert os.path.basename(rec.rel_path) == f"{content_sha256(payload)}.json.gz"


def test_idempotent_same_payload(tmp_path):
    """12. 동일 payload → 파일 1개, 2번째는 written=False(재기록 안 함)."""
    store = RawSnapshotStore(str(tmp_path / "raw"))
    payload = _payload(100, 101)
    r1 = _emit(store, payload)
    r2 = _emit(store, payload)
    assert r1.written is True and r2.written is False
    assert r1.content_sha256 == r2.content_sha256
    assert len(store.list_content_files()) == 1


def test_distinct_payload_no_collision(tmp_path):
    """13. 다른 payload → 다른 sha, 다른 파일(충돌 없음)."""
    store = RawSnapshotStore(str(tmp_path / "raw"))
    r1 = _emit(store, _payload(100))
    r2 = _emit(store, _payload(200))
    assert r1.content_sha256 != r2.content_sha256
    assert len(store.list_content_files()) == 2


def test_no_secret_in_snapshot_or_receipt(tmp_path):
    """14. 저장본/receipts 에 secret 문자열·키 없음. secret 키는 fail-closed."""
    store = RawSnapshotStore(str(tmp_path / "raw"))
    rec = _emit(store, _payload(100))
    # 저장본에 secret 토큰 없음.
    blob = gzip.open(os.path.join(store.root, rec.rel_path), "rb").read().decode()
    for hint in ("authorization", "bearer", "client_secret", "TOSS_CLIENT"):
        assert hint.lower() not in blob.lower()
    # receipts 필드 = 고정 화이트리스트(secret 키 없음).
    receipts = (tmp_path / "raw" / RECEIPTS_BASENAME).read_text()
    entry = json.loads(receipts.splitlines()[0])
    assert set(entry) == {
        "symbol", "interval", "before", "requested_at", "received_at", "source",
        "content_sha256", "rel_path", "payload_bytes", "compressed_bytes",
        "candle_count", "next_before", "written"}
    # 방어 가드: secret-스러운 키가 섞이면 즉시 실패.
    with pytest.raises(ValueError):
        _assert_no_secret_keys({"authorization": "x"})


def test_stray_partial_not_promoted(tmp_path):
    """15. crash 로 남은 *.partial 은 완료본으로 오인되지 않는다."""
    store = RawSnapshotStore(str(tmp_path / "raw"))
    _emit(store, _payload(100))
    # 가짜 partial(중단 흔적) 생성.
    d = os.path.join(store.root, "SPY", "1m", "ab")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "deadbeef.json.gz.1234.partial"), "wb") as f:
        f.write(b"not-a-real-snapshot")
    files = store.list_content_files()
    assert all(not p.endswith(".partial") for p in files)
    assert len(files) == 1                      # partial 은 세지 않음
    assert store.manifest()["content_file_count"] == 1


def test_manifest_deterministic_and_order_independent(tmp_path):
    """16. 같은 content 집합이면 write 순서·인스턴스 무관하게 동일 raw_content_sha256."""
    s1 = RawSnapshotStore(str(tmp_path / "a"))
    for p in (_payload(100), _payload(200), _payload(300)):
        _emit(s1, p)
    m1 = s1.manifest()
    s2 = RawSnapshotStore(str(tmp_path / "b"))
    for p in (_payload(300), _payload(100), _payload(200)):   # 다른 순서
        _emit(s2, p)
    m2 = s2.manifest()
    assert m1["raw_content_sha256"] == m2["raw_content_sha256"]
    assert m1["content_file_count"] == m2["content_file_count"] == 3


def test_interrupt_resume_does_not_corrupt(tmp_path):
    """중단→재개(새 인스턴스)로 다시 emit 해도 기존 완료본을 손상시키지 않는다(immutable)."""
    root = str(tmp_path / "raw")
    payload = _payload(100, 101)
    r1 = _emit(RawSnapshotStore(root), payload)
    final = os.path.join(root, r1.rel_path)
    before_bytes = open(final, "rb").read()
    r2 = _emit(RawSnapshotStore(root), payload)   # 새 프로세스/인스턴스 시뮬레이션
    assert r2.written is False
    assert open(final, "rb").read() == before_bytes   # 재기록 없음


# --- provider fetch_page 가 parse 전 payload 를 보존하는가(진짜 raw) ---

class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def get_candles(self, symbol, interval, *, count=200, before=None):
        return self._payload


def test_provider_preserves_pre_parse_payload(tmp_path):
    """provider.fetch_page(raw_sink=...) 이 canonical 변환 전 원본 payload 를 보존한다.

    저장본은 문자열 가격·여분 키(marketPhase, meta)를 그대로 담아야 한다(파싱 후 Candle 에는
    없는 필드) → '캔들 재구성' 이 아니라 진짜 응답 payload 임을 증명.
    """
    payload = _payload(123, 124)
    prov = object.__new__(TossMarketDataProvider)   # TossClient init 우회
    prov._client = _FakeClient(payload)             # noqa: SLF001 (테스트)
    store = RawSnapshotStore(str(tmp_path / "raw"))
    candles, nxt = prov.fetch_page("SPY", "1m", count=200, before=None, raw_sink=store.emit)
    # 파싱 결과는 정상.
    assert len(candles) == 2 and candles[0].open == 123.0
    # 저장된 raw == 원본(문자열 가격·여분 키 보존).
    rels = store.list_content_files()
    assert len(rels) == 1
    stored = store.read_payload(rels[0])
    assert stored == payload
    assert stored["candles"][0]["openPrice"] == "123"   # 문자열(파싱 전)
    assert "marketPhase" in stored["candles"][0]         # 여분 필드 보존
    assert canonical_bytes(stored) == canonical_bytes(payload)
