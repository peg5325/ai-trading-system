"""ORB 사전등록 계약 로더 — 이슈 #91 (4-A).

**목적**: 구현이 임계값을 재입력하지 못하게 한다. 모든 수치는 동결된
`docs/research/PREREG_P0_US30_ORB_V1.json` 에서만 온다. 이 모듈이 임의로 임계값을 정하는
곳이 아니다 — 사전등록이 정본이고 이 모듈은 그것을 fail-closed 로 읽어들이는 얇은 층이다.

정규화·해시 계산 방식은 `tests/test_prereg_orb_contract.py` 가 문서 동결에 쓰는 것과
**정확히 같아야 한다**(마크다운은 공백만 정규화, JSON 은 `sort_keys` 정본 직렬화). 그 테스트
모듈을 이 패키지가 import 하지는 않는다 — 같은 로직을 여기 독립적으로 구현하고, 테스트가
두 값이 실제로 같은지 대조한다(§테스트 브리프 §3.1).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# repo 상대 경로(고정) — 이 문자열 자체는 계약 값이 아니라 계약 **위치**다.
PREREG_MD_PATH = "docs/research/PREREG_P0_US30_ORB_V1.md"
PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_ORB_V1.json"

# `tests/test_prereg_orb_contract.py` 의 동결 상수와 **동일해야 한다**(그 파일이 병합 시점의
# 정본 해시를 갖고 있다). 두 상수가 어긋나면 `test_orb_contract.py` 가 즉시 실패한다.
PREREG_MD_SHA256 = "ccce3837f24a3a0da43f511ff0eff44ee59f677c73f9fcc349754786eaad8b80"
PREREG_JSON_SHA256 = "adb2e246a9ed94c7b1fce1fd3dc12105b7ab49534bdd08dba543e62ab17bfad6"


class ContractIntegrityError(RuntimeError):
    """사전등록 계약의 무결성이 깨졌을 때(hash 불일치·필수 값 부재) — fail-closed."""


def repo_root() -> str:
    """저장소 루트 절대경로. 이 파일(`src/research/orb/contract.py`) 기준 4단계 상위."""
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    return here


def _normalized_md(text: str) -> str:
    """공백만 정규화한다 — 의미 있는 문자는 하나도 바꾸지 않는다.

    `tests/test_prereg_orb_contract.py:_normalized_md` 와 바이트 단위로 동일해야 한다.
    """
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip() + "\n"


def compute_md_sha256(path: str | None = None) -> str:
    """사전등록 마크다운의 정규화 SHA-256. `path` 미지정 시 repo 의 정본 파일을 읽는다."""
    p = path or os.path.join(repo_root(), PREREG_MD_PATH)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    return hashlib.sha256(_normalized_md(text).encode("utf-8")).hexdigest()


def compute_json_sha256(path: str | None = None) -> str:
    """사전등록 계약 JSON 의 `sort_keys` 정본 직렬화 SHA-256.

    `tests/test_prereg_orb_contract.py:_json_hash` 와 동일한 직렬화(`sort_keys=True,
    ensure_ascii=False, separators=(",", ":")`)를 쓴다.
    """
    p = path or os.path.join(repo_root(), PREREG_JSON_PATH)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    canon = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    """깊은 불변화: dict → `MappingProxyType`(중첩도), list/tuple → `tuple`(중첩도).

    호출자가 반환된 계약을 변조할 수 없게 한다(§3.1 "반환값은 불변이어야 한다").
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def load_contract(*, verify: bool = True) -> Mapping[str, Any]:
    """사전등록 계약 JSON 을 로드한다.

    `verify=True`(기본, fail-closed): 문서·JSON 의 정규화 SHA-256 이 위 동결 상수와 어긋나면
    `ContractIntegrityError`. 반환값은 깊은 불변(freeze)이다 — 호출자가 계약을 변조할 수 없다.
    """
    md_path = os.path.join(repo_root(), PREREG_MD_PATH)
    json_path = os.path.join(repo_root(), PREREG_JSON_PATH)
    if verify:
        md_hash = compute_md_sha256(md_path)
        if md_hash != PREREG_MD_SHA256:
            raise ContractIntegrityError(
                f"사전등록 문서 hash 불일치(fail-closed): {md_hash} != {PREREG_MD_SHA256}")
        json_hash = compute_json_sha256(json_path)
        if json_hash != PREREG_JSON_SHA256:
            raise ContractIntegrityError(
                f"사전등록 계약 JSON hash 불일치(fail-closed): {json_hash} != {PREREG_JSON_SHA256}")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    return _freeze(data)


