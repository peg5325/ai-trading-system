"""ORB 사전등록 계약 로더 테스트 — 이슈 #91 (4-A).

`src.research.orb.contract` 는 사전등록 JSON 을 fail-closed 로 읽어들이는 얇은 층이다. 이
테스트는 (a) 그 계약 hash 상수가 `tests/test_prereg_orb_contract.py` 의 동결 정본과 실제로
같은지, (b) 문서/JSON 이 변조되면 fail-closed 로 죽는지, (c) 반환 계약이 불변인지, (d) spec
접근자가 이웃을 하드코딩 없이 계약에서 파생하는지를 검증한다.
"""
from __future__ import annotations

import ast
import json
import os

import pytest

from src.research.orb import contract as c
from tests.test_prereg_orb_contract import (
    PREREG_JSON_SHA256 as FROZEN_JSON_SHA256,
)
from tests.test_prereg_orb_contract import (
    PREREG_MD_SHA256 as FROZEN_MD_SHA256,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# (a) 계약 hash 상수 ↔ 사전등록 동결 테스트의 정본이 동일
# --------------------------------------------------------------------------
def test_module_hash_constants_match_the_frozen_prereg_test():
    """`contract.py` 의 hash 상수가 사전등록을 실제로 동결한 테스트의 값과 어긋나면,
    이 모듈은 병합 이후 바뀐(혹은 아직 안 바뀐) 사전등록을 검증하는 셈이 된다."""
    assert c.PREREG_MD_SHA256 == FROZEN_MD_SHA256
    assert c.PREREG_JSON_SHA256 == FROZEN_JSON_SHA256


def test_compute_functions_match_the_pinned_constants():
    """실제 repo 파일에서 재계산한 해시가 동결 상수와 일치(= 사전등록이 변조되지 않음)."""
    assert c.compute_md_sha256() == c.PREREG_MD_SHA256
    assert c.compute_json_sha256() == c.PREREG_JSON_SHA256


def test_repo_root_resolves_to_actual_repo():
    root = c.repo_root()
    assert os.path.isdir(os.path.join(root, "docs", "research"))
    assert os.path.isfile(os.path.join(root, c.PREREG_MD_PATH))
    assert os.path.isfile(os.path.join(root, c.PREREG_JSON_PATH))


def test_load_contract_succeeds_against_real_files():
    data = c.load_contract()
    assert data["schema"] == "prereg-orb/contract/v1"


# --------------------------------------------------------------------------
# (b) 변조 → fail-closed (mutation 가드가 실제로 실패를 잡는지 증명)
# --------------------------------------------------------------------------
def test_tampered_markdown_copy_fails_integrity_check(tmp_path):
    """정본을 1바이트 변조한 임시 복사본은 hash 가 어긋나야 한다."""
    real_path = os.path.join(REPO, c.PREREG_MD_PATH)
    with open(real_path, encoding="utf-8") as f:
        text = f.read()
    tampered = tmp_path / "tampered.md"
    tampered.write_text(text.replace("RVOL", "RVOI", 1), encoding="utf-8")
    tampered_hash = c.compute_md_sha256(str(tampered))
    assert tampered_hash != c.PREREG_MD_SHA256


def test_tampered_json_copy_fails_integrity_check(tmp_path):
    real_path = os.path.join(REPO, c.PREREG_JSON_PATH)
    with open(real_path, encoding="utf-8") as f:
        data = json.load(f)
    data["candidate"]["rvol"]["threshold"] = 1.4    # 1.5 -> 1.4 로 변조
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data), encoding="utf-8")
    tampered_hash = c.compute_json_sha256(str(tampered))
    assert tampered_hash != c.PREREG_JSON_SHA256


def test_load_contract_raises_when_md_hash_constant_is_wrong(monkeypatch):
    """`load_contract(verify=True)` 는 상수가 실제 계산값과 다르면 예외를 던져야 한다.

    mutation: 상수를 실제 값과 다른 문자열로 되돌려(패치) 테스트가 이를 잡는지 확인한다.
    """
    monkeypatch.setattr(c, "PREREG_MD_SHA256", "0" * 64)
    with pytest.raises(c.ContractIntegrityError):
        c.load_contract(verify=True)


