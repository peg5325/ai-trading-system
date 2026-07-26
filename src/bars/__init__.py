"""봉(bar) 집계 kernel — 연구·라이브·replay 공용.

**의도적으로 leaf 다**: `src/models.py` 외에 어떤 것도 import 하지 않는다(저장소·로더·주문 무관).
`src/engines`(라이브 결정 경로)와 `src/history`(오프라인 데이터 인프라)가 **둘 다** 이 kernel 을
쓰기 때문에, 어느 한쪽 패키지 안에 두면 다른 쪽이 그 패키지의 초기화(sqlite 로더 등)를 끌어오게
된다(이슈 #76 독립 리뷰 R1). 이 패키지에는 부수효과 있는 import 를 넣지 말 것.
"""
