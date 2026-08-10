# P0-US30 Intraday Gross-Edge Screen — 사전등록 v1.0

상태: **FROZEN BEFORE RESULTS**  
이슈: #124  
목적: blind OOS를 열지 않고, 고정된 development 구간에서 다음 정식 전략 연구로 넘길 만한
일중 방향 가설이 있는지만 선별한다.

## 1. 연구 질문과 금지 사항

질문은 하나다.

> P0-US30의 고정된 8개 단순 방향 가설 중, 거래비용 적용 전 일별 동일가중 gross return이
> 시간 fold 전반에서 안정적으로 양수인 후보가 있는가?

이 검증은 전략 선택·최적화·Paper 승격 검증이 아니다. 다음을 금지한다.

- blind OOS(2026-01-21~2026-07-21) 가격·성과 접근
- 후보별 임계값 탐색, 결과를 본 뒤 규칙 변경, 최선 후보 하나만 고르기
- live/order/broker/risk/config 변경
- 동일 날짜의 30종목을 30개의 독립 통계 표본으로 세기

## 2. 데이터 identity와 구간

ORB 사전등록과 동일한 frozen snapshot을 쓴다.

- snapshot: `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- snapshot file SHA-256: `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33`
- snapshot content SHA-256: `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4`
- tracked manifest data SHA-256: `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c`
- raw content SHA-256: `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`

4개 중 하나라도 불일치하거나 검증할 수 없으면 `INCONCLUSIVE_DATA`이고 성과 계산을 시작하지
않는다. 모든 가격 로드는 `research_load(... purpose="performance", track="intraday")`를 경유한다.

- development 평가: 2023-05-18~2026-01-20, 670 NYSE 거래일
- 시간 안정성: 기존 ORB 계약과 동일한 F01~F10 연속 fold
- blind OOS: 2026-01-21~2026-07-21, 계속 잠금
- 종목: 고정 P0-US30 30종목

## 3. 후보 8개와 체결 시각

모든 후보는 `symbol-day`당 최대 1회이며, long/short를 대칭으로 허용한다. 신호가 정확히 0이면
거래하지 않는다. 현재 거래일이 조기폐장일이거나 필요한 정확 시각의 1분봉이 하나라도 없으면
그 symbol-day는 제외한다. 가격은 REGULAR 세션만 쓴다.

| 가족 | 신호 | 신호 확정 | 진입 | 방향 |
|---|---|---:|---:|---|
| gap | 당일 09:30 open − 직전 거래일 정규장 마지막 1분봉 close | 09:30 | 09:35 open | continuation / reversal |
| opening_30m | 09:59 close − 09:30 open | 10:00 | 10:00 open | continuation / reversal |
| first_hour | 10:29 close − 09:30 open | 10:30 | 10:30 open | continuation / reversal |
| morning | 11:59 close − 09:30 open | 12:00 | 12:00 open | continuation / reversal |

공통 청산은 15:55 ET 1분봉 open이다. `continuation`은 신호 부호와 같은 방향,
`reversal`은 반대 방향이다. gross return(%p)은
`direction × (exit / entry − 1) × 100`이다. 포지션 크기·자본경쟁·복리는 적용하지 않는다.

직전 거래일 close는 NYSE 캘린더상 정확한 직전 거래일의 정규장 마지막 기대 분(정상일 15:59,
조기폐장일 12:59)이어야 한다. 오래된 stale close 대체를 금지한다.

## 4. 집계와 통계

1. 각 candidate의 symbol-day gross return을 계산한다.
2. 같은 거래일의 종목 수익을 산술평균해 **일별 동일가중 gross return** 하나를 만든다.
3. fold 효과크기는 그 fold의 일별 gross return 산술평균이다.
4. 유의성은 0이 아닌 일별 gross return의 양수 일수에 대한 exact one-sided binomial sign test다.
5. 8개 후보에 Bonferroni를 적용한다: `adjusted_p = min(raw_p × 8, 1)`.

이 통계는 횡단면 상관 때문에 동일 날짜의 종목을 독립 표본으로 취급하지 않는다. 평균 효과크기와
부호 검정은 서로 다른 역할이며 둘 다 보고한다.

## 5. 비용 진단

gross-positive 후보에만 아래 round-trip %p를 단순 차감해 경제성을 진단한다. 이는 과거 거래의
실현 체결비용을 새로 추정하는 비용감사가 아니라, 2026-08-10 비용 감사에서 정한 고정 hurdle이다.

- `published_floor`: 0.204%p — 현재 공개 수수료·매도 규제비용 상단을 보수적으로 반올림한 바닥
- `conservative`: 0.340%p — 저장소 공식 연구 기준
- `stress`: 0.680%p — 스트레스 기준

## 6. 판정

후보별 `gross_edge_pass`는 다음을 **전부** 만족해야 한다.

- G1: 거래 수 ≥ 536
- G2: 거래가 있는 일수 ≥ 322
- G3: 전체 일별 동일가중 gross 평균 > 0
- G4: 10개 fold 중 양수 fold ≥ 7
- G5: Bonferroni 조정 one-sided sign-test p < 0.05

후보별 `economic_pass`는 `gross_edge_pass`이면서 conservative 차감 후 평균 > 0이다.
published_floor/stress 통과 여부는 별도 보고한다.

전체 verdict:

- `SHORTLIST`: economic_pass 후보가 하나 이상. 모두 다음 정식 사전등록 후보로 나열하며 하나를 고르지 않는다.
- `GROSS_EDGE_NOT_ECONOMIC`: gross_edge_pass는 있으나 economic_pass는 없다.
- `NO_GROSS_EDGE`: gross_edge_pass 후보가 없다.
- `INCONCLUSIVE_DATA`: identity·구간·필수 데이터·결정론 검증 결함으로 판정 불가.

어떤 verdict도 blind OOS 개봉이나 Paper 실행을 자동 승인하지 않는다.

## 7. 구현 전 필수 테스트

- 8개 후보 registry와 신호/진입/청산 시각 고정
- 신호 확정 다음 시각 진입 및 미래봉 미사용
- continuation/reversal 부호 대칭
- 직전 거래일·조기폐장 close 검증과 현재 조기폐장 제외
- 입력 순서 불변, 동일 결과 JSON byte 결정론
- 일별 동일가중 집계(종목 수를 통계 표본 수로 쓰지 않음)
- exact sign-test 고정값과 Bonferroni 8개
- blind OOS 접근 차단, identity fail-closed
- JSON ↔ Markdown 결과 표 정합

## 8. 실행 규율

이 문서와 동형 JSON을 결과 계산 전에 별도 커밋한다. 그 뒤 구현·테스트를 커밋하고 단 한 번
공식 실행한다. 결과 문서는 별도 커밋한다. 데이터 무효화 결함이 아닌 한 결과를 본 뒤 이 계약을
개정하지 않는다.

