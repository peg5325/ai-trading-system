# 결과 — P0-US30 큰 갭하락 후 VWAP 회복 development

> 판정: **NO_SELECTION** · blind OOS 미개봉

## 핵심 결과

- 거래: 26건 / 거래 발생일 24일
- official 순손익: -1,055.145035
- official 거래당 기대값: -40.582501
- official PF: 0.721390
- official MDD: 0.013643
- 양수 fold: 3/10
- 실패 게이트: D1, D2, D3, D4, D6, D7

## 비용별 결과

| 비용 | 누적 순손익 | 기대값 | PF | MDD | 양수 fold |
|---|---:|---:|---:|---:|---:|
| low | -98.402105 | -3.784696 | 0.969833 | 0.012210 | 3/10 |
| official | -1,055.145035 | -40.582501 | 0.721390 | 0.013643 | 3/10 |
| stress | -2,681.608017 | -103.138770 | 0.439976 | 0.028190 | 3/10 |

## 데이터 품질

- 불완전 종목-일: 962 / 20100 (4.7861%)
- 직전 거래일 종가 누락: 0 (0.0000%)
- 신규 충돌: 0
- 중대 결함: 0

## 규율

- 결과 전에 커밋한 primary 하나만 실행했다.
- 2026-01-21~2026-07-21 blind OOS는 로드하지 않았다.
- 외부 공급자, Paper/실거래, 주문·위험·config 변경은 없었다.
- 결과 payload SHA-256: `ece0ab96d36eb3880ecb3323e48f4bd8e6b2704f679650387fbac4c24b42db06`
- payload SHA-256은 해당 해시 필드를 넣기 전 canonical payload에 대해 계산했다.

## 실행 감사

- post_result_merge_review: `signal_completion_window_compared_against_start_label` → `compare_canonical_5m_start_label_plus_5_minutes`; 성과 사전열람=True, 전략 규칙 변경=False