def test_load_contract_raises_when_json_hash_constant_is_wrong(monkeypatch):
    monkeypatch.setattr(c, "PREREG_JSON_SHA256", "0" * 64)
    with pytest.raises(c.ContractIntegrityError):
        c.load_contract(verify=True)


def test_load_contract_verify_false_skips_hash_check(monkeypatch):
    """verify=False 는 hash 를 검사하지 않는다(계약 fixture 테스트 등에 한정 사용)."""
    monkeypatch.setattr(c, "PREREG_MD_SHA256", "0" * 64)
    monkeypatch.setattr(c, "PREREG_JSON_SHA256", "0" * 64)
    data = c.load_contract(verify=False)
    assert data["schema"] == "prereg-orb/contract/v1"


def test_load_contract_default_call_with_no_args_is_fail_closed(monkeypatch):
    """B2(독립 리뷰 H3) — `verify: bool = True` 라는 **기본값** 자체가 fail-closed 여야 한다.

    이전 판은 실패를 보는 두 테스트가 `verify=True` 를 **명시적으로** 넘겨서, `verify: bool =
    True` → `False` 로 기본값을 뒤집는 변조가 살아남았다. 여기서는 인자 없이 호출한다.
    """
    monkeypatch.setattr(c, "PREREG_MD_SHA256", "0" * 64)
    with pytest.raises(c.ContractIntegrityError):
        c.load_contract()   # 인자 없음 — verify 기본값 사용


def test_load_contract_default_call_catches_json_hash_mismatch_too(monkeypatch):
    monkeypatch.setattr(c, "PREREG_JSON_SHA256", "0" * 64)
    with pytest.raises(c.ContractIntegrityError):
        c.load_contract()


# --------------------------------------------------------------------------
# (c) 반환 계약 불변성
# --------------------------------------------------------------------------
def test_loaded_contract_is_immutable():
    data = c.load_contract()
    with pytest.raises(TypeError):
        data["development_gate"]["D1_min_trades"] = 999          # 최상위 dict 변조 시도
    with pytest.raises(TypeError):
        data["windows"]["development_data_start"] = "2000-01-01"  # 중첩 dict 변조 시도


def test_loaded_contract_lists_are_tuples():
    data = c.load_contract()
    assert isinstance(data["universe"]["market_etf"], tuple)
    assert isinstance(data["folds"]["boundaries"], tuple)
    assert isinstance(data["folds"]["boundaries"][0], type(data["folds"]["boundaries"][0]))
    # 리스트 안 dict 도 얼어 있어야(중첩 불변)
    with pytest.raises(TypeError):
        data["folds"]["boundaries"][0]["trading_days"] = 1


# --------------------------------------------------------------------------
# (d) SpecParams 접근자
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def contract() -> dict:
    return c.load_contract()


def test_primary_spec_values(contract):
    p = c.primary_spec(contract)
    assert p.name == "primary"
    assert p.opening_range_minutes == 15
    assert p.gap_threshold_abs == 0.003
    assert p.rvol_threshold == 1.5
    assert p.target_r_multiple == 2.0
    assert p.authority == "decides_go_nogo"


def test_sensitivity_spec_shares_everything_but_or_window(contract):
    p = c.primary_spec(contract)
    s = c.sensitivity_spec(contract)
    assert s.name == "sensitivity"
    assert s.opening_range_minutes == 5
    assert s.authority == "diagnostic_only"
    # §4: OR 길이만 다르고 나머지 규칙(gap/rvol/target)은 primary 와 완전히 동일.
    assert s.gap_threshold_abs == p.gap_threshold_abs
    assert s.rvol_threshold == p.rvol_threshold
    assert s.target_r_multiple == p.target_r_multiple


def test_all_specs_are_exactly_eight_with_correct_authority(contract):
    specs = c.all_specs(contract)
    assert len(specs) == 8
    authorities = [s.authority for s in specs]
    assert authorities.count("decides_go_nogo") == 1
    assert specs[0].name == "primary" and specs[0].authority == "decides_go_nogo"
    for s in specs[1:]:
        assert s.authority == "diagnostic_only"