def _require(mapping: Any, *keys: str) -> Any:
    """중첩 키를 따라가며 값을 얻는다. 어느 단계든 없으면 `ContractIntegrityError`(하드코딩 fallback 금지)."""
    cur = mapping
    for k in keys:
        if not isinstance(cur, Mapping) or k not in cur:
            raise ContractIntegrityError(
                f"계약에 없는 값: {'.'.join(keys)} (누락 지점 {k!r}) — 하드코딩 fallback 사용 안 함")
        cur = cur[k]
    return cur


@dataclass(frozen=True)
class SpecParams:
    """하나의 완전한 ORB 파라미터화(primary / sensitivity / 이웃 중 하나)."""

    name: str                    # "primary" | "sensitivity" | "rvol_1.3" 등
    opening_range_minutes: int
    gap_threshold_abs: float
    rvol_threshold: float
    target_r_multiple: float
    authority: str               # "decides_go_nogo" | "diagnostic_only"


# 축 이름(계약 JSON) → (SpecParams 필드, 이름 접두어). 하드코딩된 것은 **축 이름 매핑**뿐이고
# 축의 값·이웃 자체는 전부 계약에서 읽는다(§10.1 은 1축 이웃만 허용하며 이 매핑은 그 세 축을
# SpecParams 필드에 연결하는 배선일 뿐이다).
_AXIS_FIELD = {
    "rvol_threshold": "rvol_threshold",
    "gap_threshold_abs": "gap_threshold_abs",
    "target_r_multiple": "target_r_multiple",
}
_AXIS_PREFIX = {
    "rvol_threshold": "rvol",
    "gap_threshold_abs": "gap",
    "target_r_multiple": "target",
}


def primary_spec(contract: Mapping[str, Any]) -> SpecParams:
    """primary(15분 OR) — GO/NO-GO 판정 권한을 가진 유일한 spec(사전등록 §4)."""
    return SpecParams(
        name="primary",
        opening_range_minutes=int(_require(contract, "spec", "primary", "opening_range_minutes")),
        gap_threshold_abs=float(_require(contract, "candidate", "gap", "threshold_abs")),
        rvol_threshold=float(_require(contract, "candidate", "rvol", "threshold")),
        target_r_multiple=float(_require(contract, "risk", "target_r_multiple")),
        authority=str(_require(contract, "spec", "primary", "authority")),
    )


def sensitivity_spec(contract: Mapping[str, Any]) -> SpecParams:
    """sensitivity(5분 OR) — 진단 전용. §4: OR 길이만 다르고 나머지 규칙은 primary 와 동일."""
    return SpecParams(
        name="sensitivity",
        opening_range_minutes=int(
            _require(contract, "spec", "sensitivity", "opening_range_minutes")),
        gap_threshold_abs=float(_require(contract, "candidate", "gap", "threshold_abs")),
        rvol_threshold=float(_require(contract, "candidate", "rvol", "threshold")),
        target_r_multiple=float(_require(contract, "risk", "target_r_multiple")),
        authority=str(_require(contract, "spec", "sensitivity", "authority")),
    )


