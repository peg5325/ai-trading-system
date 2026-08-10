# P0-US30 Intraday Gross-Edge Screen — 결과

- verdict: **NO_GROSS_EDGE**
- development: 2023-05-18~2026-01-20 (670 거래일)
- universe/candidates: 30 / 8
- blind OOS accessed: `false`
- payload SHA-256: `6af152ca13e7c1f3070f37040c7e1ae16f6ddd057a849d5d5aee3f7fe8afaeba`

## 후보별 결과

| candidate | trades | days | gross mean %p | positive folds | adjusted p | after 0.204 | after 0.340 | after 0.680 | gross pass | economic pass |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|
| gap_continuation | 19109 | 659 | -0.040019 | 5/10 | 1 | -0.244019 | -0.380019 | -0.720019 | NO | NO |
| gap_reversal | 19109 | 659 | 0.040019 | 5/10 | 0.850133 | -0.163981 | -0.299981 | -0.639981 | NO | NO |
| opening_30m_continuation | 19101 | 659 | 0.059105 | 6/10 | 0.24571 | -0.144895 | -0.280895 | -0.620895 | NO | NO |
| opening_30m_reversal | 19101 | 659 | -0.059105 | 4/10 | 1 | -0.263105 | -0.399105 | -0.739105 | NO | NO |
| first_hour_continuation | 19107 | 659 | 0.062890 | 9/10 | 0.0504204 | -0.141110 | -0.277110 | -0.617110 | NO | NO |
| first_hour_reversal | 19107 | 659 | -0.062890 | 1/10 | 1 | -0.266890 | -0.402890 | -0.742890 | NO | NO |
| morning_continuation | 19118 | 659 | 0.027037 | 5/10 | 1 | -0.176963 | -0.312963 | -0.652963 | NO | NO |
| morning_reversal | 19118 | 659 | -0.027037 | 5/10 | 1 | -0.231037 | -0.367037 | -0.707037 | NO | NO |

## 판정

- conservative-cost shortlist: 없음
- gross-only candidates: 없음
- 이 결과는 blind OOS 개봉·Paper 실행을 자동 승인하지 않는다.

## 무결성

- snapshot_file: `match`
- snapshot_content: `match`
- manifest_data: `match`
- raw_content: `match`