def test_neighbor_specs_are_six_and_vary_exactly_one_axis_each(contract):
    p = c.primary_spec(contract)
    neighbors = c.neighbor_specs(contract)
    assert len(neighbors) == 6

    base = {
        "opening_range_minutes": p.opening_range_minutes,
        "gap_threshold_abs": p.gap_threshold_abs,
        "rvol_threshold": p.rvol_threshold,
        "target_r_multiple": p.target_r_multiple,
    }
    axis_counts: dict[str, int] = {}
    for n in neighbors:
        got = {
            "opening_range_minutes": n.opening_range_minutes,
            "gap_threshold_abs": n.gap_threshold_abs,
            "rvol_threshold": n.rvol_threshold,
            "target_r_multiple": n.target_r_multiple,
        }
        diffs = [k for k in base if got[k] != base[k]]
        assert len(diffs) == 1, f"{n.name} 은 정확히 1축만 달라야 한다: {diffs}"
        axis = diffs[0]
        axis_counts[axis] = axis_counts.get(axis, 0) + 1
        assert n.authority == "diagnostic_only"
    # 축별로 정확히 2개(하한/상한)
    assert axis_counts == {
        "rvol_threshold": 2, "gap_threshold_abs": 2, "target_r_multiple": 2,
    }


def test_neighbor_values_match_contract_json_exactly(contract):
    neighbors = {n.name: n for n in c.neighbor_specs(contract)}
    assert neighbors["rvol_1.3"].rvol_threshold == 1.3
    assert neighbors["rvol_1.7"].rvol_threshold == 1.7
    assert neighbors["gap_0.002"].gap_threshold_abs == 0.002
    assert neighbors["gap_0.004"].gap_threshold_abs == 0.004
    assert neighbors["target_1.5"].target_r_multiple == 1.5
    assert neighbors["target_2.5"].target_r_multiple == 2.5


def test_neighbor_specs_raise_when_contract_has_no_neighbor_definition(contract):
    """이웃 정의를 지운 fixture → 예외(하드코딩 fallback 이 없음을 증명).

    mutation: `parameter_cliff_neighborhood` 키 자체를 지운 얕은 사본으로 호출한다.
    """
    mutated = dict(contract)
    mutated.pop("parameter_cliff_neighborhood")
    with pytest.raises(c.ContractIntegrityError):
        c.neighbor_specs(mutated)


def test_neighbor_specs_raise_on_unknown_axis():
    """알 수 없는 축 이름이 섞이면 하드코딩된 축 목록 밖이라 실패해야 한다.

    mutation: (frozen mappingproxy 는 deepcopy 가 안 되므로) 실제 JSON 을 평범한 dict 로
    새로 읽어 그 사본만 변조한다.
    """
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["parameter_cliff_neighborhood"]["variants"].append(
        {"axis": "unknown_axis", "neighbors": [1, 2]})
    with pytest.raises(c.ContractIntegrityError):
        c.neighbor_specs(mutated)


def test_accessors_raise_when_required_value_missing(contract):
    """접근자가 계약에 없는 값을 만들어내지 않는다 — 필수 키가 없으면 예외."""
    mutated = dict(contract)
    mutated.pop("risk")
    with pytest.raises(c.ContractIntegrityError):
        c.primary_spec(mutated)


def test_require_helper_reports_missing_path():
    with pytest.raises(c.ContractIntegrityError):
        c._require({"a": {"b": 1}}, "a", "c")
    assert c._require({"a": {"b": 1}}, "a", "b") == 1


# --------------------------------------------------------------------------
# (e) 계약 연결(C1) — decision_bar_spec / rvol_lookback 이 계약값을 재입력 없이 반영하는가
# --------------------------------------------------------------------------
def test_decision_bar_spec_matches_contract_literals(contract):
    spec = c.decision_bar_spec(contract)
    assert spec == c.DecisionBarSpec(minutes=5, min_bars=1)
    assert spec.minutes == contract["spec"]["decision_bar_minutes"]
    assert spec.min_bars == contract["spec"]["decision_bar_min_bars"]


