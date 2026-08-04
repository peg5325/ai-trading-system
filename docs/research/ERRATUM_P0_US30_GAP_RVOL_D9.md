# Gap+RVOL development D9 정오표 E1

> 대상: `RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.{json,md}`
> 성격: 결과 이후 발견된 **결함 계수 매핑 오류**의 append-only 정정
> 범위: development-only · blind OOS 미개봉 · 성과 재계산 없음 · 전략 계약 변경 없음

## 1. 정정 사항

원 결과는 고정 스냅샷 manifest의 `conflict` 합계 28건을 D9의 중대 결함 28건으로
계수했다. 그러나 이 28건은 Gap+RVOL 결과가 나오기 전인 ORB 사전등록에서 이미 다음과 같이
판정·고정된 기준선이다.

- 근거: `PREREG_P0_US30_ORB_V1.json`
- 파일 SHA-256: `4f69e8539a1140a94a9e14ceb10249352f130e3bc4bb98f518a2492089847559`
- 기준선 고정 commit: `17681a7c1b893f035a7c4f645e08d98734e00fec`
- 합계: 28건 — SPY 3 · QQQ 7 · NVDA 5 · VST 1 · SOXL 12
- 승격 단계 conflict candle 1건은 위 28건 중 부분집합이며 별도 결함 기준이 아니다.
- 판정 의미: first-write-wins로 결정론적으로 해소되고 원본 증거가 보존됐으므로, **이
  기준선을 초과하는 새 conflict만 결함**이다.

Gap+RVOL 사전등록도 invalidation-grade 대상을 “새 timestamp value conflict”로 한정한다.
따라서 원 runner가 전체 28건을 새 결함으로 다시 더한 것은 구현 매핑 오류다. 고정
스냅샷의 종목별 수치는 기준선과 같으므로 새 conflict는 0건이고, 다른 raw major defect와
identity failure도 0건이다. 교정된 major defect count는 **0**이다.

원 RESULT와 receipt는 당시 실행 증거로 보존하며 덮어쓰지 않는다. 후속 runner는 추적된
기준선보다 종목별 conflict가 감소하면 provenance 이상으로 fail-closed하고, 증가분만 새
중대 결함으로 계수한다.

## 2. 판정에 미치는 영향

결론은 바뀌지 않는다.

| 항목 | 원 결과 | 교정 |
|---|---:|---:|
| major defect count | 28 | **0** |
| data exclusion | 1,296 / 20,100 = 6.4478% | 동일 |
| D9 한도 | ≤ 5% | 동일 |
| D9 | FAIL | **FAIL** |
| verdict | INCONCLUSIVE_DATA | **INCONCLUSIVE_DATA** |

데이터 제외율은 고정된 5% 한도를 초과한다. 이 정정은 GO를 쉽게 만들거나 경제성 실패를
무시하지 않는다. D9가 계속 실패하므로 blind OOS 개봉과 다음 전략 후보 진행은 금지된다.

## 3. 제외율 감사

`AUDIT_P0_US30_GAP_RVOL_D9.{json,md}`가 동일 고정 스냅샷에서 성과 계산 없이 D9 입력만
재구성했다.

- 고유 제외 종목-일: 1,296 / 20,100
- 사유: incomplete_decision_bar 734 · incomplete_rvol_window 708 ·
  insufficient_rvol_lookback 566 · no_previous_regular_close 723
- 30종목 전부 제외된 거래일: 2024-08-01 · 2024-08-02 · 2024-08-05
- 모든 parameter spec의 제외 signature: 동일
- 감사 JSON SHA-256: `14207ea572a3433c18d4c9bd80839acef45b6d1fc8b2948ef7142d8b5f68d9f6`

이는 현재 고정 스냅샷의 결측을 증명한다. 공급자 원천에 해당 과거 구간이 영구적으로 없는지,
다른 검증 가능한 소스로 복구할 수 있는지는 별도 데이터 수급 판단이며 이 정오표가 단정하지
않는다.

## 4. 허용되는 다음 조치

1. 동일 timestamp 범위를 독립적으로 검증할 수 있는 원천을 확보한다.
2. raw/canonical/snapshot provenance를 새 identity로 고정한다.
3. 결측만 복구했음을 독립 리뷰로 확인한다.
4. 기존 RESULT를 덮지 않고 동일 사전등록·동일 비용·동일 게이트로 D9를 재판정한다.

위 조건 전에는 OOS unlock을 만들지 않으며, 성과 부진을 데이터 결함으로 재분류하지 않는다.
