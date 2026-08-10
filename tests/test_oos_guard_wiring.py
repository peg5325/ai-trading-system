"""OOS 게이트 배선 경계 테스트 (계약 §5.1 · 이슈 #75 — PR #77 독립 리뷰 차단 1 반영).

blind OOS fail-closed 가 '자발적 opt-in wrapper' 로 남지 않도록 정적(AST) 검사로 강제한다:
  · src/·scripts/·analysis/ 코드는 `load_candles` 를 직접 import/호출할 수 없다(허용 목록 제외).
  · 공식 연구 로드 경로는 `src.history.oos_guard.research_load` 하나이며, 기본
    purpose="performance" 가 fail-closed 다. coverage 우회는 호출부에 명시적으로만 쓴다.
tests/ 는 스캔 대상이 아니다(픽스처 구성에 로더 직접 사용 허용).
AST 기반이므로 docstring/주석의 언급은 위반으로 잡지 않는다(import·속성접근·이름 사용만).

범위 결정(기록): 스캔은 최상위 {src, scripts, analysis} 로 고정한다. **새 최상위 파이썬
디렉터리를 만들면 SCAN_DIRS 에 추가해야 검사가 미친다**(누락 시 무검사 — 재리뷰 지적).
알려진 한계(의도된 트립와이어 성격): `getattr(mod, "load_candles")` 문자열 접근·raw sqlite3
직접 조회는 정적 검사로 못 잡는다 — 이 테스트의 목적은 **우발적 직접 import 방지**이며,
실제 blind OOS 개봉의 증거 검증은 #78 검증 로더가 담당한다.
"""
from __future__ import annotations

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("src", "scripts", "analysis")

# load_candles 직접 참조가 허용되는 파일(정의처·하위호환 re-export·게이트 자신).
ALLOWED = {
    os.path.join("src", "history", "loader.py"),
    os.path.join("src", "history", "__init__.py"),
    os.path.join("src", "history", "oos_guard.py"),
}


def _py_files():
    for base in SCAN_DIRS:
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def _direct_load_candles_refs(path: str) -> list[str]:
    """import / 속성접근 / 이름 사용 수준의 load_candles 직접 참조 목록."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    refs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(a.name == "load_candles" for a in node.names):
                refs.append(f"L{node.lineno}: from {node.module} import load_candles")
        elif isinstance(node, ast.Attribute) and node.attr == "load_candles":
            refs.append(f"L{node.lineno}: .load_candles 속성 접근")
        elif isinstance(node, ast.Name) and node.id == "load_candles":
            refs.append(f"L{node.lineno}: load_candles 이름 사용")
    return refs


def test_no_direct_load_candles_outside_gate():
    """src/·scripts/·analysis/ 는 guarded loader(research_load) 만 쓸 수 있다."""
    offenders: dict[str, list[str]] = {}
    for path in _py_files():
        rel = os.path.relpath(path, REPO)
        if rel in ALLOWED:
            continue
        refs = _direct_load_candles_refs(path)
        if refs:
            offenders[rel] = refs
    assert offenders == {}, (
        "load_candles 직접 사용 금지 — blind OOS 게이트(oos_guard.research_load)를 "
        f"경유하라(계약 §5.1). 위반: {offenders}")


def test_allowlist_files_exist():
    """허용 목록이 실제 파일과 어긋나면(이동/개명) 검사가 조용히 무의미해지는 것을 방지."""
    for rel in sorted(ALLOWED):
        assert os.path.exists(os.path.join(REPO, rel)), f"허용 목록 파일 없음: {rel}"


def test_eval_slices_wired_through_guarded_loader():
    """공식 평가 스크립트는 guarded loader 를 coverage 목적 명시로 사용한다."""
    path = os.path.join(REPO, "scripts", "history_eval_slices.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "from src.history.oos_guard import research_load" in src
    assert 'purpose="coverage"' in src
    assert _direct_load_candles_refs(path) == []


def test_verified_unlock_factory_is_private_to_verifier():
    """production 코드가 검증 로더를 건너뛰어 private factory를 호출하지 못한다."""
    allowed = {
        os.path.join("src", "history", "oos_guard.py"),
        os.path.join("src", "history", "oos_unlock.py"),
    }
    offenders = []
    for path in _py_files():
        rel = os.path.relpath(path, REPO)
        if rel in allowed:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "_from_verified_artifact"
            ):
                offenders.append(f"{rel}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OOSUnlock"
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"OOSUnlock 검증 우회 production 호출: {offenders}"