def test_decision_bar_spec_reflects_tampered_contract_value():
    """C1(독립 리뷰 MAJOR-4/M5) — 코드가 5·1 을 재입력하지 않고 실제로 계약을 읽는지: 계약값을
    바꾸면 접근자 출력도 바뀌어야 한다(하드코딩이면 바뀌지 않을 것이므로 회귀 감지력이 있다)."""
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["spec"]["decision_bar_minutes"] = 7
    mutated["spec"]["decision_bar_min_bars"] = 3
    spec = c.decision_bar_spec(mutated)
    assert spec == c.DecisionBarSpec(minutes=7, min_bars=3)


def test_decision_bar_spec_raises_when_keys_missing():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    del mutated["spec"]["decision_bar_minutes"]
    with pytest.raises(c.ContractIntegrityError):
        c.decision_bar_spec(mutated)


def test_rvol_lookback_matches_contract_literals(contract):
    lb = c.rvol_lookback(contract)
    assert lb == c.RvolLookbackSpec(lookback_trading_days=20, min_valid_lookback_days=15)


def test_rvol_lookback_reflects_tampered_contract_value():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["candidate"]["rvol"]["lookback_trading_days"] = 30
    mutated["candidate"]["rvol"]["min_valid_lookback_days"] = 22
    lb = c.rvol_lookback(mutated)
    assert lb == c.RvolLookbackSpec(lookback_trading_days=30, min_valid_lookback_days=22)


def test_rvol_lookback_raises_when_keys_missing():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    del mutated["candidate"]["rvol"]["min_valid_lookback_days"]
    with pytest.raises(c.ContractIntegrityError):
        c.rvol_lookback(mutated)


# --------------------------------------------------------------------------
# (f) 계약 연결(C2) — universe(contract) 가 30종목·family 를 재입력 없이 반영하는가
# --------------------------------------------------------------------------
def test_universe_matches_contract_and_has_30_symbols(contract):
    u = c.universe(contract)
    assert len(u.all_symbols) == 30
    assert len(set(u.all_symbols)) == 30
    assert set(u.exposure_families) == {"QQQ_FAMILY", "SOX_FAMILY"}
    assert u.exposure_families["QQQ_FAMILY"] == ("QQQ", "TQQQ", "SQQQ")
    assert u.exposure_families["SOX_FAMILY"] == ("SMH", "SOXL", "SOXS")
    assert set(u.exposure_families["QQQ_FAMILY"]) <= set(u.all_symbols)
    assert set(u.exposure_families["SOX_FAMILY"]) <= set(u.all_symbols)


def test_universe_reflects_tampered_contract_symbol_list():
    """C2(독립 리뷰 MAJOR-5) — 4-B 의 §7 family 한도가 종목 목록을 재입력하지 않도록, 접근자가
    실제로 계약을 읽는지 확인한다."""
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["universe"]["large_cap"].append("ZZZZ")
    u = c.universe(mutated)
    assert "ZZZZ" in u.all_symbols
    assert len(u.all_symbols) == 31


def test_universe_raises_when_exposure_families_missing():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    del mutated["universe"]["exposure_families"]
    with pytest.raises(c.ContractIntegrityError):
        c.universe(mutated)


# --------------------------------------------------------------------------
# (g) C3 — `load_contract(verify=False)` 는 src/·scripts/·analysis/ 밖에서 금지(tests/ 는 허용)
# --------------------------------------------------------------------------
def test_verify_false_forbidden_outside_tests():
    """결과 산출 코드가 hash 검증을 끄고 실행할 수 없게 한다(테스트 fixture 용도로만 허용)."""
    offenders: list[str] = []
    for base in ("src", "scripts", "analysis"):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    name_matches = (
                        (isinstance(func, ast.Name) and func.id == "load_contract")
                        or (isinstance(func, ast.Attribute) and func.attr == "load_contract")
                    )
                    if not name_matches:
                        continue
                    for kw in node.keywords:
                        if (kw.arg == "verify" and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False):
                            offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == [], f"verify=False 로 load_contract 호출 발견: {offenders}"


