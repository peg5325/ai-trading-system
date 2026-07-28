"""연구 전용 코드 루트. 매매·주문·리스크·라이브 파이프라인이 이 패키지를 import 하지 않는다.

`src/` 아래 두는 이유는 `tests/test_oos_guard_wiring.py` 의 AST 경계 검사(scan dirs =
src/scripts/analysis)가 이 트리도 스캔하게 하기 위함이다(사전등록 §3.5).
"""
