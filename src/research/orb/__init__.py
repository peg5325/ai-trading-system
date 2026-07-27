"""ORB(Opening Range Breakout) 사전등록 연구 구현 — 이슈 #91 (4-A: 공통 기반).

사전등록: `docs/research/PREREG_P0_US30_ORB_V1.md` / `.json` (이슈 #89, merged, 내용 해시로 동결).
이 패키지는 그 사전등록의 **구현**이며, 임의로 임계값을 재정의하지 않는다 — 모든 수치는
`contract.py` 를 통해 동결된 JSON 에서만 읽는다.

4-A 범위(공통 기반, 이 패키지): 계약 로더 · P3 identity 검증 · 구간/fold/캘린더 · 게이트
경유 로딩 · opening range/gap/RVOL 피처. **성과·수익률·PnL 계산은 4-B 이후 범위이며 여기
없다.** blind OOS 구간(2026-01-21~2026-07-21)은 이 패키지 어디서도 로드하지 않는다.

leaf 패키지 — 매매·주문·리스크·라이브 파이프라인은 이 패키지를 import 하지 않는다.
"""