def test_verify_false_forbidden_rule_actually_detects_it(tmp_path):
    """거짓 가드 방지 — 위 스캐너가 실제로 `verify=False` 호출을 탐지하는지 별도 fixture 로 증명."""
    offending_src = (
        "from src.research.orb.contract import load_contract\n"
        "load_contract(verify=False)\n"
    )
    tmp_file = tmp_path / "offender.py"
    tmp_file.write_text(offending_src, encoding="utf-8")
    tree = ast.parse(offending_src, filename=str(tmp_file))
    hits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "load_contract"
        and any(kw.arg == "verify" and isinstance(kw.value, ast.Constant)
               and kw.value.value is False for kw in node.keywords)
    ]
    assert len(hits) == 1


# --------------------------------------------------------------------------
# (h) F4(lane3 Medium) — 계약 값 연동(provenance): 값만 보고 출처를 안 보던 구멍.
#     실제 계약값에서 primary_spec() 이 나온 값을 확인하는 것만으로는, 그 함수가 값을
#     하드코딩해도(우연히 계약값과 같으면) 통과한다. 계약값을 바꿔 접근자 출력이 따라
#     바뀌는지를 봐야 진짜 연동을 증명한다.
# --------------------------------------------------------------------------
def test_primary_spec_reflects_tampered_contract_values_for_all_numeric_fields():
    """`contract.py` 의 opening_range_minutes=15 등 하드코딩이 통과하던 구멍을 막는다."""
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["spec"]["primary"]["opening_range_minutes"] = 21
    mutated["candidate"]["gap"]["threshold_abs"] = 0.0099
    mutated["candidate"]["rvol"]["threshold"] = 9.9
    mutated["risk"]["target_r_multiple"] = 9.9
    p = c.primary_spec(mutated)
    assert p.opening_range_minutes == 21
    assert p.gap_threshold_abs == 0.0099
    assert p.rvol_threshold == 9.9
    assert p.target_r_multiple == 9.9


def test_sensitivity_spec_reflects_tampered_contract_opening_range_minutes():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    mutated["spec"]["sensitivity"]["opening_range_minutes"] = 7
    s = c.sensitivity_spec(mutated)
    assert s.opening_range_minutes == 7


# --------------------------------------------------------------------------
# (i) F5(lane3 Medium) — 변조 fixture 비대칭: "이웃 정확히 6개" 게이트가 개수 자체가
#     줄어든 fixture 로는 검증되지 않고 있었다(기존은 unknown axis 추가/전체 삭제만 테스트).
# --------------------------------------------------------------------------
def test_neighbor_specs_raise_when_neighbor_count_reduced():
    with open(os.path.join(REPO, c.PREREG_JSON_PATH), encoding="utf-8") as f:
        mutated = json.load(f)
    # 한 축(rvol_threshold)의 이웃을 2개에서 1개로 줄인다 → 전체 이웃 수 6 -> 5.
    mutated["parameter_cliff_neighborhood"]["variants"][0]["neighbors"] = \
        mutated["parameter_cliff_neighborhood"]["variants"][0]["neighbors"][:1]
    with pytest.raises(c.ContractIntegrityError):
        c.neighbor_specs(mutated)


# --------------------------------------------------------------------------
# (j) F6(lane3 Medium) — `_normalized_md` 의 "정본과 바이트 단위로 동일" 주장을 CRLF 를
#     포함한 입력으로 실제 검증한다. 정본 문서엔 CRLF 가 없어 `.replace("\r\n","\n")` 제거가
#     이전엔 생존했다.
# --------------------------------------------------------------------------
def test_normalized_md_matches_the_frozen_test_normalizer_with_crlf_input():
    import tests.test_prereg_orb_contract as prereg_test_module

    sample = "line one  \r\nline two\ttab kept\r\n\r\n  trailing spaces   \r\n"
    ours = c._normalized_md(sample)
    theirs = prereg_test_module._normalized_md(sample)
    assert ours == theirs
    assert "\r" not in ours
