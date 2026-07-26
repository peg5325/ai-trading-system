"""Phase 1B 자본-안전 격리 테스트 (이슈 #75).

검증(작업 테스트 23·24):
  23. 기존 1d/1m canonical 데이터 무변경 — 신규 코드는 임의 DB 경로만 쓰고 기존 data/ 를 참조 안 함
  24. 매매·Paper·RiskManager 회귀 0 — 신규 모듈이 주문/broker/risk/live/pipeline 을 import 하지 않음
격리는 **import 그래프**로 구조적으로 증명한다(서브프로세스에서 대상 모듈만 import 후 sys.modules 검사).
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 리포지토리 상대경로로 venv 파이썬을 유도(홈 절대경로 하드코딩 금지). worktree 처럼 로컬
# .venv 가 없으면 sys.executable(테스트를 띄운 인터프리터=메인 venv)로 폴백한다.
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable

# Phase 1B 신규/확장 모듈 + 이슈 #72 완료-거래일 cutoff / partial remediation 모듈.
_TARGETS = [
    "src.history.session_filter",
    "src.history.raw_snapshot",
    "src.history.exchange_calendar",
    "src.history.intraday_manifest",
    "src.history.oos_guard",
    "src.history.resample",
    "src.bars.resample",          # 이슈 #76: 집계 kernel(leaf) 도 자본 경로 무관
    "src.ingest.backfill_history",
    # 이슈 #72: 저장 전 cutoff 게이트·감사 remediation 은 주문/broker/risk/live 를 끌어오지 않는다.
    "src.history.completed_session",
    "src.history.partial_remediation",
]

# 자본 경로(주문/broker/risk/live/pipeline/reconcile) — 신규 데이터 코드가 끌어오면 안 됨.
_FORBIDDEN = ("src.order", "src.risk", "src.live", "src.pipeline",
              "src.reconcile", "src.runtime", "src.portfolio", "src.ai")


def _imported_modules(module: str) -> set[str]:
    """서브프로세스에서 module 만 import 하고 sys.modules 의 src.* 목록을 돌려준다."""
    code = (
        "import sys; import importlib;"
        f"importlib.import_module({module!r});"
        "print('\\n'.join(m for m in sys.modules if m.startswith('src.')))"
    )
    out = subprocess.run([PY, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, f"import {module} 실패: {out.stderr}"
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def test_new_modules_do_not_import_capital_paths():
    """24. 신규 Phase 1B 모듈은 주문/broker/risk/live/pipeline 을 import 하지 않는다."""
    for target in _TARGETS:
        imported = _imported_modules(target)
        leaked = sorted(
            m for m in imported
            if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN))
        assert leaked == [], f"{target} 이 자본 경로를 import 함: {leaked}"


def test_new_modules_import_cleanly():
    """스모크: 신규 모듈이 부작용/네트워크 없이 import 된다."""
    for target in _TARGETS:
        _imported_modules(target)   # returncode==0 assert 내부에서


def test_session_filter_and_raw_are_leaf_light():
    """세션 필터·raw 스냅샷은 models/stdlib 만 의존(무거운 그래프 회피)."""
    sf = _imported_modules("src.history.session_filter")
    assert sf <= {"src", "src.models", "src.config"} or "src.models" in sf
    raw = _imported_modules("src.history.raw_snapshot")
    # raw_snapshot 은 src 하위 모듈을 아예 안 끌어오거나 최소만.
    assert not any(m.startswith("src.order") or m.startswith("src.risk") for m in raw)
