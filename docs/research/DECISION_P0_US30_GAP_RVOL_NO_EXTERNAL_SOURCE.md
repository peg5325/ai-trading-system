# 결정 — Gap+RVOL 외부 공급자 미사용 종결

> 작성: 2026-08-10 · 이슈 #109
> 성격: **결과 후 데이터 원천·비용 경계 결정.** 과학적 게이트나 전략 규칙의 개정이 아니다.

## 결정

추가 외부 데이터 공급자를 사용하지 않는다. Toss가 현재 제공하지 못하는 D9 선행 입력을 다른
원천에서 구매·수집하지 않으며, Gap+RVOL의 공식 판정은 **`INCONCLUSIVE_DATA`** 로 유지한다.

이 결정은 `NO_SELECTION`이 아니다. Gap+RVOL의 경제성이 없다고 결론낸 것이 아니라, 사전 고정한
D9 품질 한도 안에서 경제성을 판단할 수 없다는 뜻이다. 따라서 blind OOS를 열지 않고 #78 unlock
artifact도 만들지 않는다.

## 고정 증거

| 증거 | 고정 사실 | SHA-256 |
| --- | --- | --- |
| development result | verdict `INCONCLUSIVE_DATA`, next stage 없음 | `11d6e7e5f593b7b498b147b386585608d507e32d31feae4cc1b8a4b4e3542d05` |
| D9 audit | 1,296 / 20,100 = 6.447761%, 한도 5% 초과 | `14207ea572a3433c18d4c9bd80839acef45b6d1fc8b2948ef7142d8b5f68d9f6` |
| Toss overlay audit | 66개 eligible event-day 적용 후보로 해결된 exclusion 0 | `a73d114c2c6a17b7f22a2f25f9970d49b0a964ad08eb8d91865fc5c8cdc509cd` |
| dependency plan | 고유 대상 458, prior close 193, RVOL lookback 448 | `7a27ee176f910e943c955ac2db034dfebf47d7f2ffa5583c3edf244d576014c0` |
| Toss dependency probe | 265 가용 / 193 불가, prior-close 193 전부 불가 | `7d27367c20f4d43444a1f622ef4331f07c1bb666529cae54227165e5ce9e1717` |

## 다음 후보를 시작하지 않는 이유

기존 r3 계획과 Gap+RVOL 사전등록은 D9 실패 시 결함 교정·독립 재리뷰 전까지 다음 후보 이동을
금지한다. 또한 후보 3 VWAP/ATR Mean Reversion도 같은 고정 1분봉 데이터와 세션 VWAP을 사용하므로,
현재 결손을 무시한 채 시작하면 동일한 품질 문제를 전략 실패로 오인할 수 있다.

따라서 이번 데이터 원천 경계에서 intraday 순차 후보 연구는 여기서 멈춘다. 향후 재개하려면 다음
중 하나를 **새 계획과 결과 전 계약**으로 먼저 승인해야 한다.

1. 현재 결손을 채울 수 있는 원천을 도입한다.
2. 완전한 새 데이터 스냅샷과 새 development/OOS 경계를 만들고 후보 연구를 처음부터 재등록한다.

현재 결정은 어느 경로도 승인하지 않는다.

## 불변식

- D9 한도, 비용, 기간, 전략 신호와 체결 규칙을 완화하지 않았다.
- 기존 RESULT, audit, snapshot, raw를 수정하거나 덮어쓰지 않았다.
- 성과·forward return을 다시 계산하지 않았다.
- blind OOS는 닫혀 있고 #78 unlock artifact는 없다.
- canonical DB, live/paper/order/risk 경로는 변경하지 않았다.
- 추가 외부 공급자 계정·adapter·API 호출·결제는 수행하지 않았다.

이 결정의 기계 판독 정본은 `DECISION_P0_US30_GAP_RVOL_NO_EXTERNAL_SOURCE.json`이다.
