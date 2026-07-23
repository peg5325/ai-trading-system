"""manifest as_of ↔ 수집 cutoff 일관성(이슈 #72).

수집이 완료-거래일 cutoff C 까지만 저장했는데 manifest 가 as_of A(> C)로 커버리지를 주장하면
(A 초과가 아니라 (C, A] 구간이 **미수집**) OK 판정이 거짓이 된다. build_manifest 는 이 경우
fail-closed(raise)해 OK 를 받을 수 없게 한다(경계 완화 금지 규율과 정합).
"""
from __future__ import annotations

import pytest

from src.history import daily_manifest as dm
from src.history import intraday_manifest as im
from src.history.store import HistoryStore

YAML = "config/universe_p0_us30.yaml"


def _empty_db(tmp_path) -> str:
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as _s:
        pass
    return dbp


# --- daily ---
def test_daily_raises_when_cutoff_before_asof(tmp_path):
    dbp = _empty_db(tmp_path)
    with pytest.raises(ValueError):
        dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                          as_of="2026-07-21", collection_completed_through="2026-07-17")


def test_daily_ok_when_cutoff_equals_asof(tmp_path):
    dbp = _empty_db(tmp_path)
    man = dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-21", collection_completed_through="2026-07-21")
    assert man["collection"]["completed_through"] == "2026-07-21"


def test_daily_ok_when_cutoff_after_asof(tmp_path):
    # cutoff 가 as_of 보다 늦음(수집이 더 멀리 감) → as_of 커버리지는 확보됨(허용).
    dbp = _empty_db(tmp_path)
    man = dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-17", collection_completed_through="2026-07-21")
    assert man["collection"]["completed_through"] == "2026-07-21"


def test_daily_none_cutoff_unchanged(tmp_path):
    dbp = _empty_db(tmp_path)
    man = dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-21")
    assert man["collection"].get("completed_through") is None


def test_daily_raises_on_bad_cutoff_format(tmp_path):
    dbp = _empty_db(tmp_path)
    with pytest.raises(ValueError):
        dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                          as_of="2026-07-21", collection_completed_through="2026/07/21")


# --- intraday ---
def test_intraday_raises_when_cutoff_before_asof(tmp_path):
    dbp = _empty_db(tmp_path)
    with pytest.raises(ValueError):
        im.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                          as_of="2026-07-21", collection_completed_through="2026-07-17")


def test_intraday_ok_when_cutoff_equals_asof(tmp_path):
    dbp = _empty_db(tmp_path)
    man = im.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-21", collection_completed_through="2026-07-21")
    assert man["collection"]["completed_through"] == "2026-07-21"