def neighbor_specs(contract: Mapping[str, Any]) -> tuple[SpecParams, ...]:
    """1축 파라미터 절벽 이웃 6개(사전등록 §10.1) — 계약의 이웃 목록에서 **파생**한다(하드코딩 금지).

    각 이웃은 primary 값에서 정확히 한 축만 바뀐다. 순서는 계약 JSON 의
    `parameter_cliff_neighborhood.variants`(축) × 각 축의 `neighbors`(값) 선언 순서로 결정론적이다.
    """
    base = primary_spec(contract)
    variants = _require(contract, "parameter_cliff_neighborhood", "variants")
    out: list[SpecParams] = []
    for variant in variants:
        axis = variant["axis"]
        if axis not in _AXIS_FIELD:
            raise ContractIntegrityError(f"알 수 없는 파라미터 절벽 축: {axis!r}")
        field = _AXIS_FIELD[axis]
        prefix = _AXIS_PREFIX[axis]
        for value in variant["neighbors"]:
            values = {
                "opening_range_minutes": base.opening_range_minutes,
                "gap_threshold_abs": base.gap_threshold_abs,
                "rvol_threshold": base.rvol_threshold,
                "target_r_multiple": base.target_r_multiple,
            }
            values[field] = float(value)
            out.append(SpecParams(
                name=f"{prefix}_{value}",
                opening_range_minutes=int(values["opening_range_minutes"]),
                gap_threshold_abs=float(values["gap_threshold_abs"]),
                rvol_threshold=float(values["rvol_threshold"]),
                target_r_multiple=float(values["target_r_multiple"]),
                authority="diagnostic_only",
            ))
    result = tuple(out)
    if len(result) != 6:
        raise ContractIntegrityError(
            f"파라미터 절벽 이웃은 정확히 6개여야 한다(got {len(result)}) — 계약 변조 의심")
    return result


def all_specs(contract: Mapping[str, Any]) -> tuple[SpecParams, ...]:
    """primary + 이웃 6 + sensitivity = 8개(사전등록 §11.1 "8개 파라미터화 전부 게재")."""
    return (primary_spec(contract),) + neighbor_specs(contract) + (sensitivity_spec(contract),)


@dataclass(frozen=True)
class DecisionBarSpec:
    """결정봉 스펙(§6.2) — 시간축(길이)과 커버리지축(min_bars)을 계약에서만 읽는다.

    v1.0 은 `decision_bars`/`expected_decision_minutes` 가 5·1 을 직접 하드코딩했다 — 계약에
    같은 값(`spec.decision_bar_minutes`·`spec.decision_bar_min_bars`)이 있는데도 코드가 이를
    재입력하고 있어, 4-B 가 다른 값을 쓰는 실수를 검사가 잡지 못했다(독립 리뷰 MAJOR-4/M5).
    """

    minutes: int         # 결정봉 길이(분) — §6.2: 5
    min_bars: int        # 커버리지축 — §6.2: 1(결측 분이 있어도 만든다)


def decision_bar_spec(contract: Mapping[str, Any]) -> DecisionBarSpec:
    return DecisionBarSpec(
        minutes=int(_require(contract, "spec", "decision_bar_minutes")),
        min_bars=int(_require(contract, "spec", "decision_bar_min_bars")),
    )


@dataclass(frozen=True)
class RvolLookbackSpec:
    """RVOL lookback 스펙(§5.2) — `RvolWindow` 생성 시 넘길 lookback_days/min_valid_days 의
    유일한 출처. `RvolWindow(20, 15, ...)` 처럼 리터럴을 직접 타이핑하면 계약이 바뀌어도 코드가
    조용히 낡은 값을 쓰게 된다(독립 리뷰 MAJOR-4)."""

    lookback_trading_days: int    # §5.2: 20
    min_valid_lookback_days: int  # §5.2: 15


def rvol_lookback(contract: Mapping[str, Any]) -> RvolLookbackSpec:
    return RvolLookbackSpec(
        lookback_trading_days=int(_require(contract, "candidate", "rvol", "lookback_trading_days")),
        min_valid_lookback_days=int(
            _require(contract, "candidate", "rvol", "min_valid_lookback_days")),
    )


@dataclass(frozen=True)
class Universe:
    """P0-US30 유니버스(§2.1) — 30종목 4개 역할군 + family(§7 집중도 한도의 재입력 방지)."""

    market_etf: tuple[str, ...]
    sector_etf: tuple[str, ...]
    large_cap: tuple[str, ...]
    leveraged_inverse: tuple[str, ...]
    exposure_families: Mapping[str, tuple[str, ...]]

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(
            self.market_etf + self.sector_etf + self.large_cap + self.leveraged_inverse))


def universe(contract: Mapping[str, Any]) -> Universe:
    u = _require(contract, "universe")
    fam = _require(u, "exposure_families")
    return Universe(
        market_etf=tuple(_require(u, "market_etf")),
        sector_etf=tuple(_require(u, "sector_etf")),
        large_cap=tuple(_require(u, "large_cap")),
        leveraged_inverse=tuple(_require(u, "leveraged_inverse")),
        exposure_families=MappingProxyType({k: tuple(v) for k, v in fam.items()}),
    )
